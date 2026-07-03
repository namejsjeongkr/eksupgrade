"""Karpenter drift-based node upgrade logic.

Karpenter-managed nodes are NOT upgraded by terminating EC2 instances. They are
upgraded via Karpenter's native Drift mechanism:

1. The EKS control plane is upgraded first (the Karpenter controller stays running).
2. EC2NodeClass ``amiSelectorTerms`` that use an EKS-optimized ``alias`` re-resolve
   to the AMI matching the new Kubernetes version (this happens even for a
   date-pinned alias, because the alias resolves through a K8s-version-keyed path).
3. Karpenter adds a ``Drifted`` status condition to the affected NodeClaims and
   replaces them capacity-first, honoring PodDisruptionBudgets, disruption
   budgets, and the ``karpenter.sh/do-not-disrupt`` annotation.

This module classifies how each EC2NodeClass selects its AMI and waits, with a
bound, for drift to settle — it never pauses the controller and never force
terminates instances.

Karpenter v1 API groups:
  - NodePool / NodeClaim : ``karpenter.sh/v1``
  - EC2NodeClass         : ``karpenter.k8s.aws/v1``
"""

from __future__ import annotations

import time

from kubernetes import client

from eksupgrade.src.k8s_client import loading_config
from eksupgrade.utils import echo_info, echo_warning, get_logger

logger = get_logger(__name__)

# Karpenter v1 CRD coordinates
KARPENTER_CORE_GROUP = "karpenter.sh"
KARPENTER_AWS_GROUP = "karpenter.k8s.aws"
KARPENTER_VERSION = "v1"


def classify_ami_selector(ec2nodeclass: dict) -> str:
    """Classify how an EC2NodeClass selects its AMI.

    Returns:
        "alias"  - selector uses an EKS-optimized ``alias`` (e.g. ``al2023@latest``
                   or ``al2023@v20240807``). These re-resolve to the AMI for the new
                   Kubernetes version on a control-plane upgrade, so nodes drift
                   automatically — the tool only needs to observe.
        "pinned" - selector uses ``id``/``name``/``tags``/``ssmParameter`` (or is
                   empty/unrecognized). These do NOT track the Kubernetes version,
                   so nodes will not auto-drift on a control-plane upgrade.
    """
    terms = ec2nodeclass.get("spec", {}).get("amiSelectorTerms", []) or []
    if any("alias" in term for term in terms):
        return "alias"
    return "pinned"


def get_ec2nodeclasses(cluster_name: str, region: str) -> list[dict]:
    """Return all Karpenter EC2NodeClass objects (karpenter.k8s.aws/v1)."""
    loading_config(cluster_name, region)
    custom_api = client.CustomObjectsApi()
    response = custom_api.list_cluster_custom_object(
        group=KARPENTER_AWS_GROUP,
        version=KARPENTER_VERSION,
        plural="ec2nodeclasses",
    )
    return response.get("items", [])


def _is_nodeclaim_drifted(nodeclaim: dict) -> bool:
    """Return True if the NodeClaim still carries a Drifted=True status condition."""
    conditions = nodeclaim.get("status", {}).get("conditions", []) or []
    return any(c.get("type") == "Drifted" and c.get("status") == "True" for c in conditions)


def _nodeclaim_nodepool(nodeclaim: dict) -> str | None:
    """Return the NodePool a NodeClaim belongs to (karpenter.sh/nodepool label)."""
    labels = nodeclaim.get("metadata", {}).get("labels", {}) or {}
    return labels.get("karpenter.sh/nodepool")


def _list_nodeclaims(custom_api) -> list[dict]:
    """List all Karpenter NodeClaim objects (karpenter.sh/v1)."""
    response = custom_api.list_cluster_custom_object(
        group=KARPENTER_CORE_GROUP,
        version=KARPENTER_VERSION,
        plural="nodeclaims",
    )
    return response.get("items", [])


def _list_nodepools(custom_api) -> list[dict]:
    """List all Karpenter NodePool objects (karpenter.sh/v1)."""
    response = custom_api.list_cluster_custom_object(
        group=KARPENTER_CORE_GROUP,
        version=KARPENTER_VERSION,
        plural="nodepools",
    )
    return response.get("items", [])


def nodepools_for_nodeclasses(custom_api, nodeclass_names: set[str]) -> set[str]:
    """Return the names of NodePools whose nodeClassRef points at one of the classes."""
    matching: set[str] = set()
    for nodepool in _list_nodepools(custom_api):
        ref = nodepool.get("spec", {}).get("template", {}).get("spec", {}).get("nodeClassRef", {})
        if ref.get("name") in nodeclass_names:
            matching.add(nodepool["metadata"]["name"])
    return matching


def _minor(version: str) -> str:
    """Return the 'X.Y' minor portion of a version like 'v1.34.0' or '1.34'."""
    cleaned = version.lstrip("v").split("-")[0]
    parts = cleaned.split(".")
    return ".".join(parts[:2])


