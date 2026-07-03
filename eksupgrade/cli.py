"""Handle CLI specific logic and module definitions."""

from __future__ import annotations

from queue import Queue
from typing import Optional

import typer
import urllib3
from rich.console import Console
from rich.table import Table

from eksupgrade import __version__
from eksupgrade.utils import PhaseTimer, confirm, echo_error, echo_info, echo_warning, get_logger

from .exceptions import ClusterInactiveException
from .models.eks import Cluster, _previous_minor
from .src.k8s_client import cluster_auto_enable_disable, is_cluster_auto_scaler_present, is_karpenter_present
from .src.karpenter import handle_karpenter_drift
from .src.preflight import run_preflight
from .src.rollback import get_rollback_readiness, incompatible_addons
from .starter import StatsWorker, actual_update

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = get_logger(__name__)
app = typer.Typer(help="Automated Amazon EKS cluster upgrade CLI utility")
console = Console()


def version_callback(value: bool) -> None:
    """Handle the version callback."""
    if value:
        typer.secho(f"eksupgrade version: {__version__}", fg=typer.colors.BRIGHT_BLUE, bold=True)
        raise typer.Exit()


@app.callback()
def callback(
    version: Optional[bool] = typer.Option(
        None, "--version", callback=version_callback, is_eager=True, help="Display the current eksupgrade version"
    ),
) -> None:
    """Automated Amazon EKS cluster upgrade CLI utility."""


