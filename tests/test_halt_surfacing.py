"""Unit tests for defect 1 (halt surfacing) and defect 2 (durable
cross-session halt) as wired end-to-end through `ComputerTool.execute()`
(`tool-computer-use/__init__.py`) and the `tool:post` hook
(`hook-computer-use/__init__.py`).

Real evaluation evidence this closes
(`.amplifier/evaluation/computer-use/20260802T113341Z/s2-interrupt-halt/`):
a session halted five times over ~57s and the model's final report was
"Task completed successfully" with zero mention of any interruption; a
sibling/parent session then resumed writing automatically ~80s later with
no human ever choosing to resume anything.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import amplifier_module_hook_computer_use as hook_mod
from amplifier_module_tool_computer_use import ComputerTool
from amplifier_module_tool_computer_use.backend import (
    BackendError,
    ScreenGeometry,
    WindowList,
)
from amplifier_module_tool_computer_use.coexistence_guard import CoexistenceGuard
from amplifier_module_tool_computer_use.halt_state import load_halt
from amplifier_module_tool_computer_use.presence import (
    Confidence,
    PresenceMonitor,
    PresenceSnapshot,
    PresenceState,
)


def _run(coro):
    """Matches `test_screenshot_permissions.py`'s helper exactly, rather than
    using `asyncio.run()`: `asyncio.run()` clears the process-wide "current
    event loop" on exit (`asyncio.set_event_loop(None)`), which breaks OTHER
    test files in this suite still using the older
    `asyncio.get_event_loop().run_until_complete(...)` pattern if they run
    afterward in the same pytest session - a real, observed cross-file
    interaction, not a hypothetical one.
    """
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeBackend:
    name = "linux-x11"

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(800, 600, 0, 0)

    def list_monitors(self):
        raise BackendError("no monitor enumeration on this fake")

    def cursor_position(self):
        return (0, 0)

    def capture(self, region=None) -> bytes:
        # Minimal valid 2x2 PNG so `screenshot` can round-trip end to end.
        return bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000002000000020802000000fdd49a73"
            "0000001649444154789c63fccfc0c0c0c0c0c4c0c0c0c0c000000d1d01036ac29b"
            "e90000000049454e44ae426082"
        )

    def click(self, x, y, button="left", count=1) -> None:
        pass

    def type_text(self, text, guard=None) -> None:
        pass

    def list_windows(self) -> WindowList:
        return WindowList([], None)

    def close(self) -> None:  # pragma: no cover
        pass


def _halted_snapshot() -> PresenceSnapshot:
    return PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="idle_reconciliation",
        last_human_input_ago_ms=3618.0,
        margin_ms=3603.0,
        guard_ms=5.0,
        guard_measured=True,
        sample_interval_ms=60.0,
        latched_until_ms=None,
    )


def _make_halted_computer() -> ComputerTool:
    backend = _FakeBackend()
    computer = ComputerTool(backend, {})
    computer.resolve_display()
    # Long-idle fake, not 0.0: `before_event()` always takes one fresh live
    # presence sample even when already seeded halted (\u00a76.0), and since
    # defect 1's fix a first sample with a RECENT idle read is itself
    # classified HUMAN_ACTIVE (no injection history to reconcile against -
    # see `presence.py::_classify`). An idle_source stuck at 0.0 would keep
    # re-triggering that fresh-detection path and overwrite the seeded
    # snapshot under test with the live one on every call - unrelated to
    # what this test is verifying (halt-notice surfacing from a SEEDED
    # halt). A long idle read keeps the guard's own live sampling honestly
    # quiet, exactly like every other "seeded, not live-detected" fixture
    # in this suite.
    presence = PresenceMonitor(idle_source=lambda: 999_999.0, platform="linux-x11")
    guard = CoexistenceGuard(presence=presence, release_all=lambda reason: [])
    guard.seed_halted(_halted_snapshot())
    computer._coexistence_guard = guard  # type: ignore[assignment]
    return computer


# -- defect 1: halt_notices recorded + surfaced on execute() -----------------


def _patch_record_halt_state_dir(monkeypatch, tmp_path: Path) -> None:
    """`ComputerTool.execute()` calls the `record_halt` name it imported
    directly (`from .halt_state import record_halt`), which already bound
    `halt_state.DEFAULT_STATE_DIR`'s value as its `state_dir` default at
    import time - patching `halt_state.DEFAULT_STATE_DIR` afterward would
    not reach that already-bound default. Patch the imported name in the
    tool module's own namespace instead, redirecting to `tmp_path` so tests
    never touch a real `~/.amplifier/computer-use/halt/`.
    """
    import amplifier_module_tool_computer_use as tool_mod
    from amplifier_module_tool_computer_use import halt_state as halt_state_mod

    monkeypatch.setattr(halt_state_mod, "DEFAULT_STATE_DIR", tmp_path)
    monkeypatch.setattr(
        tool_mod,
        "record_halt",
        lambda platform, snapshot, *, reason: halt_state_mod.record_halt(
            platform, snapshot, reason=reason, state_dir=tmp_path
        ),
    )


def test_execute_records_a_halt_notice_and_still_reports_the_tool_error(
    tmp_path, monkeypatch
):
    _patch_record_halt_state_dir(monkeypatch, tmp_path)
    computer = _make_halted_computer()
    assert computer.halt_notices == []

    result = _run(computer.execute({"action": "left_click", "coordinate": [1, 1]}))

    assert result.success is False
    assert "halted" in (result.error or {}).get("message", "").lower()
    assert len(computer.halt_notices) == 1
    notice = computer.halt_notices[0]
    assert notice["action"] == "left_click"
    assert "halted" in notice["message"].lower()
    assert notice["margin_ms"] == 3603.0


def test_execute_persists_the_halt_durably(tmp_path, monkeypatch):
    _patch_record_halt_state_dir(monkeypatch, tmp_path)
    computer = _make_halted_computer()

    _run(computer.execute({"action": "left_click", "coordinate": [1, 1]}))

    record = load_halt("linux-x11", state_dir=tmp_path)
    assert record is not None
    assert "halted" in record.reason.lower()


def test_multiple_halts_accumulate_notices(tmp_path, monkeypatch):
    _patch_record_halt_state_dir(monkeypatch, tmp_path)
    computer = _make_halted_computer()

    _run(computer.execute({"action": "left_click", "coordinate": [1, 1]}))
    _run(computer.execute({"action": "key", "text": "a"}))

    assert len(computer.halt_notices) == 2
    assert computer.halt_notices[0]["action"] == "left_click"
    assert computer.halt_notices[1]["action"] == "key"


def test_read_only_screenshot_is_not_gated_by_halt_and_records_no_notice():
    """Reads must still pass through even while halted (\u00a76.0 only gates
    writes) - and must not spuriously add a halt notice of their own."""
    computer = _make_halted_computer()
    result = _run(computer.execute({"action": "screenshot"}))
    assert result.success is True
    assert computer.halt_notices == []


# -- defect 1: the hook surfaces halt_notices via inject_context -------------


class _FakeCoordinator:
    def __init__(self, tools: dict) -> None:
        self._tools = tools

    def get(self, mount_point, name=None):
        if mount_point != "tools":
            return None
        return self._tools.get(name) if name else self._tools


class _ToolWithNotices:
    def __init__(self, notices):
        self.halt_notices = notices


def test_halt_notice_handler_continues_when_no_notices():
    coord = _FakeCoordinator({"computer": _ToolWithNotices([])})
    handler = hook_mod._make_halt_notice_handler(coord)

    result = _run(handler("tool:post", {"tool_name": "computer", "tool_input": {}}))

    assert result.action == "continue"


def test_halt_notice_handler_injects_context_when_notices_present():
    notices = [
        {
            "at": 0.0,
            "action": "left_click",
            "message": "halted: a human at this machine produced input 12.0ms ago",
            "margin_ms": 30.0,
            "guard_ms": 5.0,
            "last_human_input_ago_ms": 12.0,
        }
    ]
    coord = _FakeCoordinator({"computer": _ToolWithNotices(notices)})
    handler = hook_mod._make_halt_notice_handler(coord)

    result = _run(handler("tool:post", {"tool_name": "computer", "tool_input": {}}))

    assert result.action == "inject_context"
    assert result.context_injection is not None
    assert "1 human-detected interruption" in result.context_injection
    assert "MUST explicitly acknowledge" in result.context_injection
    assert result.context_injection_role == "system"
    assert result.ephemeral is True


def test_halt_notice_handler_ignores_unrelated_tools():
    coord = _FakeCoordinator({"computer": _ToolWithNotices([{"message": "x"}])})
    handler = hook_mod._make_halt_notice_handler(coord)

    result = _run(handler("tool:post", {"tool_name": "read_file", "tool_input": {}}))

    assert result.action == "continue"


def test_halt_notice_handler_repeats_the_reminder_every_subsequent_call():
    """The reminder must reappear on every `tool:post` for the rest of the
    session once at least one halt occurred - not just the first time -
    since the model's final response may come many turns later."""
    notices = [{"message": "halted once"}]
    coord = _FakeCoordinator({"computer": _ToolWithNotices(notices)})
    handler = hook_mod._make_halt_notice_handler(coord)

    first = _run(handler("tool:post", {"tool_name": "computer", "tool_input": {}}))
    second = _run(handler("tool:post", {"tool_name": "computer", "tool_input": {}}))

    assert first.action == "inject_context"
    assert second.action == "inject_context"


