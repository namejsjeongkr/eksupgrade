"""Define the starter module."""

from __future__ import annotations

import datetime
import queue
import threading
import time

from eksupgrade.utils import echo_error, echo_info, echo_success, echo_warning, get_logger

from .src.boto_aws import (
    add_autoscaling,
    add_node,
    get_latest_instance,
    get_num_of_instances,
    get_outdated_asg,
    get_outdated_instance_ids,
    wait_for_ready,
    worker_terminate,
)
from .src.eks_get_image_type import get_ami_name
from .src.k8s_client import (
    delete_node,
    drain_nodes,
    find_node,
    get_statefulset_pods_on_node,
    unschedule_old_nodes,
    wait_for_statefulset_pods_ready,
)
from .src.latest_ami import get_latest_ami
from .src.self_managed import update_nodegroup

logger = get_logger(__name__)

queue = queue.Queue()


class StatsWorker(threading.Thread):
    """Define the Stats worker for process and queue handling."""

    def __init__(self, queue, id, failures: list[str] | None = None) -> None:
        """Initialize the stats worker.

        failures collects the names of node groups whose update raised, so the
        caller can report them after queue.join().
        """
        threading.Thread.__init__(self)
        self.queue = queue
        self.id = id
        self.failures = failures if failures is not None else []

    def run(self) -> None:
        """Run the thread routine.

        task_done() must run even when an update raises — a dead worker that
        never marks its item done leaves the caller's queue.join() hanging
        forever (with the Cluster Autoscaler still paused).
        """
        while True:
            cluster_name, ng_name, to_update, region, max_retry, forced, typse = self.queue.get()
            try:
                echo_info(f"Updating node group: {ng_name} to version: {to_update}")
                if typse == "managed":
                    update_nodegroup(cluster_name, ng_name, to_update, region)
                elif typse == "selfmanaged":
                    actual_update(
                        cluster_name=cluster_name,
                        asg_iter=ng_name,
                        to_update=to_update,
                        region=region,
                        max_retry=max_retry,
                        forced=forced,
                    )
                echo_success(f"Updated node group: {ng_name} to version: {to_update}")
            except Exception as error:  # noqa: BLE001
                echo_error(f"Failed updating node group: {ng_name} - Error: {error}")
                self.failures.append(ng_name)
            finally:
                self.queue.task_done()


def actual_update(cluster_name, asg_iter, to_update, region, max_retry, forced):
    """Perform the update."""
    instance_type, image_to_search = get_ami_name(cluster_name, asg_iter, region)
    echo_info(f"The Image Type Detected = {instance_type}")

    if instance_type == "NAN":
        return False
    if isinstance(image_to_search, str) and "Windows_Server" in image_to_search:
        image_to_search = image_to_search[:46]
    latest_ami = get_latest_ami(to_update, instance_type, image_to_search, region)
    echo_info(f"The Latest AMI Recommended = {latest_ami}")

    if get_outdated_asg(asg_iter, latest_ami, region):
        add_autoscaling(asg_iter, latest_ami, region)
        echo_info(f"New Launch Configuration Added to = {asg_iter} With EKS AMI = {latest_ami}")

    outdated_instances = get_outdated_instance_ids(asg_iter, latest_ami, region)
    if not outdated_instances:
        return True

    try:
        terminated_ids = []
        echo_info(f"The Outdate Instance Found Are = {outdated_instances}")
        for instance in outdated_instances:
            before_count = get_num_of_instances(asg_iter, terminated_ids, region)
            echo_info(f"Total Instance count = {before_count}")
            add_time = datetime.datetime.now(datetime.timezone.utc)

            # Surge: ensure at least one FRESH (non-outdated) instance exists
            # before draining this node, adding one only when needed. The legacy
            # abs(count - outdated) != outdated formula also re-added on later
            # iterations when the replacement from the previous round already
            # provided the surge capacity.
            remaining_outdated = len(outdated_instances) - len(terminated_ids)
            fresh_count = before_count - remaining_outdated
            if fresh_count < 1:
                add_node(asg_iter, region)
                time.sleep(45)
                latest_instance = get_latest_instance(asg_name=asg_iter, add_time=add_time, region=region)
                echo_info(f"The Instance Created = {latest_instance} and waiting for it to be ready")
                time.sleep(30)
                wait_for_ready(latest_instance, region)

            old_pod_id = find_node(cluster_name=cluster_name, instance_id=instance, operation="find", region=region)
            if old_pod_id != "NAN":
                # Re-confirm the instance still maps to a node, retrying a BOUNDED
                # number of times. The legacy loop only advanced its counter when
                # the node WAS found, so a node deregistering mid-loop spun forever.
                flag = 0
                for _ in range(max_retry + 1):
                    if (
                        find_node(cluster_name=cluster_name, instance_id=instance, operation="find", region=region)
                        != "NAN"
                    ):
                        flag = 1
                        break
                    time.sleep(10)
                if flag == 0:
                    worker_terminate(instance, region=region)
                    echo_error("404 instance is not corresponded to particular node group")
                    raise Exception("404 instance is not corresponded to particular node group")

            # Capture StatefulSet pods BEFORE draining — drain removes them from
            # the node, after which they can no longer be identified.
            statefulset_pods = get_statefulset_pods_on_node(
                cluster_name=cluster_name, node_name=old_pod_id, region=region
            )

            echo_info(f"Unscheduling the worker node = {old_pod_id}")

            unschedule_old_nodes(cluster_name=cluster_name, node_name=old_pod_id, region=region)
            echo_info(f"The node: {old_pod_id} has been unscheduled! Worker Node Draining...")
            drain_nodes(cluster_name=cluster_name, node_name=old_pod_id, forced=forced, region=region)
            echo_info(f"The worker node has been drained! Deleting worker Node Started = {old_pod_id}")
            delete_node(cluster_name=cluster_name, node_name=old_pod_id, region=region)

            # Wait for evicted StatefulSet pods to reschedule and re-bind their PVCs
            # (pod-Ready confirms the volume mounted) before terminating the node.
            if statefulset_pods and not wait_for_statefulset_pods_ready(
                cluster_name=cluster_name, region=region, pods=statefulset_pods
            ):
                echo_warning(
                    f"StatefulSet pods from node {old_pod_id} are not all Ready yet; "
                    f"terminating the instance anyway — verify them manually.",
                )

            echo_info(f"The worker node: {old_pod_id} has been deleted. Terminating Worker Node: {instance}...")
            worker_terminate(instance, region=region)
            terminated_ids.append(instance)
            echo_success(f"The worker node instance: {instance} has been terminated!")
        return True
    except Exception as e:
        echo_error(f"Error encountered during actual update! Exception: {e}")
        raise e
