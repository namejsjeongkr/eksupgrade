"""Test the preflight read-only check module."""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from eksupgrade.src.preflight import (
    PreflightFinding,
    PreflightResult,
    _check_addons,
    _check_control_plane,
    _check_karpenter,
    _check_managed_nodegroups,
    _check_pod_disruption_budgets,
    run_preflight,
)


def _finding(severity: str) -> PreflightFinding:
    return PreflightFinding(area="Control Plane", item="version", severity=severity, detail="x")


def test_exit_code_pass_when_all_pass() -> None:
    result = PreflightResult(findings=[_finding("pass")], check_failed=False)
    assert result.exit_code() == 0


def test_exit_code_pass_when_only_warnings() -> None:
    result = PreflightResult(findings=[_finding("warning")], check_failed=False)
    assert result.warning_count == 1
    assert result.exit_code() == 0


def test_exit_code_one_when_blocking() -> None:
    result = PreflightResult(findings=[_finding("pass"), _finding("blocking")], check_failed=False)
    assert result.blocking_count == 1
    assert result.exit_code() == 1


def test_exit_code_two_when_check_failed() -> None:
    # check_failed overrides everything, even if no blocking findings.
    result = PreflightResult(findings=[_finding("pass")], check_failed=True)
    assert result.exit_code() == 2


def test_exit_code_two_overrides_blocking() -> None:
    # check_failed (exit 2) must win even when blocking findings exist.
    result = PreflightResult(findings=[_finding("blocking")], check_failed=True)
    assert result.exit_code() == 2


def test_invalid_severity_rejected() -> None:
    with pytest.raises(ValueError):
        PreflightFinding(area="x", item="y", severity="bloking", detail="z")


def _cluster(version="1.32", target_version="1.33", status="ACTIVE"):
    c = MagicMock()
    c.version = version
    c.target_version = target_version
    c.status = status
    c.active = status == "ACTIVE"
    c.updating = status == "UPDATING"
    return c


def test_control_plane_pass_single_minor_active() -> None:
    findings = _check_control_plane(_cluster("1.32", "1.33", "ACTIVE"))
    by_item = {f.item: f.severity for f in findings}
    assert by_item["status"] == "pass"
    assert by_item["version"] == "pass"


def test_control_plane_blocking_when_updating() -> None:
    findings = _check_control_plane(_cluster("1.32", "1.33", "UPDATING"))
    assert any(f.severity == "blocking" and "UPDATING" in f.detail for f in findings)


def test_control_plane_blocking_on_multi_minor() -> None:
    findings = _check_control_plane(_cluster("1.32", "1.34", "ACTIVE"))
    assert any(f.severity == "blocking" and "minor" in f.detail.lower() for f in findings)


def test_control_plane_warns_when_already_target() -> None:
    findings = _check_control_plane(_cluster("1.33", "1.33", "ACTIVE"))
    assert any(f.severity == "warning" for f in findings)
    assert not any(f.severity == "blocking" for f in findings)


def test_control_plane_blocking_on_downgrade() -> None:
    findings = _check_control_plane(_cluster("1.33", "1.31", "ACTIVE"))
    assert any(f.severity == "blocking" and "downgrade" in f.detail.lower() for f in findings)


def _addon(name, version, target_version, available_versions):
    a = MagicMock()
    a.name = name
    a.version = version
    a.target_version = target_version
    a.available_versions = available_versions
    return a


def test_addons_pass_when_compatible_version_exists() -> None:
    cluster = MagicMock()
    cluster.addons = [_addon("coredns", "v1.11.4", "v1.12.4", ["v1.12.4", "v1.11.4"])]
    findings = _check_addons(cluster)
    assert any(f.item == "coredns" and f.severity == "pass" for f in findings)


def test_addons_blocking_when_no_compatible_version() -> None:
    cluster = MagicMock()
    cluster.addons = [_addon("coredns", "v1.11.4", "", [])]
    findings = _check_addons(cluster)
    assert any(f.item == "coredns" and f.severity == "blocking" for f in findings)


def test_addons_warning_on_lookup_failure() -> None:
    # available_versions raising simulates a describe_addon_versions failure.
    bad = MagicMock()
    bad.name = "vpc-cni"
    type(bad).available_versions = PropertyMock(side_effect=RuntimeError("boom"))
    cluster = MagicMock()
    cluster.addons = [bad]
    findings = _check_addons(cluster)
    assert any(f.item == "vpc-cni" and f.severity == "warning" for f in findings)


