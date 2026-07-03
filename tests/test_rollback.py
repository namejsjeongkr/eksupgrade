"""Test the rollback readiness module.

EKS version rollback (2026-07) reuses UpdateClusterVersion with the N-1
version; the service is the final authority on eligibility. Our insight
check is advisory: ERROR/UNKNOWN findings block unless --force, and a
failed fetch must NOT block (the server re-validates).
"""

from unittest.mock import MagicMock

from eksupgrade.src.rollback import RollbackReadiness, get_rollback_readiness, incompatible_addons


def _cluster_with_insights(insights: list[dict]) -> MagicMock:
    cluster = MagicMock()
    cluster.name = "eks-test"
    cluster.eks_client.list_insights.return_value = {"insights": insights}
    return cluster


def _insight(name: str, status: str) -> dict:
    return {"name": name, "insightStatus": {"status": status, "reason": "detail"}}


def test_readiness_blocking_on_error_and_unknown():
    cluster = _cluster_with_insights(
        [
            _insight("kubelet skew", "ERROR"),
            _insight("addon compat", "UNKNOWN"),
            _insight("api usage", "PASSING"),
            _insight("pdb", "WARNING"),
        ]
    )

    readiness = get_rollback_readiness(cluster)

    assert readiness.blocking == ["kubelet skew", "addon compat"]
    assert readiness.fetch_failed is False
    call = cluster.eks_client.list_insights.call_args
    assert call.kwargs["filter"] == {"categories": ["ROLLBACK_READINESS"]}


def test_readiness_passing_and_warning_do_not_block():
    cluster = _cluster_with_insights([_insight("api usage", "PASSING"), _insight("pdb", "WARNING")])

    readiness = get_rollback_readiness(cluster)

    assert readiness.blocking == []


def test_readiness_fetch_failure_is_advisory_not_blocking():
    """Old botocore / unsupported API must not block — EKS validates server-side."""
    cluster = MagicMock()
    cluster.name = "eks-test"
    cluster.eks_client.list_insights.side_effect = Exception("Unknown operation")

    readiness = get_rollback_readiness(cluster)

    assert readiness.fetch_failed is True
    assert readiness.blocking == []


def test_incompatible_addons_lists_versions_missing_for_target():
    """An addon whose CURRENT version is not offered for the rollback target
    must be reported (EKS never rolls back addons — the operator must)."""
    ok_addon = MagicMock()
    ok_addon.name = "kube-proxy"
    ok_addon.version = "v1.35.0-eksbuild.1"
    ok_addon.available_versions = ["v1.35.0-eksbuild.1", "v1.34.0-eksbuild.1"]

    bad_addon = MagicMock()
    bad_addon.name = "coredns"
    bad_addon.version = "v1.14.2-eksbuild.4"
    bad_addon.available_versions = ["v1.13.0-eksbuild.1"]

    cluster = MagicMock()
    cluster.addons = [ok_addon, bad_addon]

    result = incompatible_addons(cluster)

    assert result == ["coredns (v1.14.2-eksbuild.4)"]


def test_incompatible_addons_treats_lookup_failure_as_incompatible():
    """An addon absent from the target version listing entirely must be flagged."""

    class _MissingAddon:
        name = "third-party-agent"
        version = "v9.9.9"

        @property
        def available_versions(self):
            raise RuntimeError("addon not offered for this Kubernetes version")

    cluster = MagicMock()
    cluster.addons = [_MissingAddon()]

    result = incompatible_addons(cluster)

    assert result == ["third-party-agent (v9.9.9)"]


def test_rollback_readiness_dataclass_blocking_property():
    readiness = RollbackReadiness(
        findings=[
            {"name": "a", "status": "ERROR", "reason": ""},
            {"name": "b", "status": "PASSING", "reason": ""},
        ]
    )
    assert readiness.blocking == ["a"]
