"""The EKS Upgrade kubernetes client module.

Attributes:
    queue (queue.Queue): The queue used for executing jobs (status checks, etc).

"""

from __future__ import annotations

import base64
import os
import queue
import re
import tempfile
import threading
import time
from functools import cache
from typing import Any

import boto3
from botocore.signers import RequestSigner
from kubernetes import client, watch
from kubernetes.client import V1Eviction
from kubernetes.client.rest import ApiException

from eksupgrade.utils import echo_error, echo_info, echo_warning, get_logger

logger = get_logger(__name__)

queue = queue.Queue()


class StatsWorker(threading.Thread):
    def __init__(self, queue, id):
        threading.Thread.__init__(self)
        self.queue = queue
        self.id = id

    def run(self):
        while self.queue.not_empty:
            cluster_name, namespace, new_pod_name, _, region = self.queue.get()
            status = addon_status(
                cluster_name=cluster_name,
                new_pod_name=new_pod_name,
                region=region,
                namespace=namespace,
            )
            # signals to queue job is done
            if not status:
                echo_error(
                    f"Pod not started! Cluster: {cluster_name} - Namespace: {namespace} - New Pod: {new_pod_name}"
                )
                raise Exception("Pod Not Started", new_pod_name)

            self.queue.task_done()


def get_bearer_token(cluster_id: str, region: str) -> str:
    """Authenticate the session with sts token."""
    sts_token_expiration_ttl: int = 900
    session = boto3.session.Session()

    sts_client = session.client("sts", region_name=region)
    service_id = sts_client.meta.service_model.service_id
    signer = RequestSigner(service_id, region, "sts", "v4", session.get_credentials(), session.events)

    params = {
        "method": "GET",
        "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "body": {},
        "headers": {"x-k8s-aws-id": cluster_id},
        "context": {},
    }

    # Getting a presigned Url
    signed_url = signer.generate_presigned_url(
        params, region_name=region, expires_in=sts_token_expiration_ttl, operation_name=""
    )
    base64_url = base64.urlsafe_b64encode(signed_url.encode("utf-8")).decode("utf-8")

    # remove any base64 encoding padding and returning the kubernetes token
    return "k8s-aws-v1." + re.sub(r"=*", "", base64_url)


_CA_CERT_FILES: dict[str, str] = {}


def _ca_cert_path(endpoint: str, ca_data_b64: str) -> str:
    """Decode a base64 PEM cluster CA to a temp file (cached per endpoint) and return its path."""
    cached = _CA_CERT_FILES.get(endpoint)
    if cached and os.path.exists(cached):
        return cached
    _CA_CERT_FILES.pop(endpoint, None)  # discard a stale (deleted-file) entry before rewriting
    fd, path = tempfile.mkstemp(prefix="eksupgrade-ca-", suffix=".pem")
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.b64decode(ca_data_b64))
    _CA_CERT_FILES[endpoint] = path
    return path


def loading_config(cluster_name: str, region: str) -> str:
    """Configure the default kubernetes client from EKS describe-cluster (CA + STS bearer token)."""
    eks = boto3.client("eks", region_name=region)
    resp = eks.describe_cluster(name=cluster_name)
    endpoint = resp["cluster"]["endpoint"]
    ca_data = resp["cluster"]["certificateAuthority"]["data"]
    configs = client.Configuration()
    configs.host = endpoint
    configs.verify_ssl = True
    configs.ssl_ca_cert = _ca_cert_path(endpoint, ca_data)
    configs.debug = False
    configs.api_key = {"authorization": "Bearer " + get_bearer_token(cluster_name, region)}
    client.Configuration.set_default(configs)
    return "Initialized"


def unschedule_old_nodes(cluster_name: str, node_name: str, region: str) -> None:
    """Unschedule the nodes to avoid new nodes being launched."""
    loading_config(cluster_name, region)
    try:
        core_v1_api = client.CoreV1Api()
        # unscheduling the nodes
        body = {"spec": {"unschedulable": True}}
        core_v1_api.patch_node(node_name, body)
    except Exception as e:
        echo_error(
            f"Exception encountered while attempting to unschedule old nodes - cluster: {cluster_name} - node: {node_name}",
        )
        raise e
    return


