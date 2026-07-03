"""Test loading_config caching.

Every kubernetes helper calls loading_config(), and the legacy implementation
re-ran describe_cluster + STS token generation on EVERY call — draining one
node with N pods cost ~N AWS round-trips. The config must be cached per
(cluster, region) and refreshed before the 15-minute STS token expires.
"""

from unittest.mock import MagicMock, patch

from eksupgrade.src.k8s_client import _CONFIG_STATE, loading_config

_BOTO = "eksupgrade.src.k8s_client.boto3"
_TOKEN = "eksupgrade.src.k8s_client.get_bearer_token"
_CA = "eksupgrade.src.k8s_client._ca_cert_path"
_K8S = "eksupgrade.src.k8s_client.client"


def _reset_cache() -> None:
    _CONFIG_STATE["key"] = None
    _CONFIG_STATE["expires"] = 0.0


def _mock_eks(mock_boto3) -> MagicMock:
    eks = mock_boto3.client.return_value
    eks.describe_cluster.return_value = {
        "cluster": {"endpoint": "https://example.eks", "certificateAuthority": {"data": "Y2E="}}
    }
    return eks


@patch(_K8S)
@patch(_CA, return_value="/tmp/ca.pem")
@patch(_TOKEN, return_value="k8s-aws-v1.token")
@patch(_BOTO)
def test_repeated_calls_reuse_cached_config(mock_boto3, mock_token, mock_ca, mock_k8s):
    """Back-to-back calls for the same cluster must hit AWS only once."""
    _reset_cache()
    eks = _mock_eks(mock_boto3)

    for _ in range(5):
        assert loading_config("my-cluster", "us-east-1") == "Initialized"

    assert eks.describe_cluster.call_count == 1
    assert mock_token.call_count == 1


@patch(_K8S)
@patch(_CA, return_value="/tmp/ca.pem")
@patch(_TOKEN, return_value="k8s-aws-v1.token")
@patch(_BOTO)
def test_expired_cache_is_refreshed(mock_boto3, mock_token, mock_ca, mock_k8s):
    """After the TTL passes (STS token nearing expiry) the config must be rebuilt."""
    _reset_cache()
    eks = _mock_eks(mock_boto3)

    loading_config("my-cluster", "us-east-1")
    _CONFIG_STATE["expires"] = 0.0  # simulate TTL elapsed
    loading_config("my-cluster", "us-east-1")

    assert eks.describe_cluster.call_count == 2


@patch(_K8S)
@patch(_CA, return_value="/tmp/ca.pem")
@patch(_TOKEN, return_value="k8s-aws-v1.token")
@patch(_BOTO)
def test_different_cluster_reconfigures(mock_boto3, mock_token, mock_ca, mock_k8s):
    """A different (cluster, region) key must not reuse the previous config."""
    _reset_cache()
    eks = _mock_eks(mock_boto3)

    loading_config("cluster-a", "us-east-1")
    loading_config("cluster-b", "us-east-1")

    assert eks.describe_cluster.call_count == 2
