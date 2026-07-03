"""Test the functionality of the CLI module."""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from eksupgrade.cli import app
from eksupgrade.src.preflight import PreflightResult

runner = CliRunner()


def test_entry_version_arg() -> None:
    """Test the entry method with version argument."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "eksupgrade version" in result.stdout


def test_entry_no_arg() -> None:
    """Test the entry method with no arguments."""
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    # Newer Click routes usage errors to stderr; result.output captures both streams.
    assert "OPTIONS" in result.output


def test_preflight_runs_check_and_exits_without_upgrade() -> None:
    fake_cluster = MagicMock()
    with (
        patch("eksupgrade.cli.Cluster.get", return_value=fake_cluster) as mock_get,
        patch(
            "eksupgrade.cli.run_preflight", return_value=PreflightResult(findings=[], check_failed=False)
        ) as mock_pre,
    ):
        result = runner.invoke(
            app, ["upgrade", "my-cluster", "1.33", "ap-northeast-2", "--preflight", "--no-interactive"]
        )
    mock_get.assert_called_once()
    mock_pre.assert_called_once()
    # The cluster's mutating methods must never be called in preflight mode.
    fake_cluster.update_cluster.assert_not_called()
    fake_cluster.upgrade_addons.assert_not_called()
    fake_cluster.upgrade_nodegroups.assert_not_called()
    assert result.exit_code == 0


def test_preflight_force_still_exits_without_upgrade() -> None:
    # --preflight --force must NOT reach any mutation: the Exit dominates the
    # force flag (force is only read at the confirm/drain steps, after preflight).
    fake_cluster = MagicMock()
    with (
        patch("eksupgrade.cli.Cluster.get", return_value=fake_cluster),
        patch("eksupgrade.cli.run_preflight", return_value=PreflightResult(findings=[], check_failed=False)),
    ):
        result = runner.invoke(app, ["upgrade", "my-cluster", "1.33", "ap-northeast-2", "--preflight", "--force"])
    fake_cluster.update_cluster.assert_not_called()
    fake_cluster.upgrade_addons.assert_not_called()
    fake_cluster.upgrade_nodegroups.assert_not_called()
    assert result.exit_code == 0


def test_preflight_blocking_exits_one() -> None:
    # A blocking finding must bubble through the CLI as exit code 1.
    from eksupgrade.src.preflight import PreflightFinding

    fake_cluster = MagicMock()
    blocking = PreflightResult(
        findings=[PreflightFinding(area="Control Plane", item="version", severity="blocking", detail="x")],
        check_failed=False,
    )
    with (
        patch("eksupgrade.cli.Cluster.get", return_value=fake_cluster),
        patch("eksupgrade.cli.run_preflight", return_value=blocking),
    ):
        result = runner.invoke(
            app, ["upgrade", "my-cluster", "1.33", "ap-northeast-2", "--preflight", "--no-interactive"]
        )
    fake_cluster.update_cluster.assert_not_called()
    assert result.exit_code == 1


def test_preflight_crash_exits_nonzero() -> None:
    fake_cluster = MagicMock()
    with (
        patch("eksupgrade.cli.Cluster.get", return_value=fake_cluster),
        patch("eksupgrade.cli.run_preflight", side_effect=RuntimeError("kube down")),
    ):
        result = runner.invoke(
            app, ["upgrade", "my-cluster", "1.33", "ap-northeast-2", "--preflight", "--no-interactive"]
        )
    fake_cluster.update_cluster.assert_not_called()
    assert result.exit_code == 2


def _fake_upgradeable_cluster() -> MagicMock:
    """Return a cluster mock that walks the full upgrade path with no nodegroups."""
    fake_cluster = MagicMock()
    fake_cluster.version = "1.34"
    fake_cluster.target_version = "1.35"
    fake_cluster.available = True
    fake_cluster.active = True
    fake_cluster.status = "ACTIVE"
    fake_cluster.upgradable_managed_nodegroups = []
    fake_cluster.nodegroups = []
    fake_cluster.nodegroup_names = []
    fake_cluster.asg_names = []
    return fake_cluster


def test_cluster_autoscaler_resumed_on_keyboard_interrupt():
    """Ctrl+C mid-upgrade must NOT leave the Cluster Autoscaler paused at 0 replicas.

    KeyboardInterrupt is a BaseException, so an `except Exception` recovery
    block never sees it — the resume must live in a finally.
    """
    fake_cluster = _fake_upgradeable_cluster()
    fake_cluster.upgrade_nodegroups.side_effect = KeyboardInterrupt
    with (
        patch("eksupgrade.cli.Cluster.get", return_value=fake_cluster),
        patch(
            "eksupgrade.cli.is_cluster_auto_scaler_present",
            return_value=(True, 2, "cluster-autoscaler", "kube-system"),
        ),
        patch("eksupgrade.cli.is_karpenter_present", return_value=(False, 0, "")),
        patch("eksupgrade.cli.cluster_auto_enable_disable") as mock_toggle,
    ):
        runner.invoke(app, ["upgrade", "c", "1.35", "ap-northeast-2", "--no-interactive"])

    operations = [call.kwargs.get("operation") for call in mock_toggle.call_args_list]
    assert "pause" in operations
    assert "start" in operations, "Cluster Autoscaler was left paused after KeyboardInterrupt"


def test_cluster_autoscaler_resumed_on_error():
    """A plain exception mid-upgrade must also resume the Cluster Autoscaler (regression guard)."""
    fake_cluster = _fake_upgradeable_cluster()
    fake_cluster.upgrade_nodegroups.side_effect = RuntimeError("boom")
    with (
        patch("eksupgrade.cli.Cluster.get", return_value=fake_cluster),
        patch(
            "eksupgrade.cli.is_cluster_auto_scaler_present",
            return_value=(True, 2, "cluster-autoscaler", "kube-system"),
        ),
        patch("eksupgrade.cli.is_karpenter_present", return_value=(False, 0, "")),
        patch("eksupgrade.cli.cluster_auto_enable_disable") as mock_toggle,
    ):
        runner.invoke(app, ["upgrade", "c", "1.35", "ap-northeast-2", "--no-interactive"])

    operations = [call.kwargs.get("operation") for call in mock_toggle.call_args_list]
    assert "start" in operations


def test_karpenter_scaled_to_zero_skips_drift_wait():
    """Karpenter deployed but scaled to 0 replicas cannot drift anything —
    waiting 30 minutes for drift is pure waste. Warn and skip the wait."""
    fake_cluster = _fake_upgradeable_cluster()
    with (
        patch("eksupgrade.cli.Cluster.get", return_value=fake_cluster),
        patch("eksupgrade.cli.is_cluster_auto_scaler_present", return_value=(False, 0, "", "")),
        patch("eksupgrade.cli.is_karpenter_present", return_value=(True, 0, "karpenter")),
        patch("eksupgrade.cli.handle_karpenter_drift") as mock_drift,
    ):
        result = runner.invoke(app, ["upgrade", "c", "1.35", "ap-northeast-2", "--no-interactive"])

    mock_drift.assert_not_called()
    assert "0 replicas" in result.output


def test_parallel_worker_failure_fails_the_upgrade():
    """A failed parallel node group update must NOT let the CLI report overall success."""
    fake_cluster = _fake_upgradeable_cluster()
    fake_cluster.asg_names = ["asg-1"]
    with (
        patch("eksupgrade.cli.Cluster.get", return_value=fake_cluster),
        patch(
            "eksupgrade.cli.is_cluster_auto_scaler_present",
            return_value=(True, 2, "cluster-autoscaler", "kube-system"),
        ),
        patch("eksupgrade.cli.is_karpenter_present", return_value=(False, 0, "")),
        patch("eksupgrade.cli.cluster_auto_enable_disable") as mock_toggle,
        patch("eksupgrade.starter.actual_update", side_effect=RuntimeError("boom")),
    ):
        result = runner.invoke(app, ["upgrade", "c", "1.35", "ap-northeast-2", "--no-interactive", "--parallel"])

    assert "UPDATED TO" not in result.output, "CLI reported success despite a failed node group"
    # And the Cluster Autoscaler must still be resumed on the failure path.
    operations = [call.kwargs.get("operation") for call in mock_toggle.call_args_list]
    assert "start" in operations


def test_timing_summary_printed_on_success():
    fake_cluster = MagicMock()
    fake_cluster.version = "1.34"
    fake_cluster.target_version = "1.35"
    fake_cluster.available = True
    fake_cluster.active = True
    fake_cluster.status = "ACTIVE"
    fake_cluster.upgradable_managed_nodegroups = []
    fake_cluster.nodegroups = []
    fake_cluster.nodegroup_names = []
    fake_cluster.asg_names = []
    with (
        patch("eksupgrade.cli.Cluster.get", return_value=fake_cluster),
        patch("eksupgrade.cli.is_cluster_auto_scaler_present", return_value=(False, 0, "", "")),
        patch("eksupgrade.cli.is_karpenter_present", return_value=(False, 0, "")),
        patch("eksupgrade.cli.handle_karpenter_drift", return_value="no_drift"),
        patch("eksupgrade.cli.console.print") as mock_print,
    ):
        runner.invoke(app, ["upgrade", "c", "1.35", "ap-northeast-2", "--no-interactive"])
    # timing summary Table printed via console.print at least once
    assert mock_print.called
    from rich.table import Table

    assert any(isinstance(call.args[0], Table) for call in mock_print.call_args_list if call.args)


def _fake_rollback_cluster() -> MagicMock:
    """Cluster on 1.36 that rolls back to 1.35, with one managed NG to roll."""
    fake_cluster = MagicMock()
    fake_cluster.version = "1.36"
    fake_cluster.active = True
    fake_cluster.status = "ACTIVE"
    fake_cluster.nodegroups = []
    fake_cluster.nodegroup_names = []
    fake_cluster.asg_names = []
    fake_cluster.upgradable_managed_nodegroups = []
    return fake_cluster


def _no_blocking():
    from eksupgrade.src.rollback import RollbackReadiness

    return RollbackReadiness(findings=[])


def _blocking():
    from eksupgrade.src.rollback import RollbackReadiness

    return RollbackReadiness(findings=[{"name": "kubelet skew", "status": "ERROR", "reason": "x"}])


def test_rollback_blocking_insight_exits_one_without_force():
    fake_cluster = _fake_rollback_cluster()
    with (
        patch("eksupgrade.cli.Cluster.get", return_value=fake_cluster),
        patch("eksupgrade.cli.get_rollback_readiness", return_value=_blocking()),
        patch("eksupgrade.cli.incompatible_addons", return_value=[]),
    ):
        result = runner.invoke(app, ["rollback", "c", "ap-northeast-2", "--no-interactive"])

    assert result.exit_code == 1
    fake_cluster.rollback_cluster.assert_not_called()


def test_rollback_force_bypasses_blocking_insights():
    fake_cluster = _fake_rollback_cluster()
    with (
        patch("eksupgrade.cli.Cluster.get", return_value=fake_cluster),
        patch("eksupgrade.cli.get_rollback_readiness", return_value=_blocking()),
        patch("eksupgrade.cli.incompatible_addons", return_value=[]),
        patch("eksupgrade.cli.is_cluster_auto_scaler_present", return_value=(False, 0, "", "")),
        patch("eksupgrade.cli.is_karpenter_present", return_value=(False, 0, "")),
    ):
        result = runner.invoke(app, ["rollback", "c", "ap-northeast-2", "--no-interactive", "--force"])

    assert result.exit_code == 0
    call = fake_cluster.rollback_cluster.call_args
    assert call.kwargs["force"] is True


def test_rollback_targets_previous_minor_and_rolls_nodes_before_control_plane():
    """AWS docs: worker nodes must be rolled back BEFORE the control plane
    (kubelet must never be newer than the apiserver), and the target is N-1."""
    order: list[str] = []
    fake_cluster = _fake_rollback_cluster()
    ng = MagicMock()
    ng.name = "ng-1"
    ng.update.side_effect = lambda **kwargs: order.append("nodegroup")
    fake_cluster.upgradable_managed_nodegroups = [ng]
    fake_cluster.rollback_cluster.side_effect = lambda **kwargs: order.append("control-plane")

    with (
        patch("eksupgrade.cli.Cluster.get", return_value=fake_cluster),
        patch("eksupgrade.cli.get_rollback_readiness", return_value=_no_blocking()),
        patch("eksupgrade.cli.incompatible_addons", return_value=[]),
        patch("eksupgrade.cli.is_cluster_auto_scaler_present", return_value=(False, 0, "", "")),
        patch("eksupgrade.cli.is_karpenter_present", return_value=(False, 0, "")),
    ):
        result = runner.invoke(app, ["rollback", "c", "ap-northeast-2", "--no-interactive"])

    assert result.exit_code == 0
    assert fake_cluster.target_version == "1.35"
    assert order == ["nodegroup", "control-plane"]


def test_rollback_karpenter_drift_waits_on_previous_version():
    fake_cluster = _fake_rollback_cluster()
    with (
        patch("eksupgrade.cli.Cluster.get", return_value=fake_cluster),
        patch("eksupgrade.cli.get_rollback_readiness", return_value=_no_blocking()),
        patch("eksupgrade.cli.incompatible_addons", return_value=[]),
        patch("eksupgrade.cli.is_cluster_auto_scaler_present", return_value=(False, 0, "", "")),
        patch("eksupgrade.cli.is_karpenter_present", return_value=(True, 1, "karpenter")),
        patch("eksupgrade.cli.handle_karpenter_drift", return_value="settled") as mock_drift,
    ):
        result = runner.invoke(app, ["rollback", "c", "ap-northeast-2", "--no-interactive"])

    assert result.exit_code == 0
    assert mock_drift.call_args.kwargs["target_version"] == "1.35"


def test_rollback_resumes_cluster_autoscaler_on_failure():
    fake_cluster = _fake_rollback_cluster()
    fake_cluster.rollback_cluster.side_effect = RuntimeError("boom")
    with (
        patch("eksupgrade.cli.Cluster.get", return_value=fake_cluster),
        patch("eksupgrade.cli.get_rollback_readiness", return_value=_no_blocking()),
        patch("eksupgrade.cli.incompatible_addons", return_value=[]),
        patch(
            "eksupgrade.cli.is_cluster_auto_scaler_present",
            return_value=(True, 2, "cluster-autoscaler", "kube-system"),
        ),
        patch("eksupgrade.cli.is_karpenter_present", return_value=(False, 0, "")),
        patch("eksupgrade.cli.cluster_auto_enable_disable") as mock_toggle,
    ):
        result = runner.invoke(app, ["rollback", "c", "ap-northeast-2", "--no-interactive"])

    assert result.exit_code != 0
    operations = [call.kwargs.get("operation") for call in mock_toggle.call_args_list]
    assert "start" in operations
