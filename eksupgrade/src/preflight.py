"""Read-only preflight checks for an EKS cluster upgrade.

This module performs NO mutating AWS or Kubernetes calls. It inspects the
target cluster across four areas (control plane, addons, managed node groups,
Karpenter), renders a summary report, and returns a PreflightResult whose
exit_code() follows the kubeadm/eksup severity convention:
    0 - all checks passed (warnings allowed)
    1 - at least one blocking issue
    2 - the check itself could not run
"""

from __future__ import annotations

from dataclasses import dataclass, field

import boto3
from kubernetes import client as k8s_client
from packaging.version import parse as parse_version
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from eksupgrade.models.eks import _default_next_minor
from eksupgrade.src.k8s_client import loading_config
from eksupgrade.src.karpenter import _list_nodeclaims, _list_nodepools, classify_ami_selector, get_ec2nodeclasses
from eksupgrade.src.latest_ami import get_latest_ami

_VALID_SEVERITIES: frozenset[str] = frozenset({"pass", "warning", "blocking"})


@dataclass
class PreflightFinding:
    """A single preflight observation."""

    area: str
    item: str
    severity: str  # "pass" | "warning" | "blocking"
    detail: str

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(f"severity must be one of {sorted(_VALID_SEVERITIES)}, got {self.severity!r}")


@dataclass
class PreflightResult:
    """Aggregated preflight findings and overall outcome."""

    findings: list[PreflightFinding] = field(default_factory=list)
    check_failed: bool = False

    @property
    def blocking_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "blocking")

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warning")

    def exit_code(self) -> int:
        if self.check_failed:
            return 2
        return 1 if self.blocking_count > 0 else 0


def _check_control_plane(cluster) -> list[PreflightFinding]:
    """Check that the control plane can move one minor version, and is ACTIVE."""
    findings: list[PreflightFinding] = []
    area = "Control Plane"

    if cluster.updating:
        findings.append(
            PreflightFinding(
                area, "status", "blocking", f"Cluster is UPDATING ({cluster.status}); wait for it to finish"
            )
        )
    elif not cluster.active:
        findings.append(
            PreflightFinding(area, "status", "blocking", f"Cluster is not ACTIVE (status: {cluster.status})")
        )
    else:
        findings.append(PreflightFinding(area, "status", "pass", "Cluster is ACTIVE"))

    if cluster.version == cluster.target_version:
        findings.append(
            PreflightFinding(
                area, "version", "warning", f"Already on target version {cluster.version}; nothing to upgrade"
            )
        )
    elif parse_version(cluster.target_version) < parse_version(cluster.version):
        findings.append(
            PreflightFinding(
                area, "version", "blocking", f"Downgrade {cluster.version} -> {cluster.target_version} is not supported"
            )
        )
    elif cluster.target_version == _default_next_minor(cluster.version):
        findings.append(
            PreflightFinding(area, "version", "pass", f"{cluster.version} -> {cluster.target_version} (single minor)")
        )
    else:
        next_hop = _default_next_minor(cluster.version)
        findings.append(
            PreflightFinding(
                area,
                "version",
                "blocking",
                f"Multi-minor jump {cluster.version} -> {cluster.target_version}; EKS allows one minor at a time (next: {next_hop})",
            )
        )

    return findings


def _custom_ng_current_image(ng, region: str) -> str:
    """Return a CUSTOM node group's current AMI ImageLocation (the OS hint).

    Mirrors the runtime resolver (ManagedNodeGroup._resolve_custom_target_ami)
    so preflight judges the SAME facts the upgrade will act on.
    """
    ec2 = boto3.client("ec2", region_name=region)
    lt_id = ng.launch_template["id"]
    lt_version = str(ng.launch_template["version"])
    lt_versions = ec2.describe_launch_template_versions(LaunchTemplateId=lt_id, Versions=[lt_version]).get(
        "LaunchTemplateVersions", []
    )
    if not lt_versions:
        raise Exception(f"launch template {lt_id} version {lt_version} not found")
    current_ami = lt_versions[0]["LaunchTemplateData"]["ImageId"]
    images = ec2.describe_images(ImageIds=[current_ami]).get("Images", [])
    if not images:
        raise Exception(f"current AMI {current_ami} not found (it may have been deregistered)")
    return images[0]["ImageLocation"]


