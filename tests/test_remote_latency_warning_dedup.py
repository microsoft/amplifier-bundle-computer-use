"""Offline coverage for remote transport reporting.

Remote guard construction is intentionally quiet. A warning is emitted only
when an actual successful presence sample measures transport strictly above
`REMOTE_TRANSPORT_WARNING_MS`, and is deduplicated per physical channel.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use import (
    REMOTE_TRANSPORT_WARNING_MS,
    _build_coexistence_guard,
)
from amplifier_module_tool_computer_use.coexistence_guard import HaltedError
from amplifier_module_tool_computer_use.presence import (
    Confidence,
    IdleUnreadableError,
    PresenceSnapshot,
    PresenceState,
)


class _FakeRemoteBackend:
    """Remote-shaped backend with a counted, entirely offline idle read."""

    is_remote = True

    def __init__(self, remote_platform: str, user_host: str) -> None:
        self.name = f"remote-ssh:{remote_platform}"
        self.presence_platform = remote_platform
        self.user_host = user_host
        self.idle_reads = 0

    def presence_idle_ms(self) -> float:
        self.idle_reads += 1
        return 999_999.0


class _FakeLocalBackend:
    name = "linux-x11"

    def presence_idle_ms(self) -> float:
        return 999_999.0


def _sample(latency_ms: float) -> PresenceSnapshot:
    return PresenceSnapshot(
        state=PresenceState.QUIET,
        confidence=Confidence.HIGH,
        basis="idle_reconciliation",
        last_human_input_ago_ms=999_999.0,
        margin_ms=None,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=None,
        latched_until_ms=None,
        transport_latency_ms=latency_ms,
    )


def _transport_warnings(records) -> list:
    return [r for r in records if "remote presence sample" in r.message]


def _sample_guard(guard, latency_ms: float) -> None:
    guard.presence.sample = lambda: _sample(latency_ms)  # type: ignore[method-assign]
    guard.before_event()


def test_remote_guard_construction_is_quiet_and_does_not_read_presence(caplog):
    caplog.set_level(logging.WARNING, logger="amplifier_module_tool_computer_use")
    backend = _FakeRemoteBackend("macos", "a-user@example-macbook")

    guard = _build_coexistence_guard(backend, {})

    assert guard is not None
    assert backend.idle_reads == 0
    assert _transport_warnings(caplog.records) == []


@pytest.mark.parametrize("latency_ms", [800.0, 1500.0, REMOTE_TRANSPORT_WARNING_MS])
def test_remote_samples_at_or_below_threshold_are_quiet(caplog, latency_ms):
    caplog.set_level(logging.WARNING, logger="amplifier_module_tool_computer_use")
    guard = _build_coexistence_guard(
        _FakeRemoteBackend("macos", f"quiet-{latency_ms}@example-macbook"), {}
    )
    assert guard is not None

    _sample_guard(guard, latency_ms)

    assert _transport_warnings(caplog.records) == []


def test_first_over_threshold_remote_sample_warns_once_across_same_channel(
    caplog, monkeypatch
):
    """The first warning comes from the real `PresenceMonitor.sample()` path,
    not from a construction-time estimate or a separately polled transport."""
    import amplifier_module_tool_computer_use.presence as presence_module

    caplog.set_level(logging.WARNING, logger="amplifier_module_tool_computer_use")
    root_backend = _FakeRemoteBackend("macos", "a-user@example-macbook")
    root_guard = _build_coexistence_guard(root_backend, {})
    child_guard = _build_coexistence_guard(
        _FakeRemoteBackend("macos", "a-user@example-macbook"), {}
    )
    assert root_guard is not None
    assert child_guard is not None
    timestamps = iter([100.0, 102.000001])
    monkeypatch.setattr(presence_module.time, "monotonic", lambda: next(timestamps))

    root_guard.before_event()
    _sample_guard(child_guard, REMOTE_TRANSPORT_WARNING_MS + 250.0)

    assert root_backend.idle_reads == 1
    hits = _transport_warnings(caplog.records)
    assert len(hits) == 1
    assert "2000.001ms" in hits[0].message
    assert "reporting only" in hits[0].message


def test_human_after_own_injection_still_halts_on_slow_transport(caplog):
    caplog.set_level(logging.WARNING, logger="amplifier_module_tool_computer_use")
    guard = _build_coexistence_guard(
        _FakeRemoteBackend("macos", "human-after-write@example-macbook"), {}
    )
    assert guard is not None
    guard.after_event()
    guard.presence.sample = lambda: PresenceSnapshot(  # type: ignore[method-assign]
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="idle_reconciliation",
        last_human_input_ago_ms=12.0,
        margin_ms=30.0,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=60.0,
        latched_until_ms=None,
        transport_latency_ms=REMOTE_TRANSPORT_WARNING_MS + 1.0,
    )

    with pytest.raises(HaltedError):
        guard.before_event()

    assert guard.halted is True
    assert len(_transport_warnings(caplog.records)) == 1


def test_over_threshold_remote_samples_warn_independently_per_channel(caplog):
    caplog.set_level(logging.WARNING, logger="amplifier_module_tool_computer_use")
    mac_guard = _build_coexistence_guard(
        _FakeRemoteBackend("macos", "a-user@example-macbook"), {}
    )
    windows_guard = _build_coexistence_guard(
        _FakeRemoteBackend("windows-wsl2", "a-user@example-desktop"), {}
    )
    assert mac_guard is not None
    assert windows_guard is not None

    _sample_guard(mac_guard, REMOTE_TRANSPORT_WARNING_MS + 1.0)
    _sample_guard(windows_guard, REMOTE_TRANSPORT_WARNING_MS + 1.0)

    assert len(_transport_warnings(caplog.records)) == 2


def test_failed_remote_idle_read_uses_existing_hard_failure_without_warning(caplog):
    caplog.set_level(logging.WARNING, logger="amplifier_module_tool_computer_use")
    guard = _build_coexistence_guard(
        _FakeRemoteBackend("macos", "failed-read@example-macbook"), {}
    )
    assert guard is not None

    def _fail() -> PresenceSnapshot:
        raise IdleUnreadableError("simulated failed remote read")

    guard.presence.sample = _fail  # type: ignore[method-assign]
    with pytest.raises(IdleUnreadableError):
        guard.before_event()

    assert _transport_warnings(caplog.records) == []


def test_local_guard_has_no_transport_observer():
    guard = _build_coexistence_guard(_FakeLocalBackend(), {})

    assert guard is not None
    assert guard.on_presence_sample is None
