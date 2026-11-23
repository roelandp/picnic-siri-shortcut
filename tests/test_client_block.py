import pytest

pytest.importorskip("python_picnic_api2")

from picnic_service.client import is_block  # noqa: E402


class R:
    def __init__(self, status, content_type):
        self.status_code = status
        self.headers = {"Content-Type": content_type}


def test_block_detection():
    assert is_block(R(403, "text/html"))
    assert is_block(R(429, "application/json"))
    assert not is_block(R(403, "application/json"))
    assert not is_block(R(404, "text/html"))
    assert not is_block(R(200, "application/json"))
