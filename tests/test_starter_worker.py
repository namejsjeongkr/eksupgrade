"""Test StatsWorker failure handling in parallel mode.

The legacy worker had no exception handling: a failing node group update
killed the thread WITHOUT calling queue.task_done(), so cli.py's
queue.join() blocked forever (with the Cluster Autoscaler still paused).
These tests pin down: task_done is always called, the worker survives to
process further items, and failures are recorded for the caller.
"""

import queue as queue_module
import time
from unittest.mock import MagicMock, patch

import pytest

from eksupgrade.starter import StatsWorker, actual_update

_ACTUAL_UPDATE = "eksupgrade.starter.actual_update"
_UPDATE_NODEGROUP = "eksupgrade.starter.update_nodegroup"


def _drain_queue(q: queue_module.Queue, timeout: float = 5.0) -> bool:
    """Wait (bounded) for all queue items to be marked done; True if drained."""
    deadline = time.monotonic() + timeout
    while q.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.02)
    return q.unfinished_tasks == 0


def _put_selfmanaged(q: queue_module.Queue, ng_name: str) -> None:
    q.put(["my-cluster", ng_name, "1.36", "us-east-1", 2, False, "selfmanaged"])


@patch(_ACTUAL_UPDATE, side_effect=RuntimeError("boom"))
def test_failed_update_still_marks_task_done(mock_update):
    """A worker exception must not leave the queue join hanging forever."""
    q: queue_module.Queue = queue_module.Queue()
    failures: list[str] = []
    worker = StatsWorker(q, 0, failures=failures)
    worker.daemon = True
    worker.start()

    _put_selfmanaged(q, "asg-1")

    assert _drain_queue(q), "task_done was not called for the failed item — queue.join() would hang"


@patch(_ACTUAL_UPDATE, side_effect=RuntimeError("boom"))
def test_failure_is_recorded_for_caller(mock_update):
    """The caller must be able to see WHICH node group failed."""
    q: queue_module.Queue = queue_module.Queue()
    failures: list[str] = []
    worker = StatsWorker(q, 0, failures=failures)
    worker.daemon = True
    worker.start()

    _put_selfmanaged(q, "asg-1")
    _drain_queue(q)

    assert "asg-1" in failures


@patch(_ACTUAL_UPDATE, side_effect=[RuntimeError("boom"), True])
def test_worker_survives_failure_and_processes_next_item(mock_update):
    """The worker thread must survive a failure and keep consuming the queue."""
    q: queue_module.Queue = queue_module.Queue()
    failures: list[str] = []
    worker = StatsWorker(q, 0, failures=failures)
    worker.daemon = True
    worker.start()

    _put_selfmanaged(q, "asg-bad")
    _put_selfmanaged(q, "asg-good")
    assert _drain_queue(q)

    assert failures == ["asg-bad"]
    assert mock_update.call_count == 2


@patch(_UPDATE_NODEGROUP, side_effect=RuntimeError("boom"))
def test_managed_branch_failure_also_marks_task_done(mock_update):
    """The managed-nodegroup branch must have the same failure handling."""
    q: queue_module.Queue = queue_module.Queue()
    failures: list[str] = []
    worker = StatsWorker(q, 0, failures=failures)
    worker.daemon = True
    worker.start()

    q.put(["my-cluster", "mng-1", "1.36", "us-east-1", 2, False, "managed"])
    assert _drain_queue(q)
    assert "mng-1" in failures


@patch("eksupgrade.starter.time.sleep", return_value=None)
@patch("eksupgrade.starter.worker_terminate")
@patch("eksupgrade.starter.find_node")
@patch("eksupgrade.starter.get_num_of_instances", return_value=2)
@patch("eksupgrade.starter.get_outdated_instance_ids", return_value=["i-old"])
@patch("eksupgrade.starter.get_outdated_asg", return_value=False)
@patch("eksupgrade.starter.get_latest_ami", return_value="ami-new")
@patch("eksupgrade.starter.get_ami_name", return_value=("amazon linux 2", "amazon-eks-node-1.36"))
def test_node_vanishing_mid_check_fails_bounded_not_infinite(
    mock_ami_name,
    mock_latest_ami,
    mock_outdated_asg,
    mock_outdated_ids,
    mock_num,
    mock_find_node,
    mock_terminate,
    mock_sleep,
):
    """actual_update's node re-check loop must be bounded.

    The legacy loop only incremented its retry counter when the node WAS
    found; a node that deregistered mid-loop (find_node -> "NAN") froze the
    counter and spun forever. It must instead exhaust max_retry and raise.
    """
    # Found once (passes the outer check), then gone on every re-check.
    mock_find_node.side_effect = ["node-1"] + ["NAN"] * 10

    with pytest.raises(Exception, match="404"):
        actual_update("my-cluster", "asg-1", "1.36", "us-east-1", max_retry=2, forced=False)

    mock_terminate.assert_called_once()


@patch("eksupgrade.starter.time.sleep", return_value=None)
@patch("eksupgrade.starter.worker_terminate")
@patch("eksupgrade.starter.wait_for_statefulset_pods_ready", return_value=True)
@patch("eksupgrade.starter.get_statefulset_pods_on_node", return_value=[])
@patch("eksupgrade.starter.delete_node")
@patch("eksupgrade.starter.drain_nodes")
@patch("eksupgrade.starter.unschedule_old_nodes")
@patch("eksupgrade.starter.find_node", return_value="node-x")
@patch("eksupgrade.starter.wait_for_ready", return_value=True)
@patch("eksupgrade.starter.get_latest_instance", return_value="i-new")
@patch("eksupgrade.starter.add_node")
@patch("eksupgrade.starter.get_num_of_instances", side_effect=[2, 2])
@patch("eksupgrade.starter.get_outdated_instance_ids", return_value=["i-old-1", "i-old-2"])
@patch("eksupgrade.starter.get_outdated_asg", return_value=False)
@patch("eksupgrade.starter.get_latest_ami", return_value="ami-new")
@patch("eksupgrade.starter.get_ami_name", return_value=("amazon linux 2", "amazon-eks-node-1.36"))
def test_surge_adds_a_node_only_when_no_fresh_instance_exists(
    mock_ami_name,
    mock_latest_ami,
    mock_outdated_asg,
    mock_outdated_ids,
    mock_num,
    mock_add_node,
    mock_latest_instance,
    mock_wait_ready,
    mock_find_node,
    mock_unschedule,
    mock_drain,
    mock_delete,
    mock_sts_pods,
    mock_sts_wait,
    mock_terminate,
    mock_sleep,
):
    """Fully-outdated 2-node ASG: iteration 1 has no fresh instance (surge +1
    needed); iteration 2 already has the fresh node from iteration 1, so no
    second add. The legacy abs() formula over-provisioned by adding again."""
    assert actual_update("my-cluster", "asg-1", "1.36", "us-east-1", max_retry=2, forced=False) is True

    assert mock_add_node.call_count == 1