def _check_managed_nodegroups(cluster, region: str) -> list[PreflightFinding]:
    """Check each managed node group; for CUSTOM amiType, verify target AMI resolves.

    For CUSTOM the tool must resolve a new AMI itself (AWS rejects version-only
    updates), so a failed resolve is blocking. The OS is determined from the
    launch template's ACTUAL current AMI (not assumed): a CUSTOM group backed
    by an AL2 image is blocking (AL2 ends at 1.32 and the runtime resolver
    cannot classify it either). Non-CUSTOM groups are AWS-managed rolling
    upgrades and only reported as pass.
    """
    findings: list[PreflightFinding] = []
    area = "Managed NodeGroups"

    for ng in cluster.nodegroups:
        # AL2 EKS-optimized AMIs end at Kubernetes 1.32 — an AL2 node group
        # (amiType AL2_*; AL2023 uses the AL2023_ prefix) cannot be upgraded
        # past that, so block before the upgrade fails mid-flight.
        if ng.ami_type.startswith("AL2_") and parse_version(cluster.target_version) > parse_version("1.32"):
            findings.append(
                PreflightFinding(
                    area,
                    ng.name,
                    "blocking",
                    f"amiType {ng.ami_type}: AL2 AMIs end at 1.32 — migrate to AL2023 before targeting "
                    f"{cluster.target_version}",
                )
            )
            continue

        if ng.ami_type != "CUSTOM":
            findings.append(
                PreflightFinding(area, ng.name, "pass", f"amiType {ng.ami_type}; AWS-managed rolling upgrade")
            )
            continue

        try:
            os_hint = _custom_ng_current_image(ng, region)
        except Exception as exc:  # noqa: BLE001 - read-only check must not abort
            findings.append(
                PreflightFinding(area, ng.name, "blocking", f"CUSTOM: could not inspect the launch template AMI: {exc}")
            )
            continue

        lowered_hint = os_hint.lower()
        if "amazon-eks-node" in lowered_hint and "al2023" not in lowered_hint:
            # The CUSTOM launch template actually points at an AL2 image.
            if parse_version(cluster.target_version) > parse_version("1.32"):
                detail = (
                    f"CUSTOM launch template uses an AL2 image ({os_hint}); AL2 AMIs end at 1.32 — "
                    f"migrate to AL2023 or Bottlerocket before targeting {cluster.target_version}"
                )
            else:
                detail = f"CUSTOM launch template uses an AL2 image ({os_hint}), which this tool cannot auto-resolve"
            findings.append(PreflightFinding(area, ng.name, "blocking", detail))
            continue

        if "Windows_Server" in os_hint:
            os_hint = os_hint[:46]  # same trim the runtime resolver applies

        try:
            # Mirror the runtime resolver: get_latest_ami keys the OS family off
            # instance_type, so both arguments carry the actual image hint.
            ami = get_latest_ami(cluster.target_version, os_hint, os_hint, region)
            if not ami or ami == "NAN":
                findings.append(
                    PreflightFinding(area, ng.name, "blocking", f"CUSTOM ({os_hint}): target AMI did not resolve")
                )
            else:
                findings.append(
                    PreflightFinding(area, ng.name, "pass", f"CUSTOM ({os_hint}); target AMI resolves to {ami}")
                )
        except Exception as exc:  # noqa: BLE001 - read-only check must not abort
            findings.append(
                PreflightFinding(area, ng.name, "blocking", f"CUSTOM ({os_hint}): could not resolve target AMI: {exc}")
            )

    return findings


