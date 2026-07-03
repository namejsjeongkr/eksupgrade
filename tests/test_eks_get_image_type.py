"""Test EKS Upgrade get image type specific logic."""

from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from eksupgrade.src.eks_get_image_type import get_ami_name, image_type

_EC2_MODULE = "eksupgrade.src.eks_get_image_type.boto3.client"


@pytest.mark.parametrize(
    "node_type,image_id",
    [
        ("windows server 2019 datacenter ", "ami-ekswin"),
        ("windows server 2022", "ami-ekswin"),
        ("amazon linux 2", "ami-ekslinux"),
    ],
)
def test_image_type(ec2_client, region, node_type, image_id) -> None:
    """Test the image_type method."""
    ami_id: Optional[str] = image_type(node_type=node_type, image_id=image_id, region=region)
    assert ami_id


# ---------------------------------------------------------------------------
# AL2023 support
# ---------------------------------------------------------------------------


class TestAL2023ImageType:
    """AL2023 nodes must use the al2023 AMI name filter."""

    def test_al2023_returns_matching_ami(self, region) -> None:
        """AL2023 os_image string must match against 'amazon-eks-node-al2023-*' AMIs."""
        ami_id = "ami-al2023-test"
        ami_name = "amazon-eks-node-al2023-x86_64-standard-1.32-v20240101"

        with patch(_EC2_MODULE) as mock_boto3:
            mock_ec2 = MagicMock()
            mock_ec2.describe_images.return_value = {"Images": [{"ImageId": ami_id, "Name": ami_name}]}
            mock_boto3.return_value = mock_ec2

            result = image_type(
                node_type="Amazon Linux 2023.3.20240219.0",
                image_id=ami_id,
                region=region,
            )

        assert result == ami_name

    def test_al2023_filter_uses_al2023_pattern(self, region) -> None:
        """The describe_images call must use the 'amazon-eks-node-al2023-*' name filter."""
        with patch(_EC2_MODULE) as mock_boto3:
            mock_ec2 = MagicMock()
            mock_ec2.describe_images.return_value = {
                "Images": [{"ImageId": "ami-al2023", "Name": "amazon-eks-node-al2023-x86_64-standard-1.32"}]
            }
            mock_boto3.return_value = mock_ec2

            image_type(
                node_type="Amazon Linux 2023.3.20240219.0",
                image_id="ami-al2023",
                region=region,
            )

            filters = mock_ec2.describe_images.call_args[1]["Filters"]
            name_filter = next(f for f in filters if f["Name"] == "name")
            assert (
                "al2023" in name_filter["Values"][0].lower()
            ), f"AL2023 node must use al2023 filter, got: {name_filter['Values']}"

    def test_al2023_not_matched_by_al2_filter(self, region) -> None:
        """'amazon linux 2023' must NOT match the 'amazon-eks-node*' (AL2) filter.

        'amazon linux 2' is a substring of 'amazon linux 2023'. If the check
        order is wrong, AL2023 nodes silently receive AL2 AMIs.
        """
        al2_ami_id = "ami-wrong-al2"

        with patch(_EC2_MODULE) as mock_boto3:
            # Simulate: AL2 describe_images would return an AL2 AMI
            mock_ec2 = MagicMock()
            mock_ec2.describe_images.return_value = {
                "Images": [{"ImageId": al2_ami_id, "Name": "amazon-eks-node-1.32-v20240101"}]
            }
            mock_boto3.return_value = mock_ec2

            image_type(
                node_type="Amazon Linux 2023.3.20240219.0",
                image_id="ami-different-id",  # won't match the AL2 AMI ID
                region=region,
            )

            filters = mock_ec2.describe_images.call_args[1]["Filters"]
            name_filter = next(f for f in filters if f["Name"] == "name")
            # The filter pattern must be for AL2023, not the generic AL2 pattern
            assert (
                name_filter["Values"][0] != "amazon-eks-node*"
            ), "AL2023 node type used the AL2 name filter — substring bug detected"
            assert "al2023" in name_filter["Values"][0].lower()


# ---------------------------------------------------------------------------
# Existing node types (additional coverage)
# ---------------------------------------------------------------------------


class TestKnownNodeTypes:
    """Verify filter patterns for AL2, Bottlerocket, and Windows."""

    @pytest.mark.parametrize(
        "node_type,expected_pattern",
        [
            ("Amazon Linux 2 Kernel 5.10", "amazon-eks-node*"),
            ("Bottlerocket OS 1.15.0", "bottlerocket-aws-k8s-*"),
            ("Windows Server 2022 Datacenter", "Windows_Server-*-English-*-EKS_Optimized*"),
        ],
    )
    def test_correct_filter_pattern_used(self, region, node_type, expected_pattern) -> None:
        with patch(_EC2_MODULE) as mock_boto3:
            mock_ec2 = MagicMock()
            mock_ec2.describe_images.return_value = {"Images": []}
            mock_boto3.return_value = mock_ec2

            image_type(node_type=node_type, image_id="ami-test", region=region)

            filters = mock_ec2.describe_images.call_args[1]["Filters"]
            name_filter = next(f for f in filters if f["Name"] == "name")
            assert (
                name_filter["Values"][0] == expected_pattern
            ), f"node_type '{node_type}' used wrong filter: {name_filter['Values'][0]!r}"


class TestUnsupportedNodeType:
    """Unsupported node types must return None without raising."""

    def test_unknown_os_returns_none(self, region) -> None:
        result = image_type(node_type="SomeUnknownOS 4.0", image_id="ami-x", region=region)
        assert result is None

    def test_empty_node_type_returns_none(self, region) -> None:
        result = image_type(node_type="", image_id="ami-x", region=region)
        assert result is None


@patch("eksupgrade.src.eks_get_image_type.image_type")
@patch("eksupgrade.src.eks_get_image_type.find_node")
@patch("eksupgrade.src.eks_get_image_type.boto3")
def test_mixed_os_asg_returns_node_type_then_image_name(mock_boto3, mock_find_node, mock_image_type):
    """Mixed-OS ASG: the return order must match the homogeneous case
    ([node_type, image_name]) — the legacy tuple was (image_name, node_type),
    so callers unpacked swapped values."""
    asg = MagicMock()
    ec2 = MagicMock()
    mock_boto3.client.side_effect = lambda svc, region_name=None: asg if svc == "autoscaling" else ec2
    asg.describe_auto_scaling_groups.return_value = {
        "AutoScalingGroups": [{"Instances": [{"InstanceId": "i-1"}, {"InstanceId": "i-2"}, {"InstanceId": "i-3"}]}]
    }
    ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {"InstanceId": "i-1", "ImageId": "ami-al2"},
                    {"InstanceId": "i-2", "ImageId": "ami-br"},
                    {"InstanceId": "i-3", "ImageId": "ami-br"},
                ]
            }
        ]
    }
    # One AL2 node, two Bottlerocket nodes -> least-repeated OS is AL2.
    mock_find_node.side_effect = ["Amazon Linux 2", "Bottlerocket OS 1.32.0", "Bottlerocket OS 1.32.0"]
    mock_image_type.side_effect = [
        "amazon-eks-node-1.32-v20250101",
        "bottlerocket-aws-k8s-1.32-x86_64",
        "bottlerocket-aws-k8s-1.32-x86_64",
    ]

    result = get_ami_name("my-cluster", "asg-1", "us-east-1")

    node_type, image_name = result[0], result[1]
    assert node_type == "Amazon Linux 2"
    assert image_name == "amazon-eks-node-1.32-v20250101"