def watcher(cluster_name: str, name: str, region: str) -> bool:
    """Watch whether the pod is deleted or not."""
    loading_config(cluster_name, region)
    core_v1_api = client.CoreV1Api()
    _watcher = watch.Watch()

    try:
        for event in _watcher.stream(core_v1_api.list_pod_for_all_namespaces, timeout_seconds=30):
            echo_info(f"{event['type']} {event['object'].metadata.name}")

            if event["type"] == "DELETED" and event["object"].metadata.name == name:
                _watcher.stop()
                return True
        return False
    except Exception as e:
        echo_error(
            f"Exception encountered in watcher method against cluster: {cluster_name} name: {name} Error: {e}",
        )
        raise e


MAX_EVICTION_RETRIES = 3
PDB_EVICTION_TIMEOUT = 900
PDB_RETRY_INTERVAL = 5


def _is_daemonset_pod(pod) -> bool:
    """Return True if the pod is owned by a DaemonSet (must not be drained)."""
    return any(ref.kind == "DaemonSet" for ref in (pod.metadata.owner_references or []))


def _is_mirror_pod(pod) -> bool:
    """Return True if the pod is a static/mirror pod (cannot be evicted)."""
    return "kubernetes.io/config.mirror" in (pod.metadata.annotations or {})


def _is_standalone_pod(pod) -> bool:
    """Return True if the pod has no controller owner (unmanaged)."""
    return not (pod.metadata.owner_references or [])


def _evict_pod(
    cluster_name: str, node_name: str, pod, core_v1_api, region: str, pdb_timeout: int = PDB_EVICTION_TIMEOUT
) -> None:
    """Evict a single pod via the eviction API, retrying until the pod is gone.

    kubectl-faithful semantics: a 429 means a PodDisruptionBudget is temporarily
    blocking the eviction, so wait and retry (bounded by pdb_timeout) instead of
    failing the whole drain; a 404 means the pod is already gone, which IS success
    (also covers the retry after the watcher missed a fast DELETED event).
    """
    eviction_body = V1Eviction(metadata=client.V1ObjectMeta(name=pod.metadata.name, namespace=pod.metadata.namespace))
    deadline = time.monotonic() + pdb_timeout
    watch_misses = 0
    while watch_misses < MAX_EVICTION_RETRIES:
        try:
            core_v1_api.create_namespaced_pod_eviction(
                name=pod.metadata.name, namespace=pod.metadata.namespace, body=eviction_body
            )
        except ApiException as api_error:
            if api_error.status == 404:
                return
            if api_error.status == 429:
                if time.monotonic() >= deadline:
                    echo_error(
                        f"PodDisruptionBudget kept blocking eviction of pod: {pod.metadata.name} "
                        f"for {pdb_timeout}s - node: {node_name} - cluster: {cluster_name}",
                    )
                    raise Exception(
                        f"Eviction of pod {pod.metadata.name} blocked by a PodDisruptionBudget past {pdb_timeout}s"
                    ) from api_error
                echo_info(
                    f"Eviction of pod: {pod.metadata.name} is blocked by a PodDisruptionBudget; "
                    f"retrying in {PDB_RETRY_INTERVAL}s..."
                )
                time.sleep(PDB_RETRY_INTERVAL)
                continue
            raise
        if watcher(cluster_name, pod.metadata.name, region):
            return
        watch_misses += 1
    echo_error(
        f"Unable to evict pod: {pod.metadata.name} from node: {node_name} in cluster: {cluster_name}",
    )
    raise Exception(f"Error: Unable to delete pod {pod.metadata.name} from node {node_name}")


