"""Test Karpenter drift-based node upgrade logic.

The correct way to upgrade Karpenter-managed nodes is NOT to pause the
controller and terminate instances, but to let Karpenter's native Drift do the
work: after the control plane is upgraded, EC2NodeClass amiSelectorTerms that
use an EKS-optimized `alias` re-resolve to the AMI matching the new Kubernetes
version, Karpenter marks the NodeClaims `Drifted`, and replaces them
capacity-first while respecting PDBs and disruption budgets.

These tests pin down:
  - classify_ami_selector(): which selectors auto-drift (alias) vs. won't (id/name/tags)
  - wait_for_karpenter_drift(): bounded wait, never pauses, reports stragglers

CustomObjectsApi / CoreV1Api are mocked so no cluster is required.
"""

from unittest.mock import MagicMock, patch

from eksupgrade.src.karpenter import (
    classify_ami_selector,
    get_ec2nodeclasses,
    handle_karpenter_drift,
    wait_for_karpenter_drift,
)

CLUSTER = "test-cluster"
REGION = "us-east-1"

_LOADING = "eksupgrade.src.karpenter.loading_config"
_K8S = "eksupgrade.src.karpenter.client"


class TestClassifyAmiSelector:
    """alias selectors auto-drift on control-plane upgrade; others do not."""

    def test_alias_latest_is_observe_only(self):
        ec2nodeclass = {"spec": {"amiSelectorTerms": [{"alias": "al2023@latest"}]}}
        assert classify_ami_selector(ec2nodeclass) == "alias"

    def test_alias_pinned_version_is_still_alias(self):
        """A date-pinned alias still tracks the K8s version, so it auto-drifts."""
        ec2nodeclass = {"spec": {"amiSelectorTerms": [{"alias": "al2023@v20240807"}]}}
        assert classify_ami_selector(ec2nodeclass) == "alias"

    def test_id_selector_is_pinned(self):
        ec2nodeclass = {"spec": {"amiSelectorTerms": [{"id": "ami-0123456789"}]}}
        assert classify_ami_selector(ec2nodeclass) == "pinned"

    def test_name_selector_is_pinned(self):
        ec2nodeclass = {"spec": {"amiSelectorTerms": [{"name": "my-custom-ami-1.33-*"}]}}
        assert classify_ami_selector(ec2nodeclass) == "pinned"

    def test_tags_selector_is_pinned(self):
        ec2nodeclass = {"spec": {"amiSelectorTerms": [{"tags": {"environment": "prod"}}]}}
        assert classify_ami_selector(ec2nodeclass) == "pinned"

    def test_empty_or_unknown_is_pinned(self):
        """Unrecognized selectors must NOT be assumed to auto-drift."""
        assert classify_ami_selector({"spec": {"amiSelectorTerms": []}}) == "pinned"
        assert classify_ami_selector({"spec": {}}) == "pinned"


def _nodeclaim(name: str, drifted: bool, nodepool: str = "default") -> dict:
    """Build a NodeClaim custom-object dict with/without a Drifted condition."""
    conditions = [{"type": "Drifted", "status": "True"}] if drifted else []
    return {
        "metadata": {"name": name, "labels": {"karpenter.sh/nodepool": nodepool}},
        "status": {"conditions": conditions},
    }


def _karpenter_node(name: str, kubelet_version: str, nodepool: str = "default") -> MagicMock:
    """Build a mock Karpenter-managed Node with the given kubelet version + nodepool."""
    node = MagicMock()
    node.metadata.name = name
    node.metadata.labels = {"karpenter.sh/nodepool": nodepool}
    node.status.node_info.kubelet_version = kubelet_version
    return node


def _node_list(*nodes: MagicMock) -> MagicMock:
    response = MagicMock()
    response.items = list(nodes)
    return response


def _nodepool(name: str, nodeclass_name: str) -> dict:
    """Build a NodePool custom-object dict referencing an EC2NodeClass."""
    return {
        "metadata": {"name": name},
        "spec": {"template": {"spec": {"nodeClassRef": {"name": nodeclass_name}}}},
    }


