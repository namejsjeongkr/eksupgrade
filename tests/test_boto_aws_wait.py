"""Test wait_for_ready is time-bounded.

The legacy loop polled instance status checks forever: an instance stuck in
'initializing'/'impaired' hung the whole upgrade with no way out.
"""

from unittest.mock import patch

import pytest

from eksupgrade.src.boto_aws import wait_for_ready

_BOTO = "eksupgrade.src.boto_aws.boto3"
_SLEEP = "eksupgrade.src.boto_aws.time.sleep"


def _status_response(status: str) -> dict:
    return {"InstanceStatuses": [{"InstanceStatus": {"Details": [{"Status": status}]}}]}


@patch(_SLEEP)
@patch(_BOTO)
def test_wait_for_ready_times_out_on_stuck_instance(mock_boto3, mock_sleep):
    """An instance that never passes health checks must raise, not hang forever."""
    ec2 = mock_boto3.client.return_value
    ec2.describe_instance_status.return_value = _status_response("initializing")

    with pytest.raises(Exception, match="did not pass"):
        wait_for_ready("i-stuck", "us-east-1", timeout=0)


@patch(_SLEEP)
@patch(_BOTO)
def test_wait_for_ready_returns_true_when_passed(mock_boto3, mock_sleep):
    ec2 = mock_boto3.client.return_value
    ec2.describe_instance_status.return_value = _status_response("passed")

    assert wait_for_ready("i-ok", "us-east-1") is True