def drain_nodes(cluster_name, node_name, forced, region) -> str | None:
    """Drain ALL pods from a node via the eviction API (or force-delete if forced)."""
    loading_config(cluster_name, region)
    core_v1_api = client.CoreV1Api()
    api_response = core_v1_api.list_pod_for_all_namespaces(watch=False, field_selector=f"spec.nodeName={node_name}")

    if not api_response.items:
        return f"Empty Nothing to Drain {node_name}"

    # DaemonSet and mirror (static) pods are node-managed and must not be drained.
    drainable = [
        pod
        for pod in api_response.items
        if pod.spec.node_name == node_name and not _is_daemonset_pod(pod) and not _is_mirror_pod(pod)
    ]

    # Pre-validate before evicting anything: an unmanaged pod without --force aborts
    # the whole drain so the node is never left half-drained (kubectl-faithful).
    if not forced:
        orphans = [pod.metadata.name for pod in drainable if _is_standalone_pod(pod)]
        if orphans:
            echo_error(
                f"Node {node_name} has unmanaged pod(s) {orphans} that would be lost. Re-run with --force.",
            )
            raise Exception(f"Unmanaged pod(s) {orphans} on node {node_name} require --force to drain")

    for pod in drainable:
        try:
            if forced:
                core_v1_api.delete_namespaced_pod(
                    pod.metadata.name, pod.metadata.namespace, grace_period_seconds=0, body=client.V1DeleteOptions()
                )
            else:
                _evict_pod(cluster_name, node_name, pod, core_v1_api, region)
        except Exception as e:
            echo_error(
                f"Exception encountered while attempting to drain nodes! Node: {node_name} Cluster: {cluster_name} - Error: {e}",
            )
            raise Exception("Unable to Delete the Node")
    return None


def _is_statefulset_pod(pod) -> bool:
    """Return True if the pod is owned by a StatefulSet."""
    return any(ref.kind == "StatefulSet" for ref in (pod.metadata.owner_references or []))


def get_statefulset_pods_on_node(cluster_name: str, node_name: str, region: str) -> list[tuple[str, str]]:
    """Return (name, namespace) of StatefulSet pods on the node.

    Must be called BEFORE draining the node — draining removes the pods, after
    which they can no longer be identified. The returned identities are used to
    confirm the pods come back Ready (and thus rebind their PVCs) elsewhere.
    """
    loading_config(cluster_name, region)
    core_v1_api = client.CoreV1Api()
    api_response = core_v1_api.list_pod_for_all_namespaces(watch=False, field_selector=f"spec.nodeName={node_name}")
    return [
        (pod.metadata.name, pod.metadata.namespace)
        for pod in api_response.items
        if pod.spec.node_name == node_name and _is_statefulset_pod(pod)
    ]


def _pod_running_and_ready(pod) -> bool:
    """Return True if the pod is Running and has a Ready=True condition."""
    if pod.status.phase != "Running":
        return False
    return any(c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or []))


def wait_for_statefulset_pods_ready(
    cluster_name: str,
    region: str,
    pods: list[tuple[str, str]],
    timeout: int = 600,
    poll_interval: int = 15,
) -> bool:
    """Wait, with a bound, for the given StatefulSet pods to be Running and Ready.

    A StatefulSet pod cannot reach Ready until its volume mounts, so pod-Ready is
    the PVC-rebind confirmation. Returns immediately if there are no pods. Never
    forces anything — on timeout it reports and returns False.
    """
    if not pods:
        return True

    loading_config(cluster_name, region)
    core_v1_api = client.CoreV1Api()
    deadline = time.monotonic() + timeout

    while True:
        not_ready: list[str] = []
        for name, namespace in pods:
            try:
                pod = core_v1_api.read_namespaced_pod(name=name, namespace=namespace)
                if not _pod_running_and_ready(pod):
                    not_ready.append(name)
            except ApiException:
                # Pod not recreated yet (e.g. 404 during reschedule).
                not_ready.append(name)

        if not not_ready:
            echo_info("All StatefulSet pods are Running and Ready on their replacement nodes.")
            return True

        if time.monotonic() >= deadline:
            echo_warning(
                f"Timed out waiting for StatefulSet pods to become Ready: {not_ready}. "
                f"Check their PVCs / scheduling before continuing.",
            )
            return False

        echo_info(f"Waiting for StatefulSet pods to become Ready: {not_ready}")
        time.sleep(poll_interval)


def delete_node(cluster_name: str, node_name: str, region: str) -> None:
    """Delete the node from compute list this doesn't terminate the instance."""
    try:
        loading_config(cluster_name, region)
        core_v1_api = client.CoreV1Api()
        core_v1_api.delete_node(node_name)
        return
    except ApiException as e:
        echo_error(
            f"Exception encountered attempting to delete a node! Cluster: {cluster_name} - Node: {node_name} - Error: {e}",
        )
        raise e