class TestGetEc2NodeClasses:
    @patch(_K8S)
    @patch(_LOADING)
    def test_lists_ec2nodeclasses_via_custom_objects_api(self, mock_loading, mock_k8s):
        api = mock_k8s.CustomObjectsApi.return_value
        api.list_cluster_custom_object.return_value = {
            "items": [
                {"metadata": {"name": "default"}, "spec": {"amiSelectorTerms": [{"alias": "al2023@latest"}]}},
                {"metadata": {"name": "gpu"}, "spec": {"amiSelectorTerms": [{"id": "ami-1"}]}},
            ]
        }

        result = get_ec2nodeclasses(CLUSTER, REGION)

        assert [nc["metadata"]["name"] for nc in result] == ["default", "gpu"]
        _, kwargs = api.list_cluster_custom_object.call_args
        assert kwargs["group"] == "karpenter.k8s.aws"
        assert kwargs["version"] == "v1"
        assert kwargs["plural"] == "ec2nodeclasses"


class TestWaitForKarpenterDrift:
    """Gate on POSITIVE confirmation that nodes reached the target version,
    not merely on the absence of a transient Drifted condition."""

    @patch("eksupgrade.src.karpenter.time.sleep", return_value=None)
    @patch(_K8S)
    @patch(_LOADING)
    def test_true_when_all_nodes_on_target_and_not_drifted(self, mock_loading, mock_k8s, mock_sleep):
        api = mock_k8s.CustomObjectsApi.return_value
        api.list_cluster_custom_object.return_value = {"items": [_nodeclaim("nc-1", drifted=False)]}
        core = mock_k8s.CoreV1Api.return_value
        core.list_node.return_value = _node_list(_karpenter_node("n-1", "v1.34.0"))

        assert wait_for_karpenter_drift(CLUSTER, REGION, "1.34", timeout=30, poll_interval=5) is True

    @patch("eksupgrade.src.karpenter.time.sleep", return_value=None)
    @patch("eksupgrade.src.karpenter.time.monotonic")
    @patch(_K8S)
    @patch(_LOADING)
    def test_false_when_node_still_on_old_version(self, mock_loading, mock_k8s, mock_monotonic, mock_sleep):
        """A node still on the OLD version must NOT be reported as settled — the
        old code returned True just because no Drifted condition was present."""
        mock_monotonic.side_effect = [0, 1, 1000]
        api = mock_k8s.CustomObjectsApi.return_value
        api.list_cluster_custom_object.return_value = {"items": [_nodeclaim("nc-1", drifted=False)]}
        core = mock_k8s.CoreV1Api.return_value
        core.list_node.return_value = _node_list(_karpenter_node("n-1", "v1.33.5"))

        assert wait_for_karpenter_drift(CLUSTER, REGION, "1.34", timeout=30, poll_interval=5) is False

    @patch("eksupgrade.src.karpenter.time.sleep", return_value=None)
    @patch(_K8S)
    @patch(_LOADING)
    def test_waits_until_nodes_reach_target(self, mock_loading, mock_k8s, mock_sleep):
        api = mock_k8s.CustomObjectsApi.return_value
        # Drifted clears across polls.
        api.list_cluster_custom_object.side_effect = [
            {"items": [_nodeclaim("nc-1", drifted=True)]},
            {"items": [_nodeclaim("nc-1", drifted=False)]},
        ]
        core = mock_k8s.CoreV1Api.return_value
        # First poll: old node still present; second poll: replaced on target.
        core.list_node.side_effect = [
            _node_list(_karpenter_node("n-old", "v1.33.5")),
            _node_list(_karpenter_node("n-new", "v1.34.0")),
        ]

        assert wait_for_karpenter_drift(CLUSTER, REGION, "1.34", timeout=30, poll_interval=5) is True

    @patch("eksupgrade.src.karpenter.time.sleep", return_value=None)
    @patch("eksupgrade.src.karpenter.time.monotonic")
    @patch(_K8S)
    @patch(_LOADING)
    def test_out_of_scope_drifted_nodeclaim_does_not_hold_wait(
        self, mock_loading, mock_k8s, mock_monotonic, mock_sleep
    ):
        """A Drifted NodeClaim from a NON-drifting NodePool (pinned class, or drifted
        for an unrelated spec change) is out of scope and must not hold the wait
        open — the off_target node check is scoped, so the NodeClaim check must be too."""
        mock_monotonic.side_effect = [0, 1, 1000]
        api = mock_k8s.CustomObjectsApi.return_value
        api.list_cluster_custom_object.return_value = {
            "items": [_nodeclaim("nc-pinned", drifted=True, nodepool="np-pinned")]
        }
        core = mock_k8s.CoreV1Api.return_value
        core.list_node.return_value = _node_list(_karpenter_node("n-alias", "v1.34.0", nodepool="np-alias"))

        assert (
            wait_for_karpenter_drift(CLUSTER, REGION, "1.34", nodepools={"np-alias"}, timeout=30, poll_interval=5)
            is True
        )

    @patch("eksupgrade.src.karpenter.time.sleep", return_value=None)
    @patch("eksupgrade.src.karpenter.time.monotonic")
    @patch(_K8S)
    @patch(_LOADING)
    def test_in_scope_drifted_nodeclaim_still_holds_wait(self, mock_loading, mock_k8s, mock_monotonic, mock_sleep):
        """A Drifted NodeClaim that IS in a drifting NodePool must keep the wait open."""
        mock_monotonic.side_effect = [0, 1, 1000]
        api = mock_k8s.CustomObjectsApi.return_value
        api.list_cluster_custom_object.return_value = {
            "items": [_nodeclaim("nc-alias", drifted=True, nodepool="np-alias")]
        }
        core = mock_k8s.CoreV1Api.return_value
        core.list_node.return_value = _node_list(_karpenter_node("n-alias", "v1.34.0", nodepool="np-alias"))

        assert (
            wait_for_karpenter_drift(CLUSTER, REGION, "1.34", nodepools={"np-alias"}, timeout=30, poll_interval=5)
            is False
        )

    @patch("eksupgrade.src.karpenter.time.sleep", return_value=None)
    @patch("eksupgrade.src.karpenter.time.monotonic")
    @patch(_K8S)
    @patch(_LOADING)
    def test_times_out_and_reports_without_forcing(self, mock_loading, mock_k8s, mock_monotonic, mock_sleep):
        """If drift never completes, return False — never hang, never force."""
        mock_monotonic.side_effect = [0, 1, 1000]
        api = mock_k8s.CustomObjectsApi.return_value
        api.list_cluster_custom_object.return_value = {"items": [_nodeclaim("nc-stuck", drifted=True)]}
        core = mock_k8s.CoreV1Api.return_value
        core.list_node.return_value = _node_list(_karpenter_node("n-1", "v1.33.5"))

        assert wait_for_karpenter_drift(CLUSTER, REGION, "1.34", timeout=30, poll_interval=5) is False
        # must never have tried to pause/patch the controller
        mock_k8s.AppsV1Api.return_value.patch_namespaced_deployment.assert_not_called()


