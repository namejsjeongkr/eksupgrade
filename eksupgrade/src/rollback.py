"""EKS cluster version rollback readiness support.

EKS version rollback (2026-07) reuses the UpdateClusterVersion API with the
N-1 version; the update type becomes VersionRollback. The EKS service is the
final authority on eligibility (7-day window, in-place upgrade history,
sequential rollback) — the checks here are ADVISORY:

- ROLLBACK_READINESS cluster insights: ERROR/UNKNOWN findings block unless
  --force; a failed fetch never blocks (the server re-validates anyway).
- Addon compatibility: EKS never rolls back addon versions, so any addon
  whose current version is not offered for the target version is reported
  for the operator to downgrade first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.console import Console
from rich.table import Table

from eksupgrade.utils import echo_warning, get_logger

logger = get_logger(__name__)
console = Console()

_BLOCKING_STATUSES: frozenset[str] = frozenset({"ERROR", "UNKNOWN"})
_STATUS_BADGE = {
    "PASSING": "[green]PASSING[/green]",
    "WARNING": "[yellow]WARNING[/yellow]",
    "ERROR": "[red]ERROR[/red]",
    "UNKNOWN": "[red]UNKNOWN[/red]",
}


@dataclass
class RollbackReadiness:
    """ROLLBACK_READINESS insight findings and their fetch outcome."""

    findings: list[dict] = field(default_factory=list)
    fetch_failed: bool = False

    @property
    def blocking(self) -> list[str]:
        """Names of findings that block a rollback (ERROR/UNKNOWN)."""
        return [finding["name"] for finding in self.findings if finding["status"] in _BLOCKING_STATUSES]


def get_rollback_readiness(cluster) -> RollbackReadiness:
    """Fetch and render the cluster's ROLLBACK_READINESS insights."""
    try:
        response = cluster.eks_client.list_insights(
            clusterName=cluster.name,
            filter={"categories": ["ROLLBACK_READINESS"]},
        )
        insights = response.get("insights", [])
    except Exception as error:  # noqa: BLE001 - advisory only; the server re-validates
        echo_warning(
            f"Could not fetch rollback readiness insights ({error}); "
            "EKS will still validate the rollback server-side."
        )
        return RollbackReadiness(fetch_failed=True)

    findings = [
        {
            "name": insight.get("name", insight.get("id", "?")),
            "status": (insight.get("insightStatus") or {}).get("status", "UNKNOWN"),
            "reason": (insight.get("insightStatus") or {}).get("reason", ""),
        }
        for insight in insights
    ]

    if findings:
        table = Table("Check", "Status", "Reason", title="Rollback readiness insights")
        for finding in findings:
            table.add_row(finding["name"], _STATUS_BADGE.get(finding["status"], finding["status"]), finding["reason"])
        console.print(table)

    return RollbackReadiness(findings=findings)


def incompatible_addons(cluster) -> list[str]:
    """Return addons whose CURRENT version is not offered for the rollback target.

    Requires cluster.target_version to already be the rollback target, since
    the addon version listings are resolved against it. EKS never rolls back
    addons — the operator must downgrade these before the control plane.
    """
    incompatible: list[str] = []
    for addon in cluster.addons:
        try:
            if addon.version not in addon.available_versions:
                incompatible.append(f"{addon.name} ({addon.version})")
        except Exception:  # noqa: BLE001 - absent from the target listing entirely
            incompatible.append(f"{addon.name} ({addon.version})")
    return incompatible
