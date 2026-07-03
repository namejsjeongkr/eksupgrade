"""Test the AMI lookup logic including AL2023 support.

Tests cover:
- AL2023 x86_64 and arm64 SSM parameter paths
- AL2 SSM parameter path
- Bottlerocket x86_64 and arm64 SSM parameter paths
- Ubuntu AMI via EC2 describe_images
- Unknown OS returns "NAN"
- AL2023 is NOT matched by the AL2 code path (substring bug guard)

Note: /aws/service/* SSM paths are AWS-reserved and cannot be written via
      moto mock_ssm. All SSM-backed tests patch the boto3 SSM client directly.
"""

from unittest.mock import MagicMock, patch

from eksupgrade.src.latest_ami import get_latest_ami

K8S_VERSION = "1.32"

_BOTO3_CLIENT = "eksupgrade.src.latest_ami.boto3.client"


def _make_ssm_response(image_id: str) -> dict:
    """Build a minimal SSM get_parameters response."""
    return {"Parameters": [{"Name": "irrelevant", "Value": image_id}]}


def _make_ssm_mock(image_id: str) -> MagicMock:
    mock_ssm = MagicMock()
    mock_ssm.get_parameters.return_value = _make_ssm_response(image_id)
    return mock_ssm


class TestAL2023AMI:
    """AL2023 AMI lookup must use the correct SSM paths, not the AL2 paths."""

    def test_x86_64_uses_al2023_ssm_path(self, region) -> None:
        expected = "ami-al2023-x86-64"
        captured_names: list = []

        def fake_ssm_get(Names):
            captured_names.extend(Names)
            return _make_ssm_response(expected)

        with patch(_BOTO3_CLIENT) as mock_boto3:
            mock_ssm = MagicMock()
            mock_ssm.get_parameters.side_effect = fake_ssm_get
            mock_boto3.return_value = mock_ssm

            result = get_latest_ami(
                K8S_VERSION,
                "Amazon Linux 2023.1.0",
                "amazon-eks-node-al2023-x86_64-standard",
                region,
            )

        assert result == expected
        assert any(
            "amazon-linux-2023/x86_64" in n for n in captured_names
        ), f"Expected x86_64 AL2023 SSM path, got: {captured_names}"

    def test_arm64_detected_from_image_name(self, region) -> None:
        expected = "ami-al2023-arm64"
        captured_names: list = []

        def fake_ssm_get(Names):
            captured_names.extend(Names)
            return _make_ssm_response(expected)

        with patch(_BOTO3_CLIENT) as mock_boto3:
            mock_ssm = MagicMock()
            mock_ssm.get_parameters.side_effect = fake_ssm_get
            mock_boto3.return_value = mock_ssm

            result = get_latest_ami(
                K8S_VERSION,
                "Amazon Linux 2023.1.0",
                "amazon-eks-node-al2023-arm64-standard",
                region,
            )

        assert result == expected
        assert any("arm64" in n for n in captured_names), f"Expected arm64 SSM path, got: {captured_names}"

    def test_arm64_detected_from_instance_type_string(self, region) -> None:
        """arm64 keyword in instance_type should select the arm64 SSM path."""
        expected = "ami-al2023-arm64-via-type"
        captured_names: list = []

        def fake_ssm_get(Names):
            captured_names.extend(Names)
            return _make_ssm_response(expected)

        with patch(_BOTO3_CLIENT) as mock_boto3:
            mock_ssm = MagicMock()
            mock_ssm.get_parameters.side_effect = fake_ssm_get
            mock_boto3.return_value = mock_ssm

            result = get_latest_ami(
                K8S_VERSION,
                "Amazon Linux 2023.1.0 arm64",
                "some-image",
                region,
            )

        assert result == expected
        assert any("arm64" in n for n in captured_names)

    def test_al2023_not_routed_to_al2_ssm_path(self, region) -> None:
        """AL2023 OS image must NOT fall through to the AL2 SSM code path.

        'Amazon Linux 2' is a substring of 'Amazon Linux 2023'. If the condition
        order is wrong, AL2023 instances silently receive AL2 AMIs.
        """
        captured_names: list = []

        def fake_ssm_get(Names):
            captured_names.extend(Names)
            return _make_ssm_response("ami-al2023")

        with patch(_BOTO3_CLIENT) as mock_boto3:
            mock_ssm = MagicMock()
            mock_ssm.get_parameters.side_effect = fake_ssm_get
            mock_boto3.return_value = mock_ssm

            get_latest_ami(
                K8S_VERSION,
                "Amazon Linux 2023.3.20240219.0",
                "amazon-eks-node-al2023-x86_64-standard",
                region,
            )

        assert not any(
            "amazon-linux-2/recommended" in n for n in captured_names
        ), f"AL2023 was incorrectly routed to the AL2 SSM path. Requested: {captured_names}"
        assert any(
            "amazon-linux-2023" in n for n in captured_names
        ), f"AL2023 SSM path not used. Requested: {captured_names}"


