"""Define the EKS upgrade boto specific logic."""

from __future__ import annotations

import datetime
import time
import uuid
from typing import Any

import boto3

from eksupgrade.utils import echo_error, echo_info, echo_success, get_logger

from .self_managed import update_current_launch_template_ami

logger = get_logger(__name__)


def status_of_cluster(cluster_name: str, region: str) -> list[str]:
    """Check the satus of the cluster and version of the cluster."""
    eks_client = boto3.client("eks", region_name=region)
    try:
        response = eks_client.describe_cluster(name=cluster_name)
        return [response["cluster"]["status"], response["cluster"]["version"]]
    except Exception as e:
        echo_error(f"Exception encountered while attempting to get cluster status - Error: {e}")
        raise e


def is_cluster_exists(cluster_name: str, region: str) -> str:
    """Check whether the cluster exists or not."""
    try:
        response = status_of_cluster(cluster_name, region)
        return response[0]
    except Exception as e:
        echo_error(f"Exception encountered while checking if cluster exists. Error: {e}")
        raise e


def get_latest_instance(asg_name: str, add_time: datetime.datetime, region: str) -> str:
    """Retrieve the most recently launched/launching instance.

    Note that this is not necessarily the same one that was launched by `add_node()`,
    but it's the best I could think of.

    """
    asg_client = boto3.client("autoscaling", region_name=region)
    ec2_client = boto3.client("ec2", region_name=region)
    instances: list[dict[str, Any]] = []

    response = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    time.sleep(20)
    instance_ids = [instance["InstanceId"] for instance in response["AutoScalingGroups"][0]["Instances"]]

    response = ec2_client.describe_instances(InstanceIds=instance_ids)
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            instances.append(instance)

    instances_valid: list[dict[str, Any]] = []
    instances_valid = [
        instance
        for instance in instances
        if instance["State"]["Name"] in ["pending", "running"] and instance["LaunchTime"] > add_time
    ]

    latest_instance: dict[str, Any] = {}
    try:
        time.sleep(10)
        latest_instance = sorted(instances_valid, key=lambda instance: instance["LaunchTime"])[-1]
        return latest_instance["InstanceId"]
    except Exception as e:
        echo_error(f"Exception encountered while sorting instances. Error: {e}")
        raise e


def wait_for_ready(instanceid: str, region: str, timeout: int = 1800) -> bool:
    """Wait, with a bound, for the instance to pass the status checks.

    An instance stuck in initializing/impaired must fail the upgrade after
    `timeout` seconds instead of hanging the whole run forever.
    """
    ec2_client = boto3.client("ec2", region_name=region)
    echo_info(f"Instance {instanceid} waiting for the instance to pass the Health Checks")
    deadline = time.monotonic() + timeout
    try:
        while (
            ec2_client.describe_instance_status(InstanceIds=[instanceid])["InstanceStatuses"][0]["InstanceStatus"][
                "Details"
            ][0]["Status"]
            != "passed"
        ):
            if time.monotonic() >= deadline:
                raise Exception(f"Instance {instanceid} did not pass health checks within {timeout}s")
            echo_info(f"Instance: {instanceid} waiting for the instance to pass the Health Checks")
            time.sleep(20)
    except Exception as e:
        echo_error(str(e))
        raise Exception(f"{e}: Please rerun the Script the instance will be created")
    return True


def worker_terminate(instance_id: str, region: str) -> None:
    """Terminate instance and decreasing the desired capacity whit asg terminate instance."""
    asg_client = boto3.client("autoscaling", region_name=region)

    try:
        asg_client.terminate_instance_in_auto_scaling_group(InstanceId=instance_id, ShouldDecrementDesiredCapacity=True)
    except Exception as e:
        echo_error(f"Exception encountered while attempting to terminate worker: {instance_id} - Error: {e}")
        raise e


def add_node(asg_name: str, region: str) -> None:
    """Add node to particular ASG."""
    asg_client = boto3.client("autoscaling", region_name=region)

    response = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    try:
        old_capacity_mx = response["AutoScalingGroups"][0]["MaxSize"]
        old_capacity_des = response["AutoScalingGroups"][0]["DesiredCapacity"]
    except (KeyError, IndexError):
        echo_error(f"Exception encountered while getting old ASG capacity during add_node - ASG: {asg_name}")
        raise Exception("Error Index out of bound due to no max capacity field")

    if int(old_capacity_des) >= int(old_capacity_mx):
        # Grow MaxSize by exactly the one node being added — the legacy
        # max+desired formula permanently inflated MaxSize (it is never restored).
        asg_client.update_auto_scaling_group(AutoScalingGroupName=asg_name, MaxSize=int(old_capacity_des) + 1)

    old_capacity = response["AutoScalingGroups"][0]["DesiredCapacity"]
    new_capacity = old_capacity + 1

    try:
        asg_client.set_desired_capacity(AutoScalingGroupName=asg_name, DesiredCapacity=new_capacity)
        echo_info(f"New Node has been Added to {asg_name}")
    except Exception as e:
        echo_error(f"Exception encountered while attempting to add node to ASG: {asg_name} - Error: {e}")
        raise e


def get_num_of_instances(asg_name: str, exclude_ids: list[str], region: str) -> int:
    """Count the number of instances."""
    asg_client = boto3.client("autoscaling", region_name=region)
    ec2_client = boto3.client("ec2", region_name=region)
    instances: list[dict[str, Any]] = []

    response = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    instance_ids = [
        instance["InstanceId"]
        for instance in response["AutoScalingGroups"][0]["Instances"]
        if instance["InstanceId"] not in exclude_ids
    ]
    response = ec2_client.describe_instances(InstanceIds=instance_ids)
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            instances.append(instance)
    # getting the instance in running or pending state
    instances = [instance for instance in instances if instance["State"]["Name"] in ["running", "pending"]]

    return len(instances)


