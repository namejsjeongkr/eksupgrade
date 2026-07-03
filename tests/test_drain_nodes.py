"""Test drain_nodes() eviction behavior.

The legacy implementation evicted only the FIRST pod on a node and then
returned, leaving the remaining pods running while the instance was about to
be terminated. These tests pin down the correct behavior: every (non
daemonset) pod on the node must be evicted/deleted before the function returns.

All kubernetes client calls are patched so no real cluster is required.
"""

from unittest.mock import MagicMock, patch

import pytest

from eksupgrade.src.k8s_client import drain_nodes

CLUSTER = "test-cluster"
REGION = "us-east-1"
NODE = "ip-10-0-1-1.ec2.internal"

_LOADING = "eksupgrade.src.k8s_client.loading_config"
_K8S = "eksupgrade.src.k8s_client.client"
_WATCHER = "eksupgrade.src.k8s_client._wait_for_pod_gone"


def _owner_ref(kind: str) -> MagicMock:
    ref = MagicMock()
    ref.kind = kind
    return ref


def _make_pod(
    name: str,
    namespace: str = "default",
    owner_kind: str | None = "ReplicaSet",
    mirror: bool = False,
) -> MagicMock:
    """Return a mock pod scheduled on NODE.

    owner_kind=None means a standalone (unmanaged) pod; mirror=True marks a
    static/mirror pod via the config.mirror annotation.
    """
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.owner_references = [_owner_ref(owner_kind)] if owner_kind else []
    pod.metadata.annotations = {"kubernetes.io/config.mirror": "x"} if mirror else {}
    pod.spec.node_name = NODE
    return pod


def _pod_list(*pods: MagicMock) -> MagicMock:
    """Wrap pods in a list_pod_for_all_namespaces-style response."""
    response = MagicMock()
    response.items = list(pods)
    return response


class TestDrainAllPods:
    """Every pod on the node must be drained, not just the first."""

    @patch(_WATCHER, return_value=True)
    @patch(_K8S)
    @patch(_LOADING)
    def test_evicts_every_pod_when_not_forced(self, mock_loading, mock_k8s, mock_watcher):
        """forced=False: all pods on the node get an eviction, not just pod #1."""
        core = mock_k8s.CoreV1Api.return_value
        core.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("pod-1"), _make_pod("pod-2"), _make_pod("pod-3")
        )

        drain_nodes(CLUSTER, NODE, forced=False, region=REGION)

        assert core.create_namespaced_pod_eviction.call_count == 3

    @patch(_K8S)
    @patch(_LOADING)
    def test_deletes_every_pod_when_forced(self, mock_loading, mock_k8s):
        """forced=True: all pods on the node get deleted."""
        core = mock_k8s.CoreV1Api.return_value
        core.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("pod-1"), _make_pod("pod-2"), _make_pod("pod-3")
        )

        drain_nodes(CLUSTER, NODE, forced=True, region=REGION)

        assert core.delete_namespaced_pod.call_count == 3

    @patch(_K8S)
    @patch(_LOADING)
    def test_returns_message_when_nothing_to_drain(self, mock_loading, mock_k8s):
        """An empty pod list yields the 'nothing to drain' message and no eviction."""
        core = mock_k8s.CoreV1Api.return_value
        core.list_pod_for_all_namespaces.return_value = _pod_list()

        result = drain_nodes(CLUSTER, NODE, forced=False, region=REGION)

        assert result is not None and "Drain" in result
        core.create_namespaced_pod_eviction.assert_not_called()
        core.delete_namespaced_pod.assert_not_called()


class TestOwnerAwareDrain:
    """drain must skip DaemonSet and mirror pods like `kubectl drain`."""

    @patch(_WATCHER, return_value=True)
    @patch(_K8S)
    @patch(_LOADING)
    def test_daemonset_pods_are_not_evicted(self, mock_loading, mock_k8s, mock_watcher):
        core = mock_k8s.CoreV1Api.return_value
        core.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("ds-pod", owner_kind="DaemonSet"),
            _make_pod("app-pod", owner_kind="ReplicaSet"),
        )

        drain_nodes(CLUSTER, NODE, forced=False, region=REGION)

        # only the app pod is evicted; the daemonset pod is left alone
        assert core.create_namespaced_pod_eviction.call_count == 1
        evicted = core.create_namespaced_pod_eviction.call_args.kwargs["name"]
        assert evicted == "app-pod"

    @patch(_K8S)
    @patch(_LOADING)
    def test_daemonset_pods_not_deleted_even_when_forced(self, mock_loading, mock_k8s):
        core = mock_k8s.CoreV1Api.return_value
        core.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("ds-pod", owner_kind="DaemonSet"),
            _make_pod("app-pod", owner_kind="ReplicaSet"),
        )

        drain_nodes(CLUSTER, NODE, forced=True, region=REGION)

        assert core.delete_namespaced_pod.call_count == 1
        deleted = core.delete_namespaced_pod.call_args.args[0]
        assert deleted == "app-pod"

    @patch(_WATCHER, return_value=True)
    @patch(_K8S)
    @patch(_LOADING)
    def test_mirror_pods_are_skipped(self, mock_loading, mock_k8s, mock_watcher):
        core = mock_k8s.CoreV1Api.return_value
        core.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("static-pod", owner_kind=None, mirror=True),
            _make_pod("app-pod", owner_kind="ReplicaSet"),
        )

        drain_nodes(CLUSTER, NODE, forced=False, region=REGION)

        assert core.create_namespaced_pod_eviction.call_count == 1

    @patch(_WATCHER, return_value=True)
    @patch(_K8S)
    @patch(_LOADING)
    def test_standalone_pod_rejected_when_not_forced(self, mock_loading, mock_k8s, mock_watcher):
        """An unmanaged pod must NOT be silently evicted without force (kubectl semantics)."""
        core = mock_k8s.CoreV1Api.return_value
        core.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("orphan-pod", owner_kind=None),
        )

        with pytest.raises(Exception):
            drain_nodes(CLUSTER, NODE, forced=False, region=REGION)

        core.create_namespaced_pod_eviction.assert_not_called()

    @patch(_K8S)
    @patch(_LOADING)
    def test_standalone_pod_deleted_when_forced(self, mock_loading, mock_k8s):
        core = mock_k8s.CoreV1Api.return_value
        core.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("orphan-pod", owner_kind=None),
        )

        drain_nodes(CLUSTER, NODE, forced=True, region=REGION)

        assert core.delete_namespaced_pod.call_count == 1

    @patch(_WATCHER, return_value=True)
    @patch(_K8S)
    @patch(_LOADING)
    def test_aborts_before_evicting_anything_if_orphan_present(self, mock_loading, mock_k8s, mock_watcher):
        """kubectl-faithful: a node with an unmanaged pod aborts BEFORE any eviction."""
        core = mock_k8s.CoreV1Api.return_value
        core.list_pod_for_all_namespaces.return_value = _pod_list(
            _make_pod("managed-pod", owner_kind="ReplicaSet"),
            _make_pod("orphan-pod", owner_kind=None),
        )

        with pytest.raises(Exception):
            drain_nodes(CLUSTER, NODE, forced=False, region=REGION)

        # the managed pod must NOT have been evicted — node left intact, not half-drained
        core.create_namespaced_pod_eviction.assert_not_called()
