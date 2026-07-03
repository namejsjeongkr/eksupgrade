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

from eksupgrade.starter import StatsWorker

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