def get_outdated_instance_ids(asg_name: str, latest_img: str, region: str) -> list[str]:
    """Return running/pending instance IDs whose ImageId differs from latest_img.

    Detection is by image, uniform across launch-configuration, launch-template,
    and mixed-instances ASGs — the legacy per-launch-type comparison was broken
    for launch templates (it compared an instance's LT version against itself
    and returned full dicts where the caller expected instance IDs).
    """
    asg_client = boto3.client("autoscaling", region_name=region)
    ec2_client = boto3.client("ec2", region_name=region)
    response = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    instance_ids = [instance["InstanceId"] for instance in response["AutoScalingGroups"][0]["Instances"]]
    if not instance_ids:
        return []

    outdated: list[str] = []
    inst_response = ec2_client.describe_instances(InstanceIds=instance_ids)
    for reservation in inst_response["Reservations"]:
        for instance in reservation["Instances"]:
            if instance["State"]["Name"] in ("running", "pending") and instance["ImageId"] != latest_img:
                outdated.append(instance["InstanceId"])
    return outdated


def _asg_launch_template_spec(asg_data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the ASG's launch template specification (direct or mixed-instances), if any."""
    if asg_data.get("LaunchTemplate"):
        return asg_data["LaunchTemplate"]
    mixed = asg_data.get("MixedInstancesPolicy", {})
    return mixed.get("LaunchTemplate", {}).get("LaunchTemplateSpecification") or None


def add_autoscaling(asg_name: str, img_id: str, region: str) -> dict[str, Any]:
    """Point the ASG at the target AMI.

    Launch-template ASGs (direct or MixedInstancesPolicy) get a NEW launch
    template version with the AMI; the ASG is re-pinned only when it pins a
    concrete version (a $Latest/$Default spec picks the new version up on its
    own). Launch-configuration ASGs keep the legacy LC-copy path — note that
    CreateLaunchConfiguration is retired and blocked in newer AWS accounts;
    the legacy code attached an LC even to launch-template ASGs, silently
    downgrading them.
    """
    asg_client = boto3.client("autoscaling", region_name=region)
    response = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    asg_data = response["AutoScalingGroups"][0]

    lt_spec = _asg_launch_template_spec(asg_data)
    if lt_spec:
        lt_id = lt_spec["LaunchTemplateId"]
        new_version = update_current_launch_template_ami(lt_id, img_id, region)
        current_version = str(lt_spec.get("Version", "$Latest"))
        if current_version not in ("$Latest", "$Default"):
            if asg_data.get("LaunchTemplate"):
                asg_client.update_auto_scaling_group(
                    AutoScalingGroupName=asg_name,
                    LaunchTemplate={"LaunchTemplateId": lt_id, "Version": str(new_version)},
                )
            else:
                mixed_policy = asg_data["MixedInstancesPolicy"]
                mixed_policy["LaunchTemplate"]["LaunchTemplateSpecification"]["Version"] = str(new_version)
                asg_client.update_auto_scaling_group(AutoScalingGroupName=asg_name, MixedInstancesPolicy=mixed_policy)
        echo_success(f"Launch template {lt_id} now at version {new_version} with AMI {img_id}")
        return {"launchTemplateId": lt_id, "version": new_version}

    if not asg_data.get("LaunchConfigurationName"):
        raise Exception(f"ASG {asg_name} has neither a launch template nor a launch configuration")

    timestamp = time.time()
    timestamp_string = datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d  %H-%M-%S")
    source_instance_id = asg_data["Instances"][0]["InstanceId"]
    new_launch_config_name = f"LC {img_id} {timestamp_string} {str(uuid.uuid4())}"

    try:
        asg_client.create_launch_configuration(
            InstanceId=source_instance_id, LaunchConfigurationName=new_launch_config_name, ImageId=img_id
        )
        response = asg_client.update_auto_scaling_group(
            AutoScalingGroupName=asg_name, LaunchConfigurationName=new_launch_config_name
        )
        echo_success("Updated to latest launch configuration")
    except Exception as e:
        echo_error(
            f"Exception encountered while executing add_autoscaling with ASG: {asg_name} - Image ID: {img_id} - Region: {region} - Error: {e}",
        )
        raise e
    return response


def get_outdated_asg(asg_name: str, latest_img: str, region: str) -> bool:
    """Get the outdated autoscaling group."""
    asg_client = boto3.client("autoscaling", region_name=region)
    ec2_client = boto3.client("ec2", region_name=region)
    response = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    instance_ids = [instance["InstanceId"] for instance in response["AutoScalingGroups"][0]["Instances"]]
    old_ami_inst: list[str] = []
    # filtering old instance where the logic is used to check whether we should add new launch configuration or not
    inst_response = ec2_client.describe_instances(InstanceIds=instance_ids)
    for reservation in inst_response["Reservations"]:
        for instance in reservation["Instances"]:
            if instance["ImageId"] != latest_img:
                old_ami_inst.append(instance["InstanceId"])
    instance_ids.sort()
    old_ami_inst.sort()
    if len(old_ami_inst) != len(instance_ids):
        return False

    for count, value in enumerate(old_ami_inst):
        if value != instance_ids[count]:
            return False
    return True
