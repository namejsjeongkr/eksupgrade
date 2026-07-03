"""Test AWS list-API pagination.

describe_auto_scaling_groups returns at most 50 records per page and the EKS
list APIs cap at 100 — without pagination, resources past the first page were
silently dropped (e.g. a cluster with >50 tagged ASGs missed some node groups
entirely). All collection lookups must aggregate every page.
"""

from unittest.mock import MagicMock

from eksupgrade.models.eks import Cluster


def _paged_cluster() -> Cluster:
    cluster = Cluster(arn="abc", name="eks-test", version="1.35", target_version="1.36", region="ap-northeast-2")
    cluster.eks_client = MagicMock()
    cluster.__dict__["autoscaling_client"] = MagicMock()
    return cluster


def test_autoscaling_groups_aggregates_all_pages():
    cluster = _paged_cluster()
    paginator = cluster.autoscaling_client.get_paginator.return_value
    paginator.paginate.return_value = [
        {"AutoScalingGroups": [{"AutoScalingGroupName": "asg-1"}]},
        {"AutoScalingGroups": [{"AutoScalingGroupName": "asg-2"}]},
    ]
    # A non-paginated call would only ever see this single page:
    cluster.autoscaling_client.describe_auto_scaling_groups.return_value = {
        "AutoScalingGroups": [{"AutoScalingGroupName": "asg-1"}]
    }

    names = [asg.name for asg in cluster.autoscaling_groups]

    assert names == ["asg-1", "asg-2"]


def test_current_addons_aggregates_all_pages():
    cluster = _paged_cluster()
    paginator = cluster.eks_client.get_paginator.return_value
    paginator.paginate.return_value = [
        {"addons": ["vpc-cni", "coredns"]},
        {"addons": ["kube-proxy"]},
    ]
    cluster.eks_client.list_addons.return_value = {"addons": ["vpc-cni", "coredns"]}

    assert cluster.current_addons == ["vpc-cni", "coredns", "kube-proxy"]


def test_nodegroup_names_aggregates_all_pages():
    cluster = _paged_cluster()
    paginator = cluster.eks_client.get_paginator.return_value
    paginator.paginate.return_value = [
        {"nodegroups": ["ng-1"]},
        {"nodegroups": ["ng-2"]},
    ]
    cluster.eks_client.list_nodegroups.return_value = {"nodegroups": ["ng-1"]}

    assert cluster.nodegroup_names == ["ng-1", "ng-2"]


def test_rollback_insights_follow_next_token():
    from eksupgrade.src.rollback import get_rollback_readiness

    cluster = MagicMock()
    cluster.name = "eks-test"
    cluster.eks_client.list_insights.side_effect = [
        {
            "insights": [{"name": "check-1", "insightStatus": {"status": "PASSING", "reason": ""}}],
            "nextToken": "t1",
        },
        {"insights": [{"name": "check-2", "insightStatus": {"status": "ERROR", "reason": "x"}}]},
    ]

    readiness = get_rollback_readiness(cluster)

    assert [f["name"] for f in readiness.findings] == ["check-1", "check-2"]
    assert readiness.blocking == ["check-2"]
    second_call = cluster.eks_client.list_insights.call_args_list[1]
    assert second_call.kwargs.get("nextToken") == "t1"
