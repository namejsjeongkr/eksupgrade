"""Test _evict_pod PDB (HTTP 429) and already-gone (HTTP 404) handling.

kubectl drain retries evictions that are temporarily blocked by a
PodDisruptionBudget (the eviction API returns 429) instead of failing the
whole drain. It also treats an already-deleted pod (404) as success.
These tests pin that behavior down for _evict_pod.
"""

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.rest import ApiException

from eksupgrade.src.k8s_client import _evict_pod

CLUSTER = "test-cluster"
REGION = "us-east-1"
NODE = "ip-10-0-1-1.ec2.internal"

_WATCHER = "eksupgrade.src.k8s_client.watcher"
_SLEEP = "eksupgrade.src.k8s_client.time.sleep"


def _make_pod(name: str = "app-pod", namespace: str = "default") -> MagicMock:
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    return pod


@patch(_SLEEP)
@patch(_WATCHER, return_value=True)
def test_retries_eviction_blocked_by_pdb(mock_watcher, mock_sleep):
    """A 429 (PDB temporarily blocking) must be retried, not raised."""
    core = MagicMock()
    core.create_namespaced_pod_eviction.side_effect = [
        ApiException(status=429, reason="Too Many Requests"),
        ApiException(status=429, reason="Too Many Requests"),
        None,
    ]

    _evict_pod(CLUSTER, NODE, _make_pod(), core, REGION)

    assert core.create_namespaced_pod_eviction.call_count == 3


@patch(_SLEEP)
@patch(_WATCHER, return_value=True)
def test_pod_already_gone_is_success(mock_watcher, mock_sleep):
    """A 404 means the pod is already deleted — that IS a successful eviction."""
    core = MagicMock()
    core.create_namespaced_pod_eviction.side_effect = ApiException(status=404, reason="Not Found")

    _evict_pod(CLUSTER, NODE, _make_pod(), core, REGION)  # must not raise


@patch(_SLEEP)
@patch(_WATCHER, return_value=False)
def test_reeviction_after_missed_watch_tolerates_404(mock_watcher, mock_sleep):
    """Eviction succeeded but the watcher missed the DELETED event; the retry
    eviction hits 404 because the pod is in fact gone — treat as success."""
    core = MagicMock()
    core.create_namespaced_pod_eviction.side_effect = [
        None,  # first eviction accepted, but watcher returns False (missed event)
        ApiException(status=404, reason="Not Found"),  # pod is actually gone
    ]

    _evict_pod(CLUSTER, NODE, _make_pod(), core, REGION)  # must not raise


@patch(_SLEEP)
@patch(_WATCHER, return_value=True)
def test_pdb_blocking_past_deadline_raises(mock_watcher, mock_sleep):
    """A PDB that never unblocks must eventually fail (bounded retry), not loop forever."""
    core = MagicMock()
    core.create_namespaced_pod_eviction.side_effect = ApiException(status=429, reason="Too Many Requests")

    with pytest.raises(Exception):
        _evict_pod(CLUSTER, NODE, _make_pod(), core, REGION, pdb_timeout=0)


@patch(_SLEEP)
@patch(_WATCHER, return_value=True)
def test_other_api_errors_still_raise(mock_watcher, mock_sleep):
    """Non-429/404 API errors (e.g. 500) must propagate immediately."""
    core = MagicMock()
    core.create_namespaced_pod_eviction.side_effect = ApiException(status=500, reason="Internal Server Error")

    with pytest.raises(ApiException):
        _evict_pod(CLUSTER, NODE, _make_pod(), core, REGION)