@app.command(name="upgrade")
def main(
    cluster_name: str = typer.Argument(..., help="The name of the cluster to be upgraded"),
    cluster_version: str = typer.Argument(..., help="The target Kubernetes version to upgrade the cluster to"),
    region: str = typer.Argument(..., help="The AWS region where the target cluster resides"),
    max_retry: int = typer.Option(default=2, help="The most number of times to retry an upgrade"),
    force: bool = typer.Option(default=False, help="Force the upgrade (e.g. pod eviction with PDB)"),
    preflight: bool = typer.Option(default=False, help="Run read-only pre-upgrade checks and exit without upgrading"),
    parallel: bool = typer.Option(default=False, help="Upgrade all nodegroups in parallel"),
    latest_addons: bool = typer.Option(
        default=False, help="Upgrade addons to the latest eligible version instead of default"
    ),
    disable_checks: bool = typer.Option(
        default=False, help="Disable the pre-upgrade and post-upgrade checks during upgrade scenarios"
    ),
    interactive: bool = typer.Option(default=True, help="If enabled, prompt the user for confirmations"),
) -> None:
    """Upgrade a target cluster to the next Kubernetes version."""
    queue: Queue[list[str | int | bool]] = Queue()

    if disable_checks:
        echo_warning("--disable-checks is currently unused until the new validation workflows are implemented")

    # Initialize autoscaler state variables before try block
    is_ca_present: bool = False
    ca_paused: bool = False
    ca_replicas_value: int = 0
    ca_name: str = "cluster-autoscaler"
    ca_namespace: str = "kube-system"
    is_karpenter: bool = False
    karpenter_namespace: str = ""
    worker_failures: list[str] = []
    timer = PhaseTimer()

    try:
        # Pull cluster details, populating the object for subsequent use throughout the upgrade.
        target_cluster: Cluster = Cluster.get(
            cluster_name=cluster_name, region=region, target_version=cluster_version, latest_addons=latest_addons
        )

        # Preflight is a read-only assessment: report and exit before any mutation
        # (and before announcing an upgrade). This also defuses the
        # --preflight --no-interactive trap: we always Exit here, never reaching
        # the confirm prompt or update_cluster().
        if preflight:
            try:
                preflight_result = run_preflight(target_cluster, region)
            except typer.Exit:
                raise
            except Exception as preflight_error:  # noqa: BLE001
                # A crash in the checks themselves means we could not assess the
                # cluster. Surface that as exit code 2 ("could not run") instead of
                # letting the broad handler below swallow it into a success exit.
                echo_error(f"Preflight checks could not run: {preflight_error}")
                raise typer.Exit(code=2) from preflight_error
            raise typer.Exit(code=preflight_result.exit_code())

        echo_info(
            f"Upgrading cluster: {cluster_name} from version: {target_cluster.version} to {target_cluster.target_version}...",
        )

        # Confirm whether or not to proceed following pre-flight checks.
        if interactive:
            confirm(
                f"Are you sure you want to proceed with the upgrade process against: {cluster_name}?",
            )

        if not target_cluster.available:
            echo_error("The cluster is not active!")
            raise ClusterInactiveException("The cluster is not active")

        echo_info(
            f"The current version of the cluster was detected as: {target_cluster.version}",
        )

        # Checking Cluster is Active or Not Before Making an Update
        with timer.phase("Control Plane"):
            if target_cluster.active:
                target_cluster.update_cluster(wait=True)
            else:
                echo_warning(
                    f"The target EKS cluster: {target_cluster.name} isn't currently active - status: {target_cluster.status}",
                )
                target_cluster.wait_for_active()

        echo_info("Found the following Managed Nodegroups")
        for _mng_nodegroup_name in target_cluster.nodegroup_names:
            echo_info(f"\t* {_mng_nodegroup_name}")

        managed_nodegroup_asgs: list[str] = []
        for nodegroup in target_cluster.nodegroups:
            managed_nodegroup_asgs += nodegroup.autoscaling_group_names

        # removing self-managed from managed so that we don't update them again
        asg_list_self_managed = list(set(target_cluster.asg_names) - set(managed_nodegroup_asgs))

        # addons update
        target_cluster.upgrade_addons(wait=True, timer=timer)

        # checking Cluster Autoscaler present and the value associated from it
        is_ca_present, ca_replicas_value, ca_name, ca_namespace = is_cluster_auto_scaler_present(
            cluster_name=cluster_name, region=region
        )

        # Pause Cluster Autoscaler if present (scale its deployment to 0)
        if is_ca_present:
            cluster_auto_enable_disable(
                cluster_name=cluster_name,
                operation="pause",
                mx_val=ca_replicas_value,
                region=region,
                name=ca_name,
                namespace=ca_namespace,
            )
            ca_paused = True
            echo_info(f"Paused the Cluster AutoScaler ({ca_name} in {ca_namespace})")
        else:
            echo_info("No Cluster AutoScaler is Found")

        # checking Karpenter present — the controller is left RUNNING so its
        # native Drift can replace nodes capacity-first after the control plane upgrade.
        is_karpenter, karpenter_replicas, karpenter_namespace = is_karpenter_present(
            cluster_name=cluster_name, region=region
        )
        if is_karpenter and karpenter_replicas == 0:
            # A controller at 0 replicas cannot mark or replace anything — waiting
            # for drift would just burn the full timeout.
            echo_warning(
                f"Karpenter in namespace: {karpenter_namespace} is scaled to 0 replicas — drift cannot occur; "
                "skipping the drift wait. Scale the controller up and check NodeClaims manually."
            )
            is_karpenter = False
        elif is_karpenter:
            echo_info(f"Karpenter detected in namespace: {karpenter_namespace} (left running for drift)")
        else:
            echo_info("No Karpenter is Found")

        if parallel:
            for x in range(20):
                worker = StatsWorker(queue, x, failures=worker_failures)
                worker.daemon = True
                worker.start()

        if target_cluster.upgradable_managed_nodegroups:
            _mng_nodegroup_table = Table("Name", "Version")
            for item in target_cluster.upgradable_managed_nodegroups:
                _mng_nodegroup_table.add_row(item.name, item.version)
            echo_info("Outdated managed nodegroups:")
            console.print(_mng_nodegroup_table)
        else:
            echo_warning("No outdated managed nodegroups found!")

        target_cluster.upgrade_nodegroups(wait=not parallel, timer=timer)

        # TODO: Use custom_ami to update launch templates and re-roll self-managed nodes under ASGs.
        echo_info("Found the following Self-managed Nodegroups:")
        for asg_iter in asg_list_self_managed:
            echo_info(f"\t* {asg_iter}")
            if parallel:
                queue.put([cluster_name, asg_iter, cluster_version, region, max_retry, force, "selfmanaged"])
            else:
                actual_update(cluster_name, asg_iter, cluster_version, region, max_retry, force)

        if parallel:
            queue.join()
            if worker_failures:
                raise Exception(f"Parallel node group upgrade failed for: {worker_failures}")

        # Upgrade Karpenter-managed nodes via native Drift. The control plane is
        # already upgraded, so alias-based EC2NodeClasses re-resolve to the new
        # Kubernetes version's AMI; Karpenter replaces drifted NodeClaims
        # capacity-first while honoring PDBs and disruption budgets. We only
        # observe (and warn about pinned NodeClasses that won't auto-drift).
        if is_karpenter:
            echo_info("Handling Karpenter node upgrade via drift...")
            with timer.phase("Karpenter drift"):
                drift_result = handle_karpenter_drift(
                    cluster_name=cluster_name, region=region, target_version=cluster_version
                )
            if drift_result == "settled":
                echo_info("Karpenter drift complete — nodes are on the new version")
            elif drift_result == "timeout":
                echo_warning("Karpenter drift did not complete within the timeout; check NodeClaims")
            else:  # no_drift
                echo_warning(
                    "No Karpenter nodes auto-drifted (no alias-based EC2NodeClass). "
                    "Pinned NodeClasses need manual amiSelectorTerms updates."
                )

        echo_info(f"EKS Cluster {cluster_name} UPDATED TO {cluster_version}")
    except typer.Abort:
        echo_warning("Cluster upgrade aborted!")
    except typer.Exit:
        # typer.Exit subclasses RuntimeError -> Exception, so the broad handler
        # below would otherwise swallow our clean preflight exit. Let it propagate.
        raise
    except Exception as error:
        # Karpenter is never paused (drift needs the controller running), so there
        # is nothing to re-enable here on error.
        echo_error(f"Exception encountered! Error: {error}")
    finally:
        # Resume the Cluster Autoscaler here — and ONLY here — so it also runs on
        # KeyboardInterrupt/SystemExit, which `except Exception` never sees. A
        # Ctrl+C mid-roll must not leave the autoscaler at 0 replicas.
        if ca_paused:
            try:
                cluster_auto_enable_disable(
                    cluster_name=cluster_name,
                    operation="start",
                    mx_val=ca_replicas_value,
                    region=region,
                    name=ca_name,
                    namespace=ca_namespace,
                )
                echo_info("Cluster Autoscaler is Enabled Again")
            except Exception as resume_error:
                echo_error(
                    f"Cluster Autoscaler re-enable failed and must be done manually! Error: {resume_error}",
                )

        # Emit the per-phase timing summary last, after any autoscaler-resume
        # messages. Skipped when no phase ran (e.g. the preflight Exit path).
        if timer.records:
            console.print(timer.summary_table())