def test_halt_notice_handler_wraps_injection_in_system_reminder():
    """The injected safety notice must be wrapped in <system-reminder source="...">
    tags per the ecosystem convention (see hooks-status-context /
    hooks-todo-reminder for the reference shape), so models can distinguish
    this system-injected content from an actual user request. The notice
    text itself must remain byte-identical inside the wrapper."""
    notices = [
        {
            "at": 0.0,
            "action": "left_click",
            "message": "halted: a human at this machine produced input 12.0ms ago",
            "margin_ms": 30.0,
            "guard_ms": 5.0,
            "last_human_input_ago_ms": 12.0,
        }
    ]
    coord = _FakeCoordinator({"computer": _ToolWithNotices(notices)})
    handler = hook_mod._make_halt_notice_handler(coord)

    result = _run(handler("tool:post", {"tool_name": "computer", "tool_input": {}}))

    assert result.action == "inject_context"
    assert result.context_injection is not None
    assert result.context_injection.startswith(
        '<system-reminder source="hook-computer-use">'
    ), (
        f"Injection should open with the system-reminder wrapper; got: {result.context_injection!r}"
    )
    assert result.context_injection.endswith("</system-reminder>"), (
        f"Injection should close with the system-reminder wrapper; got: {result.context_injection!r}"
    )
    assert (
        "SAFETY NOTICE (computer-use human/agent coexistence guard): "
        "1 human-detected interruption(s) occurred during this driving session"
    ) in result.context_injection