_GET_NC = "eksupgrade.src.karpenter.get_ec2nodeclasses"
_WAIT = "eksupgrade.src.karpenter.wait_for_karpenter_drift"


class TestHandleKarpenterDrift:
    @patch(_WAIT, return_value=True)
    @patch(_GET_NC)
    def test_pinned_only_warns_and_does_not_claim_success(self, mock_get_nc, mock_wait):
        """A pinned-only cluster won't auto-drift — must warn AND must not wait or
        report drift as complete (the bug: warn then wait returns True)."""
        mock_get_nc.return_value = [
            {"metadata": {"name": "gpu"}, "spec": {"amiSelectorTerms": [{"id": "ami-old"}]}},
        ]

        with patch("eksupgrade.src.karpenter.echo_warning") as mock_warn:
            result = handle_karpenter_drift(CLUSTER, REGION, "1.34")

        assert mock_warn.called
        warned_text = " ".join(str(c) for c in mock_warn.call_args_list)
        assert "gpu" in warned_text
        # no alias class → no drift will happen → must NOT wait, must NOT claim "settled"
        mock_wait.assert_not_called()
        assert result != "settled"

    @patch(_K8S)
    @patch(_WAIT, return_value=True)
    @patch(_GET_NC)
    def test_alias_nodeclass_waits_and_reports_settled(self, mock_get_nc, mock_wait, mock_k8s):
        mock_get_nc.return_value = [
            {"metadata": {"name": "default"}, "spec": {"amiSelectorTerms": [{"alias": "al2023@latest"}]}},
        ]
        mock_k8s.CustomObjectsApi.return_value.list_cluster_custom_object.return_value = {"items": []}

        result = handle_karpenter_drift(CLUSTER, REGION, "1.34")

        mock_wait.assert_called_once()
        assert result == "settled"

    @patch(_K8S)
    @patch(_WAIT, return_value=False)
    @patch(_GET_NC)
    def test_alias_nodeclass_timeout_reports_timeout(self, mock_get_nc, mock_wait, mock_k8s):
        mock_get_nc.return_value = [
            {"metadata": {"name": "default"}, "spec": {"amiSelectorTerms": [{"alias": "al2023@latest"}]}},
        ]
        mock_k8s.CustomObjectsApi.return_value.list_cluster_custom_object.return_value = {"items": []}

        result = handle_karpenter_drift(CLUSTER, REGION, "1.34")

        assert result == "timeout"

    @patch(_WAIT, return_value=True)
    @patch(_GET_NC, return_value=[])
    def test_no_nodeclasses_skips_wait(self, mock_get_nc, mock_wait):
        result = handle_karpenter_drift(CLUSTER, REGION, "1.34")

        mock_wait.assert_not_called()
        assert result != "settled"

    @patch("eksupgrade.src.karpenter.time.sleep", return_value=None)
    @patch(_K8S)
    @patch(_LOADING)
    def test_mixed_alias_and_pinned_reports_settled(self, mock_loading, mock_k8s, mock_sleep):
        """End-to-end: an alias class whose node reached target + a pinned class
        whose node stays on the old version must report 'settled' (the pinned node
        is EXPECTED to stay and must not hold the wait open forever)."""
        custom = mock_k8s.CustomObjectsApi.return_value

        def _list(group, version, plural, **kwargs):
            if plural == "ec2nodeclasses":
                return {
                    "items": [
                        {"metadata": {"name": "alias-nc"}, "spec": {"amiSelectorTerms": [{"alias": "al2023@latest"}]}},
                        {"metadata": {"name": "pinned-nc"}, "spec": {"amiSelectorTerms": [{"id": "ami-old"}]}},
                    ]
                }
            if plural == "nodepools":
                return {"items": [_nodepool("np-alias", "alias-nc"), _nodepool("np-pinned", "pinned-nc")]}
            if plural == "nodeclaims":
                return {"items": [_nodeclaim("nc-alias", drifted=False), _nodeclaim("nc-pinned", drifted=False)]}
            return {"items": []}

        custom.list_cluster_custom_object.side_effect = _list
        core = mock_k8s.CoreV1Api.return_value
        core.list_node.return_value = _node_list(
            _karpenter_node("n-alias", "v1.34.0", nodepool="np-alias"),
            _karpenter_node("n-pinned", "v1.33.5", nodepool="np-pinned"),  # stays old — expected
        )

        result = handle_karpenter_drift(CLUSTER, REGION, "1.34", timeout=30, poll_interval=5)

        assert result == "settled"