@app.command()
def rollback(
    cluster_name: str = typer.Argument(..., help="The name of the cluster to be rolled back"),
    region: str = typer.Argument(..., help="The AWS region where the target cluster resides"),
    force: bool = typer.Option(
        default=False,
        help="Bypass ERROR/UNKNOWN rollback readiness insights (server prerequisites still apply)",
    ),
    interactive: bool = typer.Option(default=True, help="If enabled, prompt the user for confirmations"),
) -> None:
    """Roll the cluster back one minor version (within 7 days of an in-place upgrade).

    Order is the REVERSE of an upgrade, per the version skew policy (kubelet
    must never be newer than the apiserver): managed node groups are rolled
    back first, then the control plane. EKS never rolls back addons or
    self-managed nodes — those are reported for manual action.
    """
    is_ca_present: bool = False
    ca_paused: bool = False
    ca_replicas_value: int = 0
    ca_name: str = "cluster-autoscaler"
    ca_namespace: str = "kube-system"
    timer = PhaseTimer()

    try:
        target_cluster: Cluster = Cluster.get(cluster_name=cluster_name, region=region)
        # The rollback target is always exactly N-1; set it before anything
        # derives from target_version (nodegroup targets, addon listings).
        previous_version = _previous_minor(target_cluster.version)
        target_cluster.target_version = previous_version
        echo_info(
            f"Rolling back cluster: {cluster_name} from version: {target_cluster.version} to {previous_version}..."
        )

        if not target_cluster.active:
            echo_error(f"The cluster is not active! Status: {target_cluster.status}")
            raise ClusterInactiveException("The cluster is not active")

        # Advisory readiness gate: ERROR/UNKNOWN insights block unless --force;
        # a failed fetch never blocks (EKS re-validates server-side).
        readiness = get_rollback_readiness(target_cluster)
        if readiness.blocking and not force:
            echo_error(
                f"Rollback readiness insights report blocking issues: {readiness.blocking}. "
                "Resolve them or re-run with --force."
            )
            raise typer.Exit(code=1)

        _incompatible = incompatible_addons(target_cluster)
        if _incompatible:
            echo_warning(
                f"Addon versions not offered for {previous_version}: {_incompatible} — EKS does NOT roll back "
                "addons; downgrade them before the control plane rollback."
            )

        if interactive:
            confirm(f"Are you sure you want to roll back: {cluster_name} to {previous_version}?")

        is_ca_present, ca_replicas_value, ca_name, ca_namespace = is_cluster_auto_scaler_present(
            cluster_name=cluster_name, region=region
        )
        if is_ca_present:
            cluster_auto_enable_disable(
                cluster_name=cluster_name,
                operation="pause",
                mx_val=ca_replicas_value,
                region=region,
                name=ca_name,
                namespace=ca_namespace,
            )
            ca_paused = True
            echo_info(f"Paused the Cluster AutoScaler ({ca_name} in {ca_namespace})")

        # Worker nodes FIRST (version skew), then the control plane.
        for nodegroup in target_cluster.upgradable_managed_nodegroups:
            with timer.phase(f"nodegroup rollback: {nodegroup.name}"):
                nodegroup.update(wait=True)

        managed_nodegroup_asgs: list[str] = []
        for nodegroup in target_cluster.nodegroups:
            managed_nodegroup_asgs += nodegroup.autoscaling_group_names
        self_managed_asgs = list(set(target_cluster.asg_names) - set(managed_nodegroup_asgs))
        if self_managed_asgs:
            echo_warning(
                f"Self-managed node groups are NOT rolled back by this tool or EKS: {self_managed_asgs} — "
                "revert their AMIs manually before the control plane rollback."
            )

        with timer.phase("Control plane rollback"):
            target_cluster.rollback_cluster(wait=True, force=force)

        # Alias-based EC2NodeClasses re-resolve to the previous version's AMI
        # after the control plane rollback, so Karpenter nodes drift back.
        is_karpenter, karpenter_replicas, karpenter_namespace = is_karpenter_present(
            cluster_name=cluster_name, region=region
        )
        if is_karpenter and karpenter_replicas == 0:
            echo_warning(
                f"Karpenter in namespace: {karpenter_namespace} is scaled to 0 replicas — drift cannot occur; "
                "skipping the drift wait. Scale the controller up and check NodeClaims manually."
            )
        elif is_karpenter:
            echo_info("Handling Karpenter node rollback via drift...")
            with timer.phase("Karpenter drift"):
                drift_result = handle_karpenter_drift(
                    cluster_name=cluster_name, region=region, target_version=previous_version
                )
            if drift_result == "timeout":
                echo_warning("Karpenter drift did not complete within the timeout; check NodeClaims")

        echo_info(f"EKS Cluster {cluster_name} ROLLED BACK TO {previous_version}")
    except typer.Abort:
        echo_warning("Cluster rollback aborted!")
    except typer.Exit:
        raise
    except Exception as error:
        echo_error(f"Exception encountered! Error: {error}")
        raise typer.Exit(code=1) from error
    finally:
        # Same contract as upgrade: the CA resume must also run on
        # KeyboardInterrupt/SystemExit, so it lives in finally.
        if ca_paused:
            try:
                cluster_auto_enable_disable(
                    cluster_name=cluster_name,
                    operation="start",
                    mx_val=ca_replicas_value,
                    region=region,
                    name=ca_name,
                    namespace=ca_namespace,
                )
                echo_info("Cluster Autoscaler is Enabled Again")
            except Exception as resume_error:
                echo_error(
                    f"Cluster Autoscaler re-enable failed and must be done manually! Error: {resume_error}",
                )
        if timer.records:
            console.print(timer.summary_table())


if __name__ == "__main__":  # pragma: no cover
    app(prog_name="eksupgrade")