class TestAL2AMI:
    """AL2 AMI lookup via SSM."""

    def test_al2_uses_correct_ssm_path(self, region) -> None:
        expected = "ami-al2"
        captured_names: list = []

        def fake_ssm_get(Names):
            captured_names.extend(Names)
            return _make_ssm_response(expected)

        with patch(_BOTO3_CLIENT) as mock_boto3:
            mock_ssm = MagicMock()
            mock_ssm.get_parameters.side_effect = fake_ssm_get
            mock_boto3.return_value = mock_ssm

            result = get_latest_ami(
                K8S_VERSION,
                "Amazon Linux 2 Kernel 5.10 AMI 2.0.20240101 x86_64 HVM",
                "amazon-eks-node",
                region,
            )

        assert result == expected
        assert any(
            "amazon-linux-2/recommended" in n for n in captured_names
        ), f"Expected AL2 SSM path, got: {captured_names}"

    def test_al2_arm64_uses_arm64_ssm_path(self, region) -> None:
        """AL2 arm64 must use the 'amazon-linux-2-arm64' SSM path, not the x86_64 path."""
        expected = "ami-al2-arm64"
        captured_names: list = []

        def fake_ssm_get(Names):
            captured_names.extend(Names)
            return _make_ssm_response(expected)

        with patch(_BOTO3_CLIENT) as mock_boto3:
            mock_ssm = MagicMock()
            mock_ssm.get_parameters.side_effect = fake_ssm_get
            mock_boto3.return_value = mock_ssm

            result = get_latest_ami(
                K8S_VERSION,
                "Amazon Linux 2 Kernel 5.10 AMI 2.0.20240101 arm64 HVM",
                "amazon-eks-node-arm64",
                region,
            )

        assert result == expected
        assert any(
            "amazon-linux-2-arm64" in n for n in captured_names
        ), f"Expected AL2 arm64 SSM path (amazon-linux-2-arm64), got: {captured_names}"
        assert not any(
            "amazon-linux-2/recommended" in n for n in captured_names
        ), f"AL2 arm64 must NOT use x86_64 path, got: {captured_names}"

    def test_al2_os_string_does_not_use_al2023_path(self, region) -> None:
        captured_names: list = []

        def fake_ssm_get(Names):
            captured_names.extend(Names)
            return _make_ssm_response("ami-al2")

        with patch(_BOTO3_CLIENT) as mock_boto3:
            mock_ssm = MagicMock()
            mock_ssm.get_parameters.side_effect = fake_ssm_get
            mock_boto3.return_value = mock_ssm

            get_latest_ami(
                K8S_VERSION,
                "Amazon Linux 2 Kernel 5.10 AMI",
                "amazon-eks-node",
                region,
            )

        assert not any(
            "amazon-linux-2023" in n for n in captured_names
        ), f"AL2 was incorrectly routed to AL2023 SSM path. Requested: {captured_names}"


class TestBottlerocketAMI:
    """Bottlerocket AMI lookup via SSM."""

    def test_x86_64_uses_correct_ssm_path(self, region) -> None:
        expected = "ami-bottlerocket-x86"
        captured_names: list = []

        def fake_ssm_get(Names):
            captured_names.extend(Names)
            return _make_ssm_response(expected)

        with patch(_BOTO3_CLIENT) as mock_boto3:
            mock_ssm = MagicMock()
            mock_ssm.get_parameters.side_effect = fake_ssm_get
            mock_boto3.return_value = mock_ssm

            result = get_latest_ami(
                K8S_VERSION,
                "Bottlerocket OS 1.15.0 (aws-k8s-1.32)",
                "bottlerocket-aws-k8s-1.32-x86_64",
                region,
            )

        assert result == expected
        assert any(
            f"aws-k8s-{K8S_VERSION}/x86_64" in n for n in captured_names
        ), f"Expected x86_64 Bottlerocket SSM path, got: {captured_names}"

    def test_arm64_uses_correct_ssm_path(self, region) -> None:
        expected = "ami-bottlerocket-arm64"
        captured_names: list = []

        def fake_ssm_get(Names):
            captured_names.extend(Names)
            return _make_ssm_response(expected)

        with patch(_BOTO3_CLIENT) as mock_boto3:
            mock_ssm = MagicMock()
            mock_ssm.get_parameters.side_effect = fake_ssm_get
            mock_boto3.return_value = mock_ssm

            result = get_latest_ami(
                K8S_VERSION,
                "Bottlerocket OS 1.15.0 (aws-k8s-1.32)",
                "bottlerocket-aws-k8s-1.32-arm64",
                region,
            )

        assert result == expected
        assert any(
            f"aws-k8s-{K8S_VERSION}/arm64" in n for n in captured_names
        ), f"Expected arm64 Bottlerocket SSM path, got: {captured_names}"


