"""Unit tests for `type_pacing.py` (the measured masking-defect fix) and its
wiring into `ComputerTool._run`'s `type` action.

Evidence this fixes: a 202-character string typed via `MacOSBackend.type_text()`
completed in 0.07s - an inter-character gap (~0.35ms) 28x narrower than
`presence.GUARD_MS["macos"]` (10ms), making the presence detector
structurally blind for the whole operation. See `type_pacing.py`'s module
docstring for the full arithmetic.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use import ComputerTool
from amplifier_module_tool_computer_use.backend import BackendError, ScreenGeometry
from amplifier_module_tool_computer_use.coexistence_guard import CoexistenceGuard
from amplifier_module_tool_computer_use.presence import PresenceMonitor
from amplifier_module_tool_computer_use.type_pacing import (
    AUTO_PACING_MS,
    resolve_type_pacing_ms,
)

# -- pure resolution function -------------------------------------------------


def test_auto_with_guard_active_resolves_to_auto_pacing_ms():
    assert resolve_type_pacing_ms(None, guard_active=True) == AUTO_PACING_MS
    assert AUTO_PACING_MS == 25.0


def test_auto_without_guard_resolves_to_zero_full_speed():
    assert resolve_type_pacing_ms(None, guard_active=False) == 0.0


def test_explicit_override_with_guard_active_wins_in_both_directions():
    # Slower than auto.
    assert resolve_type_pacing_ms(100, guard_active=True) == 100.0
    # Faster than auto, but still nonzero.
    assert resolve_type_pacing_ms(5, guard_active=True) == 5.0


def test_explicit_zero_with_guard_active_forces_full_speed():
    assert resolve_type_pacing_ms(0, guard_active=True) == 0.0


def test_no_guard_active_means_zero_regardless_of_explicit_config():
    # No guard, nothing to pace against, and no cheap per-character path on
    # every backend - see the function's docstring. An explicit value is
    # moot when there is no guard to protect.
    assert resolve_type_pacing_ms(100, guard_active=False) == 0.0
    assert resolve_type_pacing_ms(0, guard_active=False) == 0.0


# -- config parsing / validation on ComputerTool ------------------------------


class _FakeBackend:
    name = "linux-x11"

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(800, 600, 0, 0)

    def list_monitors(self):
        raise BackendError("no monitor enumeration on this fake")

    def type_text(self, text, guard=None) -> None:
        self.calls.append(("type_text", text))

    def close(self) -> None:  # pragma: no cover
        pass


def test_type_pacing_ms_config_defaults_to_none_auto():
    computer = ComputerTool(_FakeBackend(), {})
    assert computer._type_pacing_ms is None


def test_type_pacing_ms_config_accepts_explicit_integer():
    computer = ComputerTool(_FakeBackend(), {"type_pacing_ms": 40})
    assert computer._type_pacing_ms == 40


def test_type_pacing_ms_config_accepts_explicit_zero():
    computer = ComputerTool(_FakeBackend(), {"type_pacing_ms": 0})
    assert computer._type_pacing_ms == 0


def test_type_pacing_ms_config_rejects_negative():
    with pytest.raises(ValueError):
        ComputerTool(_FakeBackend(), {"type_pacing_ms": -1})


def test_type_pacing_ms_config_rejects_non_numeric():
    with pytest.raises(ValueError):
        ComputerTool(_FakeBackend(), {"type_pacing_ms": "fast"})


# -- integration: `_run`'s `type` action actually paces when guard active ----


def _make_computer_with_real_guard(
    pacing_cfg: dict | None = None,
) -> tuple[ComputerTool, _FakeBackend, CoexistenceGuard]:
    backend = _FakeBackend()
    cfg = dict(pacing_cfg or {})
    computer = ComputerTool(backend, cfg)
    computer.resolve_display()
    idle_source = lambda: 999_999.0  # always long-idle -> never human_active
    presence = PresenceMonitor(idle_source=idle_source, platform="linux-x11")
    guard = CoexistenceGuard(presence=presence, release_all=lambda reason: [])
    computer._coexistence_guard = guard
    return computer, backend, guard


def test_auto_pacing_chunks_per_character_and_sleeps_between(monkeypatch):
    computer, backend, _guard = _make_computer_with_real_guard()
    sleeps: list[float] = []
    monkeypatch.setattr(
        "amplifier_module_tool_computer_use.time.sleep", lambda s: sleeps.append(s)
    )

    computer._run("type", {"text": "hi!"})

    # One `type_text` call per character (guard active, auto pacing).
    assert backend.calls == [("type_text", "h"), ("type_text", "i"), ("type_text", "!")]
    assert sleeps == [AUTO_PACING_MS / 1000.0] * 3


def test_explicit_zero_pacing_with_guard_active_types_full_speed_and_warns(
    monkeypatch, caplog
):
    computer, backend, _guard = _make_computer_with_real_guard({"type_pacing_ms": 0})
    sleeps: list[float] = []
    monkeypatch.setattr(
        "amplifier_module_tool_computer_use.time.sleep", lambda s: sleeps.append(s)
    )

    with caplog.at_level("WARNING"):
        computer._run("type", {"text": "hello"})

    # Full speed removes sleeps, never the per-character guard boundary.
    assert backend.calls == [("type_text", char) for char in "hello"]
    assert sleeps == []
    assert any("type_pacing_ms=0" in rec.message for rec in caplog.records)


def test_explicit_nonzero_override_with_guard_active(monkeypatch):
    computer, backend, _guard = _make_computer_with_real_guard({"type_pacing_ms": 5})
    sleeps: list[float] = []
    monkeypatch.setattr(
        "amplifier_module_tool_computer_use.time.sleep", lambda s: sleeps.append(s)
    )

    computer._run("type", {"text": "ab"})

    assert backend.calls == [("type_text", "a"), ("type_text", "b")]
    assert sleeps == [0.005, 0.005]


def test_no_guard_means_full_speed_regardless_of_explicit_config(monkeypatch):
    """An unattended machine with no presence source (no guard constructed at
    all) must never be slowed down - even if `type_pacing_ms` is explicitly
    configured, per `resolve_type_pacing_ms`'s contract."""
    backend = _FakeBackend()
    computer = ComputerTool(backend, {"type_pacing_ms": 200})
    computer.resolve_display()
    assert computer._coexistence_guard is None

    sleeps: list[float] = []
    monkeypatch.setattr(
        "amplifier_module_tool_computer_use.time.sleep", lambda s: sleeps.append(s)
    )

    computer._run("type", {"text": "unattended"})

    assert backend.calls == [("type_text", "unattended")]
    assert sleeps == []