def _check_karpenter(cluster, region: str) -> list[PreflightFinding]:
    """Inspect Karpenter NodeClasses/NodePools/NodeClaims read-only.

    Karpenter node upgrades happen via drift, not by this tool, so nothing here
    is blocking. We surface the AMI-selector style (alias auto-drifts; pinned
    does not) and warn on a broken state (orphaned NodeClaims with no NodePool).
    """
    findings: list[PreflightFinding] = []
    area = "Karpenter"

    try:
        nodeclasses = get_ec2nodeclasses(cluster.name, region)
    except Exception:  # noqa: BLE001 - CRD absence means Karpenter not in use
        return [PreflightFinding(area, "karpenter", "pass", "Karpenter not detected (skipped)")]

    try:
        # get_ec2nodeclasses configures kube access internally; call loading_config
        # explicitly here too so this does not depend on that hidden side-effect.
        loading_config(cluster.name, region)
        custom_api = k8s_client.CustomObjectsApi()
        nodepools = _list_nodepools(custom_api)
        nodeclaims = _list_nodeclaims(custom_api)
    except Exception as exc:  # noqa: BLE001 - read-only check must not abort
        return [PreflightFinding(area, "karpenter", "warning", f"Could not list NodePools/NodeClaims: {exc}")]

    if not nodeclasses and not nodepools and nodeclaims:
        findings.append(
            PreflightFinding(
                area,
                "nodeclaims",
                "warning",
                f"{len(nodeclaims)} NodeClaim(s) remain but no NodePool/EC2NodeClass exist; Karpenter stack appears torn down (manual cleanup likely needed)",
            )
        )
        return findings

    # Reaches here only when nodeclasses/nodepools are both empty and no nodeclaims
    # remain (the orphaned-claims case returned above). A non-empty nodeclasses set
    # falls through to the per-class loop below.
    if not nodeclasses and not nodepools and not nodeclaims:
        return [PreflightFinding(area, "karpenter", "pass", "Karpenter not in use (no NodePools)")]

    for nc in nodeclasses:
        name = nc.get("metadata", {}).get("name", "?")
        style = classify_ami_selector(nc)
        if style == "alias":
            detail = "alias selector; nodes auto-drift on control-plane upgrade"
        else:
            detail = "pinned selector (id/name/tags); nodes will NOT auto-drift"
        findings.append(PreflightFinding(area, name, "warning" if style == "pinned" else "pass", detail))

    return findings


def _pdb_covers(pdb_match_labels: dict, workload_labels: dict) -> bool:
    """True if every PDB selector label is present (subset match) in the workload labels."""
    if not pdb_match_labels:
        # An empty selector matches everything in k8s, but we treat it as
        # non-covering to stay conservative (a false "uncovered" warning is
        # low-harm; masking a real gap is worse). matchExpressions are not
        # evaluated, so a PDB using only matchExpressions lands here too.
        return False
    return all(workload_labels.get(k) == v for k, v in pdb_match_labels.items())


def _check_pod_disruption_budgets(cluster, region: str) -> list[PreflightFinding]:
    """Warn about replicas>=2 workloads not covered by any PodDisruptionBudget.

    During an upgrade these workloads are drained without an availability floor.
    Read-only: lists Deployments/StatefulSets and PDBs. Never blocking.

    Coverage is judged by matchLabels subset-match within the same namespace.
    PDB matchExpressions are NOT evaluated, so a PDB selecting purely via
    matchExpressions may produce a false "uncovered" warning (acceptable for a
    warning-only check; the operator can verify).
    """
    area = "Pod Disruption Budgets"
    try:
        loading_config(cluster.name, region)
        apps = k8s_client.AppsV1Api()
        policy = k8s_client.PolicyV1Api()
        deployments = apps.list_deployment_for_all_namespaces().items
        statefulsets = apps.list_stateful_set_for_all_namespaces().items
        pdbs = policy.list_pod_disruption_budget_for_all_namespaces().items
    except Exception as exc:  # noqa: BLE001 - read-only check must not abort
        return [PreflightFinding(area, "pdb", "warning", f"Could not list workloads/PDBs: {exc}")]

    pdbs_by_ns: dict[str, list[dict]] = {}
    for pdb in pdbs:
        ns = pdb.metadata.namespace
        match_labels = (pdb.spec.selector.match_labels if pdb.spec and pdb.spec.selector else None) or {}
        pdbs_by_ns.setdefault(ns, []).append(match_labels)

    findings: list[PreflightFinding] = []
    for kind, workloads in (("Deployment", deployments), ("StatefulSet", statefulsets)):
        for wl in workloads:
            replicas = wl.spec.replicas or 0
            if replicas < 2:
                continue
            ns = wl.metadata.namespace
            labels = (
                wl.spec.template.metadata.labels if wl.spec.template and wl.spec.template.metadata else None
            ) or {}
            covered = any(_pdb_covers(ml, labels) for ml in pdbs_by_ns.get(ns, []))
            if not covered:
                findings.append(
                    PreflightFinding(
                        area,
                        f"{ns}/{wl.metadata.name}",
                        "warning",
                        f"{kind}, replicas={replicas}, no PDB covers it",
                    )
                )

    if not findings:
        findings.append(PreflightFinding(area, "pdb", "pass", "All multi-replica workloads are covered by a PDB"))
    return findings