class TestUbuntuAMI:
    """Ubuntu AMI lookup uses EC2 describe_images (not SSM)."""

    def test_returns_latest_ubuntu_ami(self, region) -> None:
        """Ubuntu AMI is resolved via describe_images filtered by owner-id and name.

        Moto's owner-id filter rejects the Canonical owner ID (099720109477)
        when the registered AMI belongs to the mock account. We patch the EC2
        client directly so the test stays owner-agnostic and validates the
        code path, not the AWS account setup.
        """
        expected_ami_id = "ami-ubuntu-test-12345"

        with patch(_BOTO3_CLIENT) as mock_boto3:
            mock_ec2 = MagicMock()
            mock_ec2.describe_images.return_value = {
                "Images": [
                    {
                        "ImageId": expected_ami_id,
                        "Name": f"ubuntu-eks/k8s_{K8S_VERSION}-2024-01-01",
                        "CreationDate": "2024-01-01T00:00:00Z",
                    }
                ]
            }
            # latest_ami.py creates both ssm and ec2 clients; return the same mock
            mock_boto3.return_value = mock_ec2

            result = get_latest_ami(K8S_VERSION, "Ubuntu 20.04 LTS", "", region)

        assert result == expected_ami_id

    def test_ubuntu_uses_describe_images_not_ssm(self, region) -> None:
        """Ubuntu lookup must call describe_images, not ssm.get_parameters."""
        with patch(_BOTO3_CLIENT) as mock_boto3:
            mock_ec2 = MagicMock()
            mock_ec2.describe_images.return_value = {
                "Images": [
                    {
                        "ImageId": "ami-ubuntu",
                        "Name": f"ubuntu-eks/k8s_{K8S_VERSION}-2024-01-01",
                        "CreationDate": "2024-01-01T00:00:00Z",
                    }
                ]
            }
            mock_boto3.return_value = mock_ec2

            get_latest_ami(K8S_VERSION, "Ubuntu 20.04 LTS", "", region)

        mock_ec2.describe_images.assert_called_once()
        mock_ec2.get_parameters.assert_not_called()


class TestUnknownAMI:
    """Unknown OS types must return 'NAN' without raising."""

    def test_unknown_os_returns_nan(self, region) -> None:
        result = get_latest_ami(K8S_VERSION, "SomeCustomOS 3.0", "some-image", region)
        assert result == "NAN"

    def test_empty_instance_type_returns_nan(self, region) -> None:
        result = get_latest_ami(K8S_VERSION, "", "", region)
        assert result == "NAN"


class TestAl2EndOfLife:
    """AL2 EKS-optimized AMIs end at Kubernetes 1.32 — requesting an AL2 AMI
    for 1.33+ must fail with a clear migration message instead of an opaque
    SSM parameter-not-found error mid-upgrade."""

    def test_al2_at_or_past_1_33_raises_with_migration_hint(self, region) -> None:
        import pytest

        with pytest.raises(Exception, match="AL2023"):
            get_latest_ami("1.33", "Amazon Linux 2", "amazon-eks-node-1.32", region)

    def test_al2_at_1_32_still_resolves(self, region) -> None:
        mock_ssm = _make_ssm_mock("ami-al2-132")
        with patch(_BOTO3_CLIENT, side_effect=lambda svc, region_name=None: mock_ssm):
            result = get_latest_ami("1.32", "Amazon Linux 2", "amazon-eks-node-1.32", region)
        assert result == "ami-al2-132"

    def test_al2023_at_1_33_is_unaffected(self, region) -> None:
        mock_ssm = _make_ssm_mock("ami-al2023-133")
        with patch(_BOTO3_CLIENT, side_effect=lambda svc, region_name=None: mock_ssm):
            result = get_latest_ami("1.33", "Amazon Linux 2023", "amazon-eks-node-al2023", region)
        assert result == "ami-al2023-133"
