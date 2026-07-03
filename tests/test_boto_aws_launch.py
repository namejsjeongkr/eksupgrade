"""Test ASG launch-config/template handling in add_autoscaling.

CreateLaunchConfiguration is a retired API (blocked in newer AWS accounts),
and the legacy code attached a NEW Launch Configuration even to ASGs that use
Launch Templates — silently downgrading them. The new behavior:

- LT-backed ASG (direct or MixedInstancesPolicy): create a new LT version
  with the target AMI; re-pin the ASG only when it pins a concrete version.
- LC-backed ASG: keep the legacy LC-copy path (older accounts still work).
- Outdated instances are detected by ImageId, uniform across launch types.
"""

from unittest.mock import MagicMock, patch

import pytest

from eksupgrade.src.boto_aws import add_autoscaling, get_outdated_instance_ids

_BOTO = "eksupgrade.src.boto_aws.boto3"
_LT_UPDATE = "eksupgrade.src.boto_aws.update_current_launch_template_ami"


def _clients(mock_boto3, asg_description):
    asg = MagicMock()
    ec2 = MagicMock()
    mock_boto3.client.side_effect = lambda svc, region_name=None: asg if svc == "autoscaling" else ec2
    asg.describe_auto_scaling_groups.return_value = {"AutoScalingGroups": [asg_description]}
    return asg, ec2


@patch(_LT_UPDATE, return_value=4)
@patch(_BOTO)
def test_lt_asg_pinned_version_gets_new_lt_version_and_repin(mock_boto3, mock_lt_update):
    asg, _ = _clients(
        mock_boto3,
        {
            "AutoScalingGroupName": "asg-lt",
            "LaunchTemplate": {"LaunchTemplateId": "lt-1", "LaunchTemplateName": "lt", "Version": "3"},
            "Instances": [{"InstanceId": "i-1"}],
        },
    )

    add_autoscaling("asg-lt", "ami-new", "us-east-1")

    mock_lt_update.assert_called_once_with("lt-1", "ami-new", "us-east-1")
    asg.update_auto_scaling_group.assert_called_once_with(
        AutoScalingGroupName="asg-lt",
        LaunchTemplate={"LaunchTemplateId": "lt-1", "Version": "4"},
    )
    asg.create_launch_configuration.assert_not_called()


@patch(_LT_UPDATE, return_value=4)
@patch(_BOTO)
def test_lt_asg_tracking_latest_needs_no_repin(mock_boto3, mock_lt_update):
    asg, _ = _clients(
        mock_boto3,
        {
            "AutoScalingGroupName": "asg-lt",
            "LaunchTemplate": {"LaunchTemplateId": "lt-1", "LaunchTemplateName": "lt", "Version": "$Latest"},
            "Instances": [{"InstanceId": "i-1"}],
        },
    )

    add_autoscaling("asg-lt", "ami-new", "us-east-1")

    mock_lt_update.assert_called_once()
    asg.update_auto_scaling_group.assert_not_called()
    asg.create_launch_configuration.assert_not_called()


@patch(_LT_UPDATE, return_value=9)
@patch(_BOTO)
def test_mixed_instances_policy_asg_repins_policy_spec(mock_boto3, mock_lt_update):
    policy = {
        "LaunchTemplate": {
            "LaunchTemplateSpecification": {"LaunchTemplateId": "lt-2", "Version": "5"},
            "Overrides": [{"InstanceType": "t3.medium"}],
        },
        "InstancesDistribution": {"OnDemandPercentageAboveBaseCapacity": 50},
    }
    asg, _ = _clients(
        mock_boto3,
        {
            "AutoScalingGroupName": "asg-mixed",
            "MixedInstancesPolicy": policy,
            "Instances": [{"InstanceId": "i-1"}],
        },
    )

    add_autoscaling("asg-mixed", "ami-new", "us-east-1")

    mock_lt_update.assert_called_once_with("lt-2", "ami-new", "us-east-1")
    updated_policy = asg.update_auto_scaling_group.call_args.kwargs["MixedInstancesPolicy"]
    assert updated_policy["LaunchTemplate"]["LaunchTemplateSpecification"]["Version"] == "9"
    asg.create_launch_configuration.assert_not_called()


@patch(_LT_UPDATE)
@patch(_BOTO)
def test_lc_asg_keeps_legacy_launch_configuration_path(mock_boto3, mock_lt_update):
    asg, _ = _clients(
        mock_boto3,
        {
            "AutoScalingGroupName": "asg-lc",
            "LaunchConfigurationName": "old-lc",
            "Instances": [{"InstanceId": "i-1"}],
        },
    )

    add_autoscaling("asg-lc", "ami-new", "us-east-1")

    asg.create_launch_configuration.assert_called_once()
    assert asg.create_launch_configuration.call_args.kwargs["ImageId"] == "ami-new"
    mock_lt_update.assert_not_called()


@patch(_LT_UPDATE)
@patch(_BOTO)
def test_asg_without_lc_or_lt_raises(mock_boto3, mock_lt_update):
    _clients(mock_boto3, {"AutoScalingGroupName": "asg-x", "Instances": [{"InstanceId": "i-1"}]})

    with pytest.raises(Exception, match="[Ll]aunch"):
        add_autoscaling("asg-x", "ami-new", "us-east-1")


@patch(_BOTO)
def test_outdated_instance_ids_by_image(mock_boto3):
    """Outdated detection is by ImageId, uniform across LC/LT/mixed ASGs —
    the legacy LT path compared an instance's LT version against ITSELF and
    returned full dicts where the caller expected instance IDs."""
    asg, ec2 = _clients(
        mock_boto3,
        {
            "AutoScalingGroupName": "asg-1",
            "Instances": [{"InstanceId": "i-old"}, {"InstanceId": "i-new"}, {"InstanceId": "i-stopping"}],
        },
    )
    ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {"InstanceId": "i-old", "ImageId": "ami-old", "State": {"Name": "running"}},
                    {"InstanceId": "i-new", "ImageId": "ami-new", "State": {"Name": "running"}},
                    {"InstanceId": "i-stopping", "ImageId": "ami-old", "State": {"Name": "shutting-down"}},
                ]
            }
        ]
    }

    assert get_outdated_instance_ids("asg-1", "ami-new", "us-east-1") == ["i-old"]


@patch(_BOTO)
def test_outdated_instance_ids_empty_asg(mock_boto3):
    _clients(mock_boto3, {"AutoScalingGroupName": "asg-1", "Instances": []})

    assert get_outdated_instance_ids("asg-1", "ami-new", "us-east-1") == []
