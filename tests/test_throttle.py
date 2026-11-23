import pytest

pytest.importorskip("python_picnic_api2")

from picnic_service import client as client_mod  # noqa: E402
from picnic_service.client import HARD_MAX_PER_HOUR, RateLimitExceeded, Throttle  # noqa: E402


def test_limits_cannot_be_loosened(tmp_path):
    t = Throttle(tmp_path / "rl.json", min_interval=0.0, max_per_hour=10_000)
    assert t.min_interval == 1.0
    assert t.max_per_hour == HARD_MAX_PER_HOUR


def test_waits_between_calls_and_enforces_cap(tmp_path, monkeypatch):
    clock = {"now": 1_000_000.0}
    sleeps = []
    monkeypatch.setattr(client_mod.time, "time", lambda: clock["now"])

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(client_mod.time, "sleep", fake_sleep)
    t = Throttle(tmp_path / "rl.json", min_interval=1.0, max_per_hour=3)
    for _ in range(3):
        t.acquire()
    assert sleeps == [1.0, 1.0]
    with pytest.raises(RateLimitExceeded) as exc:
        t.acquire()
    assert 3590 < exc.value.retry_after <= 3600

    # venster wordt bewaard over instanties heen
    again = Throttle(tmp_path / "rl.json", min_interval=1.0, max_per_hour=3)
    with pytest.raises(RateLimitExceeded):
        again.acquire()
    assert oct((tmp_path / "rl.json").stat().st_mode & 0o777) == "0o600"
