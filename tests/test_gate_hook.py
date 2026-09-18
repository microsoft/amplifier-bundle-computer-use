"""Unit tests for the write-confirmation gate hook (`_make_gate_handler` in
hook-computer-use) - `docs/designs/remote-transport.md` \u00a710.4: "gate every
WRITE, or gate none". No real coordinator, no real backend, no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import asyncio

import amplifier_module_hook_computer_use as hook_mod


class _FakeBackend:
    name = "remote-ssh:macos"


class _FakeComputerTool:
    def __init__(self, gate_writes: bool) -> None:
        self._gate_writes = gate_writes
        self._backend = _FakeBackend()


class _FakeCoordinator:
    def __init__(self, tools: dict) -> None:
        self._tools = tools

    def get(self, mount_point, name=None):
        if mount_point != "tools":
            return None
        return self._tools.get(name) if name else self._tools


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_gate_asks_user_for_a_mutating_action_when_gate_writes_is_on(monkeypatch):
    """Only reaches `ask_user` when an interactive approval session is actually
    possible - see the EOF-avoidance tests below for the non-interactive path."""
    monkeypatch.setattr(hook_mod, "_interactive_approval_possible", lambda: True)
    coord = _FakeCoordinator({"computer": _FakeComputerTool(gate_writes=True)})
    handler = hook_mod._make_gate_handler(coord)

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "computer", "tool_input": {"action": "left_click"}},
        )
    )

    assert result.action == "ask_user"
    assert result.approval_default == "deny"


def test_gate_passes_through_reads_even_when_gate_writes_is_on():
    coord = _FakeCoordinator({"computer": _FakeComputerTool(gate_writes=True)})
    handler = hook_mod._make_gate_handler(coord)

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "computer", "tool_input": {"action": "screenshot"}},
        )
    )

    assert result.action == "continue"


def test_gate_off_never_asks():
    coord = _FakeCoordinator({"computer": _FakeComputerTool(gate_writes=False)})
    handler = hook_mod._make_gate_handler(coord)

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "computer", "tool_input": {"action": "left_click"}},
        )
    )

    assert result.action == "continue"


def test_gate_ignores_unrelated_tools():
    coord = _FakeCoordinator({"computer": _FakeComputerTool(gate_writes=True)})
    handler = hook_mod._make_gate_handler(coord)

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "read_file", "tool_input": {"action": "left_click"}},
        )
    )

    assert result.action == "continue"


def test_gate_handles_missing_tool_gracefully():
    coord = _FakeCoordinator({})
    handler = hook_mod._make_gate_handler(coord)

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "computer", "tool_input": {"action": "left_click"}},
        )
    )

    assert result.action == "continue"


def test_gate_applies_to_desktop_tool_mutating_actions_too(monkeypatch):
    monkeypatch.setattr(hook_mod, "_interactive_approval_possible", lambda: True)
    computer = _FakeComputerTool(gate_writes=True)

    class _FakeDesktopTool:
        _computer = computer

    coord = _FakeCoordinator({"computer": computer, "desktop": _FakeDesktopTool()})
    handler = hook_mod._make_gate_handler(coord)

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "desktop", "tool_input": {"action": "focus_window"}},
        )
    )

    assert result.action == "ask_user"


# -- EOF-avoidance (D-gate-EOF): no interactive session available ---------------
#
# Real incident this closes: `ask_user`'s approval prompt is answered by an
# app-layer ApprovalSystem this bundle does not own. On a backgrounded run with
# no TTY, that system's own `input()` hits immediate EOF and raises EOFError,
# which used to propagate uncaught all the way to the operator as
# "Tool computer failed: EOF when reading a line" - a message that names
# nothing. These tests prove the gate now decides BEFORE ever reaching that
# broken path, using `_interactive_approval_possible` as the seam (mocked here
# rather than actually manipulating sys.stdin).


def test_gate_denies_with_actionable_reason_when_no_interactive_session(monkeypatch):
    """The core EOF fix: no TTY -> a real `deny` with a real reason, never
    `ask_user` (which would crash into EOFError in the app-layer approval
    system this hook does not control)."""
    monkeypatch.setattr(hook_mod, "_interactive_approval_possible", lambda: False)
    coord = _FakeCoordinator({"computer": _FakeComputerTool(gate_writes=True)})
    handler = hook_mod._make_gate_handler(coord)

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "computer", "tool_input": {"action": "left_click"}},
        )
    )

    assert result.action == "deny"
    assert result.reason is not None
    # An agent/operator must be able to tell "needs approval" apart from
    # "no interactive session" apart from "how to proceed" without a human
    # debugging session - all three facts must be present in one string.
    assert "approval" in result.reason.lower()
    assert "interactive" in result.reason.lower() or "tty" in result.reason.lower()
    assert "unattended_writes_ok" in result.reason


def test_gate_unattended_writes_ok_allows_with_no_interactive_session(monkeypatch):
    """The explicit, logged opt-in: with unattended_writes_ok=True AND no TTY,
    the write proceeds - but ONLY because of the explicit config, never by
    default and never inferred."""
    monkeypatch.setattr(hook_mod, "_interactive_approval_possible", lambda: False)
    coord = _FakeCoordinator({"computer": _FakeComputerTool(gate_writes=True)})
    handler = hook_mod._make_gate_handler(coord, unattended_writes_ok=True)

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "computer", "tool_input": {"action": "left_click"}},
        )
    )

    assert result.action == "continue"


def test_gate_unattended_writes_ok_defaults_false(monkeypatch):
    """Never a default: constructing the handler with no explicit opt-in must
    still deny (not silently allow) when there is no interactive session."""
    monkeypatch.setattr(hook_mod, "_interactive_approval_possible", lambda: False)
    coord = _FakeCoordinator({"computer": _FakeComputerTool(gate_writes=True)})
    handler = hook_mod._make_gate_handler(coord)  # no unattended_writes_ok kwarg

    result = _run(
        handler(
            "tool:pre",
            {"tool_name": "computer", "tool_input": {"action": "left_click"}},
        )
    )

    assert result.action == "deny"


def test_interactive_approval_possible_reflects_stdin_isatty(monkeypatch):
    """The real (non-mocked) seam: a TTY reports True, a non-TTY reports
    False - both directions, so the heuristic itself is proven, not just its
    call sites."""
    monkeypatch.setattr(hook_mod.sys.stdin, "isatty", lambda: True)
    assert hook_mod._interactive_approval_possible() is True

    monkeypatch.setattr(hook_mod.sys.stdin, "isatty", lambda: False)
    assert hook_mod._interactive_approval_possible() is False


def test_interactive_approval_possible_false_when_isatty_raises(monkeypatch):
    """A closed/replaced stdin (some harnesses) must degrade to False, never
    optimistically to True."""

    def _boom():
        raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(hook_mod.sys.stdin, "isatty", _boom)
    assert hook_mod._interactive_approval_possible() is False


def test_host_approval_capability_reaches_ask_user_without_a_terminal(monkeypatch):
    monkeypatch.setattr(hook_mod, "_interactive_approval_possible", lambda: False)
    computer = _FakeComputerTool(gate_writes=True)
    coord = _FakeCoordinator({"computer": computer})
    coord.get_capability = lambda name: True if name == "approval.interactive" else None
    result = _run(
        hook_mod._make_gate_handler(coord)(
            "tool:pre",
            {"tool_name": "computer", "tool_input": {"action": "left_click"}},
        )
    )
    assert result.action == "ask_user"
    assert result.approval_default == "deny"
    assert computer._unattended_writes_ok is False


def test_unavailable_or_malformed_host_transport_never_uses_tty_fallback(monkeypatch):
    monkeypatch.setattr(hook_mod, "_interactive_approval_possible", lambda: True)
    coord = _FakeCoordinator({"computer": _FakeComputerTool(gate_writes=True)})
    for value in (False, "true", 1, {}, lambda: True):
        coord.get_capability = lambda _name, value=value: value
        result = _run(
            hook_mod._make_gate_handler(coord)(
                "tool:pre", {"tool_name": "computer", "tool_input": {"action": "type"}}
            )
        )
        assert result.action == "deny"

    def unavailable(_name):
        raise RuntimeError("transport unavailable")

    coord.get_capability = unavailable
    assert hook_mod._host_approval_possible(coord) is False
    coord.get_capability = lambda _name: None
    assert hook_mod._host_approval_possible(coord) is True