def _karpenter_nodes_off_target(core_v1_api, target_version: str, nodepools: set[str] | None) -> list[str]:
    """Return node names not on the target minor, limited to the given NodePools.

    Only nodes whose ``karpenter.sh/nodepool`` is in ``nodepools`` are checked, so
    pinned-class nodes (which are EXPECTED to stay on the old version) never hold
    the wait open. ``nodepools=None`` checks all Karpenter nodes (legacy behavior).
    """
    target = _minor(target_version)
    off_target: list[str] = []
    for node in core_v1_api.list_node().items:
        labels = node.metadata.labels or {}
        nodepool = labels.get("karpenter.sh/nodepool")
        if nodepool is None and "karpenter.sh/provisioner-name" not in labels:
            continue
        if nodepools is not None and nodepool not in nodepools:
            continue
        if _minor(node.status.node_info.kubelet_version) != target:
            off_target.append(node.metadata.name)
    return off_target


def wait_for_karpenter_drift(
    cluster_name: str,
    region: str,
    target_version: str,
    nodepools: set[str] | None = None,
    timeout: int = 1800,
    poll_interval: int = 30,
) -> bool:
    """Wait, with a bound, for Karpenter drift to actually upgrade the nodes.

    Gates on POSITIVE confirmation: every node in the drifting ``nodepools`` must
    be on the target Kubernetes minor AND no NodeClaim may still carry a
    ``Drifted`` condition. Scoping to ``nodepools`` (the ones backed by an
    alias-based EC2NodeClass) prevents pinned-class nodes — which are expected to
    stay on the old version — from holding the wait open forever. Never pauses the
    controller and never force-terminates — Karpenter honors PDBs, disruption
    budgets, and ``do-not-disrupt``.

    Returns:
        True  - all in-scope nodes reached the target version within the timeout.
        False - timed out; some in-scope nodes are still on the old version (reported, not forced).
    """
    loading_config(cluster_name, region)
    custom_api = client.CustomObjectsApi()
    core_v1_api = client.CoreV1Api()
    deadline = time.monotonic() + timeout

    while True:
        nodeclaims = _list_nodeclaims(custom_api)
        # Scope the Drifted check to the drifting NodePools, like the node check
        # below: a NodeClaim from a pinned-class pool (or one Drifted for an
        # unrelated spec change) is out of scope and must not hold the wait open.
        drifted = [
            nc["metadata"]["name"]
            for nc in nodeclaims
            if _is_nodeclaim_drifted(nc) and (nodepools is None or _nodeclaim_nodepool(nc) in nodepools)
        ]
        off_target = _karpenter_nodes_off_target(core_v1_api, target_version, nodepools)

        if not drifted and not off_target:
            echo_info(f"Karpenter drift complete — all nodes are on {_minor(target_version)}.")
            return True

        if time.monotonic() >= deadline:
            echo_warning(
                f"Timed out waiting for Karpenter drift. Still-drifting NodeClaims: {drifted}; "
                f"nodes not yet on target: {off_target}. Karpenter will keep replacing them — "
                f"check disruption budgets / do-not-disrupt.",
            )
            return False

        echo_info(f"Waiting for Karpenter drift — drifting: {drifted}, off-target nodes: {off_target}")
        time.sleep(poll_interval)


def handle_karpenter_drift(
    cluster_name: str,
    region: str,
    target_version: str,
    timeout: int = 1800,
    poll_interval: int = 30,
) -> str:
    """Orchestrate Karpenter node upgrade via drift after a control-plane upgrade.

    The Karpenter controller stays running. EC2NodeClasses using an EKS-optimized
    ``alias`` drift automatically to the new Kubernetes version's AMI; pinned
    classes (id/name/tags) do NOT, so they are warned about explicitly and the
    tool does NOT claim success for them.

    Returns one of:
        "settled"  - alias NodeClasses present and their nodes reached the target.
        "timeout"  - alias NodeClasses present but drift did not complete in time.
        "no_drift" - no NodeClasses, or only pinned ones (no auto-drift will occur).
    """
    nodeclasses = get_ec2nodeclasses(cluster_name, region)

    if not nodeclasses:
        echo_info("No Karpenter EC2NodeClasses found — nothing to drift.")
        return "no_drift"

    pinned = [nc["metadata"]["name"] for nc in nodeclasses if classify_ami_selector(nc) == "pinned"]
    aliased = [nc["metadata"]["name"] for nc in nodeclasses if classify_ami_selector(nc) == "alias"]

    if pinned:
        echo_warning(
            f"EC2NodeClasses {pinned} use a pinned AMI selector (id/name/tags) and will NOT "
            f"auto-drift on a control-plane upgrade. Their nodes may stay on the old Kubernetes "
            f"version — update their amiSelectorTerms manually to upgrade them.",
        )

    if not aliased:
        # Nothing will auto-drift; do not wait and do not report success.
        echo_warning("No alias-based EC2NodeClass found — no Karpenter nodes will auto-drift.")
        return "no_drift"

    echo_info(f"EC2NodeClasses using an alias will auto-drift to the new AMI: {aliased}")
    # Only wait on NodePools backed by an alias class — pinned-class nodes are
    # expected to stay on the old version and must not hold the wait open.
    custom_api = client.CustomObjectsApi()
    drifting_nodepools = nodepools_for_nodeclasses(custom_api, set(aliased))
    settled = wait_for_karpenter_drift(
        cluster_name,
        region,
        target_version,
        nodepools=drifting_nodepools,
        timeout=timeout,
        poll_interval=poll_interval,
    )
    return "settled" if settled else "timeout"