_SEVERITY_BADGE = {"pass": "[green]PASS[/green]", "warning": "[yellow]WARN[/yellow]", "blocking": "[red]BLOCK[/red]"}


def _render_report(cluster, result: PreflightResult) -> None:
    """Print a rich summary report (areas as tables, overall verdict at the bottom)."""
    console = Console()
    console.print(
        Panel(
            f"Cluster: [bold]{cluster.name}[/bold]   "
            f"{cluster.version} -> {cluster.target_version}   region: {cluster.region}",
            title="eksupgrade preflight (read-only)",
        )
    )

    # Derive areas from findings (insertion-ordered) so a new check can never be
    # silently omitted from the report by forgetting to list it here.
    for area in dict.fromkeys(f.area for f in result.findings):
        rows = [f for f in result.findings if f.area == area]
        if not rows:
            continue
        table = Table(title=area, show_lines=False)
        table.add_column("Item")
        table.add_column("Status")
        table.add_column("Detail")
        for f in rows:
            table.add_row(f.item, _SEVERITY_BADGE.get(f.severity, f.severity), f.detail)
        console.print(table)

    if result.blocking_count > 0:
        verdict = "[red]NOT SAFE — resolve blocking issues before upgrading[/red]"
    elif result.warning_count > 0:
        verdict = "[yellow]SAFE TO UPGRADE — review warnings[/yellow]"
    else:
        verdict = "[green]SAFE TO UPGRADE[/green]"
    console.print(f"\nBlocking: {result.blocking_count}   Warnings: {result.warning_count}   {verdict}")


def run_preflight(cluster, region: str) -> PreflightResult:
    """Run all read-only preflight checks, print a report, and return the result.

    Each individual check absorbs its own read-only lookup failures (recording
    them as warnings), so this function always returns check_failed=False. The
    exit-code-2 "could not run the checks at all" case is owned by the caller
    (cli.py), which wraps the preceding Cluster.get() in its own error handling.
    """
    findings: list[PreflightFinding] = []
    findings += _check_control_plane(cluster)
    findings += _check_addons(cluster)
    findings += _check_managed_nodegroups(cluster, region)
    findings += _check_karpenter(cluster, region)
    findings += _check_pod_disruption_budgets(cluster, region)

    result = PreflightResult(findings=findings, check_failed=False)
    _render_report(cluster, result)
    return result


def _check_addons(cluster) -> list[PreflightFinding]:
    """Check each installed addon has a target-compatible version available."""
    findings: list[PreflightFinding] = []
    area = "Addons"

    for addon in cluster.addons:
        try:
            available = addon.available_versions
            target = addon.target_version
        except Exception as exc:  # noqa: BLE001 - read-only check must not abort
            findings.append(
                PreflightFinding(area, addon.name, "warning", f"Could not resolve compatible versions: {exc}")
            )
            continue

        if available:
            findings.append(PreflightFinding(area, addon.name, "pass", f"{addon.version} -> {target or '(default)'}"))
        else:
            findings.append(
                PreflightFinding(area, addon.name, "blocking", "No compatible version for target cluster version")
            )

    return findings
