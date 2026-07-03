"""Define the AMI specific logic."""

from __future__ import annotations

import boto3
from packaging.version import Version

from eksupgrade.utils import echo_error, get_logger

logger = get_logger(__name__)

# The last Kubernetes version with EKS-optimized Amazon Linux 2 AMIs.
AL2_FINAL_VERSION = "1.32"


def get_latest_ami(cluster_version: str, instance_type: str, image_to_search: str, region: str) -> str:
    """Get the latest AMI.

    Supports the following AMI types:
    - Amazon Linux 2023 (AL2023) - x86_64 and arm64
    - Amazon Linux 2 (AL2) - x86_64, arm64, GPU
    - Windows Server
    - Bottlerocket
    - Ubuntu
    """
    ssm = boto3.client("ssm", region_name=region)
    client = boto3.client("ec2", region_name=region)

    # AL2023 must be checked BEFORE AL2 to avoid substring match ("Amazon Linux 2" is in "Amazon Linux 2023")
    if "Amazon Linux 2023" in instance_type:
        if "arm64" in image_to_search.lower() or "arm64" in instance_type.lower():
            names = [
                f"/aws/service/eks/optimized-ami/{cluster_version}/amazon-linux-2023/arm64/standard/recommended/image_id"
            ]
        else:
            names = [
                f"/aws/service/eks/optimized-ami/{cluster_version}/amazon-linux-2023/x86_64/standard/recommended/image_id"
            ]
    elif "Amazon Linux 2" in instance_type:
        if Version(cluster_version) > Version(AL2_FINAL_VERSION):
            raise Exception(
                f"EKS-optimized Amazon Linux 2 AMIs end at Kubernetes {AL2_FINAL_VERSION}; "
                f"no AL2 AMI exists for {cluster_version}. Migrate the node group to AL2023 first."
            )
        if "arm64" in image_to_search.lower() or "arm64" in instance_type.lower():
            names = [f"/aws/service/eks/optimized-ami/{cluster_version}/amazon-linux-2-arm64/recommended/image_id"]
        else:
            names = [f"/aws/service/eks/optimized-ami/{cluster_version}/amazon-linux-2/recommended/image_id"]
    elif "Windows" in instance_type:
        names = [f"/aws/service/ami-windows-latest/{image_to_search}-{cluster_version}/image_id"]
    elif "bottlerocket" in instance_type.lower():
        if "arm64" in image_to_search.lower() or "arm64" in instance_type.lower():
            names = [f"/aws/service/bottlerocket/aws-k8s-{cluster_version}/arm64/latest/image_id"]
        else:
            names = [f"/aws/service/bottlerocket/aws-k8s-{cluster_version}/x86_64/latest/image_id"]
    elif "Ubuntu" in instance_type:
        filters = [
            {"Name": "owner-id", "Values": ["099720109477"]},
            {"Name": "name", "Values": [f"ubuntu-eks/k8s_{cluster_version}*"]},
            {"Name": "is-public", "Values": ["true"]},
        ]
        response = client.describe_images(Filters=filters)
        sorted_images = sorted(response["Images"], key=lambda x: x["CreationDate"], reverse=True)
        if sorted_images:
            return sorted_images[0].get("ImageId")
        raise Exception("Couldn't Find Latest Image Retry The Script")
    else:
        return "NAN"
    response = ssm.get_parameters(Names=names)
    if response.get("Parameters"):
        return response.get("Parameters")[0]["Value"]
    echo_error("Couldn't find the latest image - please retry the script!")
    raise Exception("Couldn't Find Latest Image Retry The Script")