def _ng(name, ami_type, version="1.32"):
    n = MagicMock()
    n.name = name
    n.ami_type = ami_type
    n.version = version
    return n


def test_managed_ng_pass_non_custom() -> None:
    # AL2023 (not AL2 — AL2 AMIs end at 1.32 and are now blocking past that).
    cluster = MagicMock()
    cluster.version = "1.32"
    cluster.target_version = "1.33"
    cluster.nodegroups = [_ng("ng-al2023", "AL2023_x86_64_STANDARD")]
    findings = _check_managed_nodegroups(cluster, region="ap-northeast-2")
    assert any(f.item == "ng-al2023" and f.severity == "pass" for f in findings)


def test_managed_ng_custom_pass_when_ami_resolves() -> None:
    cluster = MagicMock()
    cluster.version = "1.32"
    cluster.target_version = "1.33"
    cluster.nodegroups = [_ng("ng-br", "CUSTOM")]
    with (
        patch(
            "eksupgrade.src.preflight._custom_ng_current_image",
            return_value="amazon/bottlerocket-aws-k8s-1.32-x86_64-v1.32.0",
        ),
        patch("eksupgrade.src.preflight.get_latest_ami", return_value="ami-0abc") as mock_ami,
    ):
        findings = _check_managed_nodegroups(cluster, region="ap-northeast-2")
    assert any(f.item == "ng-br" and f.severity == "pass" and "ami-0abc" in f.detail for f in findings)
    # Argument contract must mirror the runtime resolver: the ACTUAL image hint.
    mock_ami.assert_called_once_with(
        "1.33",
        "amazon/bottlerocket-aws-k8s-1.32-x86_64-v1.32.0",
        "amazon/bottlerocket-aws-k8s-1.32-x86_64-v1.32.0",
        "ap-northeast-2",
    )


def test_managed_ng_custom_blocking_when_ami_resolve_fails() -> None:
    cluster = MagicMock()
    cluster.version = "1.32"
    cluster.target_version = "1.33"
    cluster.nodegroups = [_ng("ng-br", "CUSTOM")]
    with (
        patch(
            "eksupgrade.src.preflight._custom_ng_current_image",
            return_value="amazon/bottlerocket-aws-k8s-1.32-x86_64-v1.32.0",
        ),
        patch("eksupgrade.src.preflight.get_latest_ami", side_effect=RuntimeError("no ami")),
    ):
        findings = _check_managed_nodegroups(cluster, region="ap-northeast-2")
    assert any(f.item == "ng-br" and f.severity == "blocking" for f in findings)


def test_managed_ng_custom_ami_resolves_via_real_ssm_path() -> None:
    # Mock at the boto/ssm layer (not get_latest_ami) so the argument mapping is
    # actually exercised: the bottlerocket branch builds an SSM path from instance_type.
    cluster = MagicMock()
    cluster.version = "1.32"
    cluster.target_version = "1.33"
    cluster.nodegroups = [_ng("ng-br", "CUSTOM")]

    fake_ssm = MagicMock()
    fake_ssm.get_parameters.return_value = {"Parameters": [{"Value": "ami-real"}]}
    fake_ec2 = MagicMock()

    def _client(service, region_name=None):
        return fake_ssm if service == "ssm" else fake_ec2

    with (
        patch(
            "eksupgrade.src.preflight._custom_ng_current_image",
            return_value="amazon/bottlerocket-aws-k8s-1.32-x86_64-v1.32.0",
        ),
        patch("eksupgrade.src.latest_ami.boto3.client", side_effect=_client),
    ):
        findings = _check_managed_nodegroups(cluster, region="ap-northeast-2")
    assert any(f.item == "ng-br" and f.severity == "pass" and "ami-real" in f.detail for f in findings)
    called_names = fake_ssm.get_parameters.call_args.kwargs.get("Names") or fake_ssm.get_parameters.call_args.args[0]
    assert any("bottlerocket/aws-k8s-1.33" in n for n in called_names)


