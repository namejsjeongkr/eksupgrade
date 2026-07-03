"""Test EKS Upgrade k8s client specific logic."""

import base64
from unittest.mock import MagicMock, patch

from kubernetes import client as k8s_client

from eksupgrade.src.k8s_client import get_bearer_token, loading_config


def test_get_bearer_token(sts_client, eks_cluster, cluster_name, region) -> None:
    """Test the get_bearer_token method."""
    token = get_bearer_token(cluster_id=cluster_name, region=region)
    assert token.startswith("k8s-aws-v1.")


def test_loading_config(eks_client, eks_cluster, cluster_name, region) -> None:
    """Test the loading_config method."""
    result = loading_config(cluster_name, region=region)
    assert result == "Initialized"


def test_loading_config_sets_cluster_ca() -> None:
    """loading_config must wire ssl_ca_cert to a file holding the decoded cluster CA."""
    from eksupgrade.src.k8s_client import _CA_CERT_FILES

    _CA_CERT_FILES.clear()
    fake_ca_pem = b"-----BEGIN CERTIFICATE-----\nFAKECERTDATA\n-----END CERTIFICATE-----\n"
    fake_ca_b64 = base64.b64encode(fake_ca_pem).decode("utf-8")

    fake_eks = MagicMock()
    fake_eks.describe_cluster.return_value = {
        "cluster": {
            "endpoint": "https://example.eks.amazonaws.com",
            "certificateAuthority": {"data": fake_ca_b64},
        }
    }

    with (
        patch("eksupgrade.src.k8s_client.boto3.client", return_value=fake_eks),
        patch("eksupgrade.src.k8s_client.get_bearer_token", return_value="faketoken"),
    ):
        loading_config("my-cluster", "ap-northeast-2")

    cfg = k8s_client.Configuration.get_default_copy()
    assert cfg.host == "https://example.eks.amazonaws.com"
    assert cfg.verify_ssl is True
    assert cfg.ssl_ca_cert is not None
    with open(cfg.ssl_ca_cert, "rb") as fh:
        assert fh.read() == fake_ca_pem


@patch("eksupgrade.src.k8s_client.client")
@patch("eksupgrade.src.k8s_client.loading_config")
def test_find_node_tolerates_missing_provider_id(mock_loading, mock_k8s):
    """A node still registering can have spec.provider_id=None — find_node must
    skip it instead of crashing with AttributeError on None.split()."""
    from unittest.mock import MagicMock

    from eksupgrade.src.k8s_client import find_node

    node_registering = MagicMock()
    node_registering.spec.provider_id = None
    node_registering.metadata.name = "n-registering"

    node_ready = MagicMock()
    node_ready.spec.provider_id = "aws:///ap-northeast-2a/i-123"
    node_ready.metadata.name = "n-ready"
    node_ready.status.node_info.kube_proxy_version = "v1.36.0-eks-abc"
    node_ready.status.node_info.kubelet_version = "v1.36.0-eks-abc"
    node_ready.status.node_info.os_image = "Bottlerocket OS 1.62.0"

    response = MagicMock()
    response.items = [node_registering, node_ready]
    mock_k8s.CoreV1Api.return_value.list_node.return_value = response

    assert find_node("my-cluster", "i-123", "find", "us-east-1") == "n-ready"
    assert find_node("my-cluster", "i-999", "find", "us-east-1") == "NAN"