def find_node(cluster_name: str, instance_id: str, operation: str, region: str) -> str:
    """Find the node by instance id."""
    loading_config(cluster_name, region)
    core_v1_api = client.CoreV1Api()
    nodes: list[list[str]] = []
    response = core_v1_api.list_node()

    if not response.items:
        return "NAN"

    for node in response.items:
        nodes.append(
            [
                node.spec.provider_id.split("/")[-1],
                node.metadata.name,
                node.status.node_info.kube_proxy_version.split("-")[0],
                node.status.node_info.kubelet_version.split("-")[0],
                node.status.node_info.os_image,
            ]
        )

    if operation == "find":
        for i in nodes:
            if i[0] == instance_id:
                return i[1]
        return "NAN"

    if operation == "os_type":
        for i in nodes:
            if i[0] == instance_id:
                echo_info(i[0])
                return i[-1]
        return "NAN"
    return "NAN"


def addon_status(cluster_name: str, new_pod_name: str, region: str, namespace: str) -> bool:
    """Get the status of an addon pod."""
    loading_config(cluster_name, region)
    core_v1_api = client.CoreV1Api()
    tts = 100
    now = time.time()

    while time.time() < now + tts:
        response = core_v1_api.read_namespaced_pod_status(name=new_pod_name, namespace=namespace)
        if response.status.container_statuses[0].ready and response.status.container_statuses[0].started:
            return True
    return False


def sort_pods(
    cluster_name: str,
    region: str,
    original_name: str,
    pod_name: str,
    old_pods_names: list[str],
    namespace: str,
    count: int = 90,
) -> str:
    """Sort the pod results."""
    if not count:
        echo_error(
            f"Pod has no associated new pod! Cluster: {cluster_name} - Namespace: {namespace} - Pod Name: {pod_name}",
        )
        raise Exception("Pod has No associated New Launch")

    pods_nodes = []
    loading_config(cluster_name, region)
    core_v1_api = client.CoreV1Api()
    try:
        if pod_name == "cluster-autoscaler":
            pod_list = core_v1_api.list_namespaced_pod(namespace=namespace, label_selector=f"app={pod_name}")
        else:
            pod_list = core_v1_api.list_namespaced_pod(namespace=namespace, label_selector=f"k8s-app={pod_name}")
    except Exception as e:
        echo_error(
            f"Exception encountered while attempting to get the pod list and sort_pods - cluster: {cluster_name}, error: {e}",
        )
        return "Not Found"

    echo_info(f"Total Pods With {pod_name} = {len(pod_list.items)}")
    for i in pod_list.items:
        pods_nodes.append([i.metadata.name, i.metadata.creation_timestamp])

    if pods_nodes:
        new_pod_name = sorted(pods_nodes, key=lambda x: x[1])[-1][0]
    else:
        count -= 1
        sort_pods(cluster_name, region, original_name, pod_name, old_pods_names, namespace, count)
        # TODO: Remove this.  Adding to resolve possible use before assignment below.
        new_pod_name = ""

    if original_name != new_pod_name and new_pod_name in old_pods_names:
        count -= 1
        sort_pods(cluster_name, region, original_name, pod_name, old_pods_names, namespace, count)
    return new_pod_name


@cache
def get_addon_details(cluster_name: str, addon: str, region: str) -> dict[str, Any]:
    """Get addon details which includes its current version."""
    eks_client = boto3.client("eks", region_name=region)
    addon_details: dict[str, Any] = eks_client.describe_addon(clusterName=cluster_name, addonName=addon).get(
        "addon", {}
    )
    return addon_details


@cache
def get_addon_update_kwargs(cluster_name: str, addon: str, region: str) -> dict[str, Any]:
    """Get kwargs for subsequent update to addon."""
    addon_details: dict[str, Any] = get_addon_details(cluster_name, addon, region)
    kwargs: dict[str, Any] = {}
    iam_role_arn: str | None = addon_details.get("serviceAccountRoleArn")
    config_values: str | None = addon_details.get("configurationValues")

    if iam_role_arn:
        kwargs["serviceAccountRoleArn"] = iam_role_arn
    if config_values:
        kwargs["configurationValues"] = config_values
    return kwargs