def test_managed_ng_custom_blocking_when_ami_unresolved() -> None:
    cluster = MagicMock()
    cluster.version = "1.32"
    cluster.target_version = "1.33"
    cluster.nodegroups = [_ng("ng-br", "CUSTOM")]
    with (
        patch(
            "eksupgrade.src.preflight._custom_ng_current_image",
            return_value="amazon/bottlerocket-aws-k8s-1.32-x86_64-v1.32.0",
        ),
        patch("eksupgrade.src.preflight.get_latest_ami", return_value="NAN"),
    ):
        findings = _check_managed_nodegroups(cluster, region="ap-northeast-2")
    assert any(f.item == "ng-br" and f.severity == "blocking" for f in findings)


def test_karpenter_skip_when_no_crd() -> None:
    cluster = MagicMock()
    cluster.name = "c"
    cluster.region = "ap-northeast-2"
    with patch("eksupgrade.src.preflight.get_ec2nodeclasses", side_effect=Exception("not found")):
        findings = _check_karpenter(cluster, region="ap-northeast-2")
    assert len(findings) == 1
    assert findings[0].severity == "pass"
    assert "not detected" in findings[0].detail


def test_karpenter_pass_with_alias_nodeclass() -> None:
    cluster = MagicMock()
    nc = {"metadata": {"name": "default"}, "spec": {"amiSelectorTerms": [{"alias": "bottlerocket@latest"}]}}
    with (
        patch("eksupgrade.src.preflight.get_ec2nodeclasses", return_value=[nc]),
        patch("eksupgrade.src.preflight.loading_config"),
        patch("eksupgrade.src.preflight._list_nodepools", return_value=[{"metadata": {"name": "np"}}]),
        patch("eksupgrade.src.preflight._list_nodeclaims", return_value=[]),
    ):
        findings = _check_karpenter(cluster, region="ap-northeast-2")
    assert any("alias" in f.detail for f in findings)
    assert not any(f.severity == "blocking" for f in findings)


def test_karpenter_warns_on_orphaned_nodeclaims() -> None:
    cluster = MagicMock()
    cluster.name = "c"
    with (
        patch("eksupgrade.src.preflight.get_ec2nodeclasses", return_value=[]),
        patch("eksupgrade.src.preflight.loading_config"),
        patch("eksupgrade.src.preflight._list_nodepools", return_value=[]),
        patch("eksupgrade.src.preflight._list_nodeclaims", return_value=[{"metadata": {"name": "nc-1"}}]),
    ):
        findings = _check_karpenter(cluster, region="ap-northeast-2")
    assert any(f.severity == "warning" and "torn down" in f.detail.lower() for f in findings)


def test_karpenter_warns_on_pinned_nodeclass() -> None:
    cluster = MagicMock()
    cluster.name = "c"
    nc = {"metadata": {"name": "custom"}, "spec": {"amiSelectorTerms": [{"id": "ami-abc123"}]}}
    with (
        patch("eksupgrade.src.preflight.get_ec2nodeclasses", return_value=[nc]),
        patch("eksupgrade.src.preflight.loading_config"),
        patch("eksupgrade.src.preflight._list_nodepools", return_value=[{"metadata": {"name": "np"}}]),
        patch("eksupgrade.src.preflight._list_nodeclaims", return_value=[]),
    ):
        findings = _check_karpenter(cluster, region="ap-northeast-2")
    assert any(f.item == "custom" and f.severity == "warning" and "pinned" in f.detail for f in findings)


def test_karpenter_warns_when_nodepool_listing_fails() -> None:
    cluster = MagicMock()
    cluster.name = "c"
    nc = {"metadata": {"name": "default"}, "spec": {"amiSelectorTerms": [{"alias": "bottlerocket@latest"}]}}
    with (
        patch("eksupgrade.src.preflight.get_ec2nodeclasses", return_value=[nc]),
        patch("eksupgrade.src.preflight.loading_config"),
        patch("eksupgrade.src.preflight._list_nodepools", side_effect=RuntimeError("api down")),
    ):
        findings = _check_karpenter(cluster, region="ap-northeast-2")
    assert any(f.severity == "warning" and "could not list" in f.detail.lower() for f in findings)
    assert not any(f.severity == "blocking" for f in findings)


def test_run_preflight_aggregates_and_returns_result() -> None:
    cluster = _cluster("1.32", "1.33", "ACTIVE")
    cluster.name = "c"
    cluster.region = "ap-northeast-2"
    cluster.addons = [_addon("coredns", "v1.11.4", "v1.12.4", ["v1.12.4"])]
    cluster.nodegroups = [_ng("ng-al2023", "AL2023_x86_64_STANDARD")]
    with (
        patch("eksupgrade.src.preflight.get_ec2nodeclasses", side_effect=Exception("none")),
        patch("eksupgrade.src.preflight.loading_config", side_effect=Exception("no cluster")),
    ):
        result = run_preflight(cluster, region="ap-northeast-2")
    assert isinstance(result, PreflightResult)
    assert result.blocking_count == 0
    assert result.exit_code() == 0
    areas = {f.area for f in result.findings}
    assert {"Control Plane", "Addons", "Managed NodeGroups", "Karpenter"} <= areas


def test_run_preflight_blocking_bubbles_to_exit_code() -> None:
    cluster = _cluster("1.32", "1.34", "ACTIVE")  # multi-minor => blocking
    cluster.name = "c"
    cluster.region = "ap-northeast-2"
    cluster.addons = []
    cluster.nodegroups = []
    with (
        patch("eksupgrade.src.preflight.get_ec2nodeclasses", side_effect=Exception("none")),
        patch("eksupgrade.src.preflight.loading_config", side_effect=Exception("no cluster")),
    ):
        result = run_preflight(cluster, region="ap-northeast-2")
    assert result.blocking_count >= 1
    assert result.exit_code() == 1


def _workload(namespace, name, replicas, template_labels):
    w = MagicMock()
    w.metadata.namespace = namespace
    w.metadata.name = name
    w.spec.replicas = replicas
    w.spec.template.metadata.labels = template_labels
    return w


def _pdb(namespace, match_labels, match_expressions=None):
    p = MagicMock()
    p.metadata.namespace = namespace
    p.spec.selector.match_labels = match_labels
    p.spec.selector.match_expressions = match_expressions
    return p


def _patch_pdb_listers(deployments, statefulsets, pdbs):
    deploy_resp = MagicMock()
    deploy_resp.items = deployments
    sts_resp = MagicMock()
    sts_resp.items = statefulsets
    pdb_resp = MagicMock()
    pdb_resp.items = pdbs

    apps = MagicMock()
    apps.list_deployment_for_all_namespaces.return_value = deploy_resp
    apps.list_stateful_set_for_all_namespaces.return_value = sts_resp
    policy = MagicMock()
    policy.list_pod_disruption_budget_for_all_namespaces.return_value = pdb_resp
    return apps, policy


def test_pdb_warns_when_multireplica_uncovered() -> None:
    cluster = MagicMock()
    cluster.name = "c"
    apps, policy = _patch_pdb_listers(
        deployments=[_workload("app", "web", 3, {"app": "web"})], statefulsets=[], pdbs=[]
    )
    with (
        patch("eksupgrade.src.preflight.loading_config"),
        patch("eksupgrade.src.preflight.k8s_client.AppsV1Api", return_value=apps),
        patch("eksupgrade.src.preflight.k8s_client.PolicyV1Api", return_value=policy),
    ):
        findings = _check_pod_disruption_budgets(cluster, region="ap-northeast-2")
    assert any(f.item == "app/web" and f.severity == "warning" for f in findings)


def test_pdb_no_warning_when_covered() -> None:
    cluster = MagicMock()
    cluster.name = "c"
    apps, policy = _patch_pdb_listers(
        deployments=[_workload("app", "web", 3, {"app": "web", "tier": "frontend"})],
        statefulsets=[],
        pdbs=[_pdb("app", {"app": "web"})],
    )
    with (
        patch("eksupgrade.src.preflight.loading_config"),
        patch("eksupgrade.src.preflight.k8s_client.AppsV1Api", return_value=apps),
        patch("eksupgrade.src.preflight.k8s_client.PolicyV1Api", return_value=policy),
    ):
        findings = _check_pod_disruption_budgets(cluster, region="ap-northeast-2")
    assert not any(f.severity == "warning" for f in findings)