@cache
def get_addon_versions(version: str, region: str) -> list[dict[str, Any]]:
    """Get addon versions for the associated Kubernetes `version`."""
    eks_client = boto3.client("eks", region_name=region)
    addon_versions: list[dict[str, Any]] = eks_client.describe_addon_versions(kubernetesVersion=version).get(
        "addons", []
    )
    return addon_versions


@cache
def get_versions_by_addon(addon: str, version: str, region: str) -> dict[str, Any]:
    """Get target addon versions."""
    addon_versions: list[dict[str, Any]] = get_addon_versions(version, region)
    return next(item for item in addon_versions if item["addonName"] == addon)


@cache
def get_default_version(addon: str, version: str, region: str) -> str:
    """Get the EKS default version of the `addon`."""
    addon_dict: dict[str, Any] = get_versions_by_addon(addon, version, region)
    return next(
        item["addonVersion"]
        for item in addon_dict["addonVersions"]
        if item["compatibilities"][0]["defaultVersion"] is True
    )


def _is_cluster_autoscaler_deployment(deployment) -> bool:
    """Match the Cluster Autoscaler by exact name or by its well-known label.

    Helm installs commonly name it ``<release>-aws-cluster-autoscaler``, so the
    exact-name check alone misses those; the label is stable across installs.
    """
    if deployment.metadata.name == "cluster-autoscaler":
        return True
    labels = deployment.metadata.labels or {}
    return labels.get("app.kubernetes.io/name") in ("aws-cluster-autoscaler", "cluster-autoscaler")


def is_cluster_auto_scaler_present(cluster_name: str, region: str) -> tuple[bool, int, str, str]:
    """Determine whether the Cluster Autoscaler deployment is present.

    Returns (is_present, replicas_count, name, namespace).
    """
    loading_config(cluster_name, region)
    apps_v1_api = client.AppsV1Api()
    res = apps_v1_api.list_deployment_for_all_namespaces()
    for res_i in res.items:
        if _is_cluster_autoscaler_deployment(res_i):
            return (True, res_i.spec.replicas, res_i.metadata.name, res_i.metadata.namespace)
    return (False, 0, "", "")


def cluster_auto_enable_disable(
    cluster_name: str,
    operation: str,
    mx_val: int,
    region: str,
    name: str = "cluster-autoscaler",
    namespace: str = "kube-system",
) -> None:
    """Pause (replicas=0) or start (replicas=mx_val) the Cluster Autoscaler deployment."""
    loading_config(cluster_name, region)
    api = client.AppsV1Api()
    if operation == "pause":
        body = {"spec": {"replicas": 0}}
    elif operation == "start":
        body = {"spec": {"replicas": mx_val}}
    else:
        echo_error("Operation must be either pause or start to auto_enable_disable!")
        raise NotImplementedError("Operation must be either pause or start!")

    try:
        api.patch_namespaced_deployment(name=name, namespace=namespace, body=body)
    except Exception as e:
        echo_error(f"Exception encountered while running auto enable disable - Error: {e}")
        raise e


def is_karpenter_present(cluster_name: str, region: str) -> tuple[bool, int, str]:
    """Determine whether or not Karpenter is present.

    Karpenter can be deployed in either 'karpenter' or 'kube-system' namespace.
    Returns (is_present, replicas_count, namespace).
    """
    loading_config(cluster_name, region)
    apps_v1_api = client.AppsV1Api()

    # Check common namespaces for Karpenter deployment
    namespaces_to_check = ["karpenter", "kube-system"]

    for namespace in namespaces_to_check:
        try:
            res = apps_v1_api.list_namespaced_deployment(namespace=namespace)
            for res_i in res.items:
                if res_i.metadata.name == "karpenter":
                    echo_info(f"Found Karpenter in namespace: {namespace}")
                    return (True, res_i.spec.replicas, namespace)
        except ApiException as e:
            # Namespace might not exist, continue checking
            if e.status == 404:
                continue
            echo_error(f"Error checking namespace {namespace}: {e}")

    return (False, 0, "")


# NOTE: Karpenter node upgrades are handled by the drift-based flow in
# eksupgrade/src/karpenter.py (handle_karpenter_drift). The old approach of
# pausing the controller and terminating EC2 instances was removed: it caused a
# capacity gap, bypassed PodDisruptionBudgets / disruption budgets, and never
# updated the AMI. Karpenter's native Drift replaces nodes capacity-first.