def test_pdb_skips_single_replica() -> None:
    cluster = MagicMock()
    cluster.name = "c"
    apps, policy = _patch_pdb_listers(
        deployments=[
            _workload("app", "solo", 1, {"app": "solo"}),
            _workload("app", "notset", None, {"app": "notset"}),  # replicas=None -> treated as 0
        ],
        statefulsets=[],
        pdbs=[],
    )
    with (
        patch("eksupgrade.src.preflight.loading_config"),
        patch("eksupgrade.src.preflight.k8s_client.AppsV1Api", return_value=apps),
        patch("eksupgrade.src.preflight.k8s_client.PolicyV1Api", return_value=policy),
    ):
        findings = _check_pod_disruption_budgets(cluster, region="ap-northeast-2")
    assert not any(f.item == "app/solo" for f in findings)
    assert not any(f.item == "app/notset" for f in findings)


def test_pdb_wrong_namespace_does_not_cover() -> None:
    cluster = MagicMock()
    cluster.name = "c"
    apps, policy = _patch_pdb_listers(
        deployments=[_workload("app", "web", 2, {"app": "web"})],
        statefulsets=[],
        pdbs=[_pdb("other", {"app": "web"})],
    )
    with (
        patch("eksupgrade.src.preflight.loading_config"),
        patch("eksupgrade.src.preflight.k8s_client.AppsV1Api", return_value=apps),
        patch("eksupgrade.src.preflight.k8s_client.PolicyV1Api", return_value=policy),
    ):
        findings = _check_pod_disruption_budgets(cluster, region="ap-northeast-2")
    assert any(f.item == "app/web" and f.severity == "warning" for f in findings)


def test_pdb_statefulset_covered_and_uncovered() -> None:
    cluster = MagicMock()
    cluster.name = "c"
    apps, policy = _patch_pdb_listers(
        deployments=[],
        statefulsets=[
            _workload("db", "covered", 3, {"app": "covered"}),
            _workload("db", "bare", 2, {"app": "bare"}),
        ],
        pdbs=[_pdb("db", {"app": "covered"})],
    )
    with (
        patch("eksupgrade.src.preflight.loading_config"),
        patch("eksupgrade.src.preflight.k8s_client.AppsV1Api", return_value=apps),
        patch("eksupgrade.src.preflight.k8s_client.PolicyV1Api", return_value=policy),
    ):
        findings = _check_pod_disruption_budgets(cluster, region="ap-northeast-2")
    items = {f.item: f.severity for f in findings if f.severity == "warning"}
    assert items.get("db/bare") == "warning"
    assert "db/covered" not in items


def test_pdb_warns_on_lookup_failure() -> None:
    cluster = MagicMock()
    cluster.name = "c"
    with patch("eksupgrade.src.preflight.loading_config", side_effect=RuntimeError("api down")):
        findings = _check_pod_disruption_budgets(cluster, region="ap-northeast-2")
    assert any(f.severity == "warning" and "could not" in f.detail.lower() for f in findings)
    assert not any(f.severity == "blocking" for f in findings)


class TestAl2NodegroupEol:
    """AL2 managed node groups cannot be upgraded to 1.33+ (no AL2 AMIs exist);
    preflight must block with a migration hint instead of failing mid-upgrade."""

    def _cluster_with_ng(self, ami_type: str, target: str):
        from unittest.mock import MagicMock

        ng = MagicMock()
        ng.name = "ng-1"
        ng.ami_type = ami_type
        cluster = MagicMock()
        cluster.nodegroups = [ng]
        cluster.target_version = target
        return cluster

    def test_al2_nodegroup_targeting_1_33_is_blocking(self):
        from eksupgrade.src.preflight import _check_managed_nodegroups

        cluster = self._cluster_with_ng("AL2_x86_64", "1.33")
        findings = _check_managed_nodegroups(cluster, "ap-northeast-2")

        assert any(f.severity == "blocking" and "AL2023" in f.detail for f in findings)

    def test_al2_nodegroup_targeting_1_32_passes(self):
        from eksupgrade.src.preflight import _check_managed_nodegroups

        cluster = self._cluster_with_ng("AL2_x86_64", "1.32")
        findings = _check_managed_nodegroups(cluster, "ap-northeast-2")

        assert all(f.severity == "pass" for f in findings)

    def test_al2023_nodegroup_targeting_1_33_passes(self):
        from eksupgrade.src.preflight import _check_managed_nodegroups

        cluster = self._cluster_with_ng("AL2023_x86_64_STANDARD", "1.33")
        findings = _check_managed_nodegroups(cluster, "ap-northeast-2")

        assert all(f.severity == "pass" for f in findings)


_BR_IMAGE = "amazon/bottlerocket-aws-k8s-1.32-x86_64-v1.32.0-cacc4ce9"
_AL2_IMAGE = "amazon/amazon-eks-node-1.32-v20250101"
_CURRENT_IMAGE = "eksupgrade.src.preflight._custom_ng_current_image"


class TestCustomNodegroupActualOs:
    """The CUSTOM preflight check must inspect the launch template's ACTUAL
    AMI (like the runtime resolver) instead of assuming Bottlerocket — a
    CUSTOM node group backed by an AL2 image used to pass preflight and then
    fail mid-upgrade."""

    def _cluster(self, target="1.33"):
        cluster = MagicMock()
        cluster.version = "1.32"
        cluster.target_version = target
        cluster.nodegroups = [_ng("ng-custom", "CUSTOM")]
        return cluster

    def test_custom_al2_image_blocks_past_1_32_with_migration_hint(self):
        with (
            patch(_CURRENT_IMAGE, return_value=_AL2_IMAGE),
            patch("eksupgrade.src.preflight.get_latest_ami") as mock_ami,
        ):
            findings = _check_managed_nodegroups(self._cluster("1.33"), region="ap-northeast-2")

        assert any(f.severity == "blocking" and "AL2023" in f.detail for f in findings)
        mock_ami.assert_not_called()

    def test_custom_al2_image_blocks_even_at_1_32(self):
        """The runtime resolver cannot classify an AL2 ImageLocation either, so
        this is blocking regardless of target — just with a different reason."""
        with (
            patch(_CURRENT_IMAGE, return_value=_AL2_IMAGE),
            patch("eksupgrade.src.preflight.get_latest_ami") as mock_ami,
        ):
            findings = _check_managed_nodegroups(self._cluster("1.32"), region="ap-northeast-2")

        assert any(f.severity == "blocking" for f in findings)
        mock_ami.assert_not_called()

    def test_custom_bottlerocket_image_resolves_with_real_os_hint(self):
        """The resolve call must carry the ACTUAL image hint (runtime contract),
        not a hardcoded 'bottlerocket' guess."""
        with (
            patch(_CURRENT_IMAGE, return_value=_BR_IMAGE),
            patch("eksupgrade.src.preflight.get_latest_ami", return_value="ami-0abc") as mock_ami,
        ):
            findings = _check_managed_nodegroups(self._cluster("1.33"), region="ap-northeast-2")

        assert any(f.item == "ng-custom" and f.severity == "pass" and "ami-0abc" in f.detail for f in findings)
        mock_ami.assert_called_once_with("1.33", _BR_IMAGE, _BR_IMAGE, "ap-northeast-2")

    def test_custom_inspect_failure_is_blocking(self):
        with patch(_CURRENT_IMAGE, side_effect=RuntimeError("lt gone")):
            findings = _check_managed_nodegroups(self._cluster("1.33"), region="ap-northeast-2")

        assert any(f.severity == "blocking" and "lt gone" in f.detail for f in findings)


def test_custom_ng_current_image_reads_lt_then_image():
    from eksupgrade.src.preflight import _custom_ng_current_image

    ng = MagicMock()
    ng.launch_template = {"id": "lt-1", "version": "3"}
    fake_ec2 = MagicMock()
    fake_ec2.describe_launch_template_versions.return_value = {
        "LaunchTemplateVersions": [{"LaunchTemplateData": {"ImageId": "ami-cur"}}]
    }
    fake_ec2.describe_images.return_value = {"Images": [{"ImageLocation": _BR_IMAGE}]}

    with patch("eksupgrade.src.preflight.boto3.client", return_value=fake_ec2):
        assert _custom_ng_current_image(ng, "ap-northeast-2") == _BR_IMAGE

    fake_ec2.describe_launch_template_versions.assert_called_once_with(LaunchTemplateId="lt-1", Versions=["3"])
    fake_ec2.describe_images.assert_called_once_with(ImageIds=["ami-cur"])
