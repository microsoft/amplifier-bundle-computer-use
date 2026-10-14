"""Gate policy is installed by pre; actual public executor owns each decision.

Former ask_user/pre-grant assertions now observe ApprovalSystem requests and
zero/allowed backend input instead: a pre result is not approval evidence.
"""

from __future__ import annotations

import asyncio
import copy

import pytest
from amplifier_core.approval import ApprovalTimeoutError
from test_neutral_computer import Backend, Coordinator, cu, run
from test_neutral_computer import hook as hook_mod


class Decisions:
    def __init__(self, selected="Allow"):
        self.selected = selected
        self.requests = []

    async def request_approval(self, prompt, options, timeout, default):
        self.requests.append((prompt, options, timeout, default))
        if isinstance(self.selected, Exception):
            raise self.selected
        return self.selected


def setup_gate(monkeypatch, *, interactive=True, unattended=False, gate=True):
    monkeypatch.setattr(hook_mod, "_interactive_approval_possible", lambda: interactive)
    computer = cu.ComputerTool(Backend(), {"gate_writes": gate, "max_edge": 800})
    computer.resolve_display()
    coordinator = Coordinator(computer, interactive)
    coordinator.tools["computer"] = computer
    coordinator.approval_system = Decisions()
    handler = hook_mod._make_gate_handler(coordinator, unattended_writes_ok=unattended)
    return computer, coordinator, handler


def install(handler, name="computer", payload=None):
    result = run(handler("tool:pre", {"tool_name": name, "tool_input": payload or {}}))
    assert result.action == "continue"
    return result


def test_gate_asks_user_for_a_mutating_action_when_gate_writes_is_on(monkeypatch):
    computer, coord, handler = setup_gate(monkeypatch)
    payload = {"action": "left_click", "coordinate": [20, 30]}
    install(handler, payload=payload)
    assert coord.approval_system.requests == []
    assert run(computer.execute(payload)).success
    prompt, options, timeout, default = coord.approval_system.requests[0]
    assert "left_click" in prompt and "neutral-fake" in prompt
    assert options == ["Allow", "Deny"]
    assert timeout == 300.0 and default == "deny"
    assert len(computer._backend.calls) == 1


def test_gate_passes_through_reads_even_when_gate_writes_is_on(monkeypatch):
    computer, coord, handler = setup_gate(monkeypatch)
    install(handler)
    assert run(computer.execute({"action": "cursor_position"})).success is False
    # This fake intentionally lacks cursor_position; it still never asks approval.
    assert coord.approval_system.requests == []


def test_gate_off_never_asks(monkeypatch):
    computer, coord, handler = setup_gate(monkeypatch, gate=False)
    install(handler)
    assert run(computer.execute({"action": "key", "text": "enter"})).success
    assert coord.approval_system.requests == []


def test_gate_ignores_unrelated_tools(monkeypatch):
    computer, _, handler = setup_gate(monkeypatch)
    install(handler, "read_file")
    assert computer._write_approval is None


def test_gate_handles_missing_tool_gracefully(monkeypatch):
    _, coord, handler = setup_gate(monkeypatch)
    coord.tools.clear()
    install(handler)


def test_gate_applies_to_desktop_tool_mutating_actions_too(monkeypatch):
    computer, coord, handler = setup_gate(monkeypatch)
    desktop = cu.DesktopTool(computer)
    coord.tools["desktop"] = desktop
    focused = []
    computer._backend.focus_window = focused.append
    computer._backend.list_windows = lambda: []
    install(handler, "desktop")
    assert run(desktop.execute({"action": "focus_window", "handle": "abc"})).success
    assert focused == ["abc"]
    assert len(coord.approval_system.requests) == 1


def test_gate_denies_with_actionable_reason_when_no_interactive_session(monkeypatch):
    computer, coord, handler = setup_gate(monkeypatch, interactive=False)
    install(handler)
    result = run(computer.execute({"action": "key", "text": "enter"}))
    assert not result.success
    assert all(
        word in result.error["message"]
        for word in ("approval", "interactive", "unattended_writes_ok")
    )
    assert not computer._backend.calls and not coord.approval_system.requests


def test_gate_unattended_writes_ok_allows_with_no_interactive_session(
    monkeypatch, caplog
):
    computer, coord, handler = setup_gate(
        monkeypatch, interactive=False, unattended=True
    )
    install(handler)
    assert run(computer.execute({"action": "key", "text": "enter"})).success
    assert computer._backend.calls == [("key", "enter")]
    assert not coord.approval_system.requests
    assert "NO human confirmation" in caplog.text


def test_gate_unattended_writes_ok_defaults_false(monkeypatch):
    test_gate_denies_with_actionable_reason_when_no_interactive_session(monkeypatch)


def test_interactive_approval_possible_reflects_stdin_isatty(monkeypatch):
    monkeypatch.setattr(hook_mod.sys.stdin, "isatty", lambda: True)
    assert hook_mod._interactive_approval_possible() is True
    monkeypatch.setattr(hook_mod.sys.stdin, "isatty", lambda: False)
    assert hook_mod._interactive_approval_possible() is False


def test_interactive_approval_possible_false_when_isatty_raises(monkeypatch):
    def boom():
        raise ValueError("closed")

    monkeypatch.setattr(hook_mod.sys.stdin, "isatty", boom)
    assert hook_mod._interactive_approval_possible() is False


def test_host_approval_capability_reaches_ask_user_without_a_terminal(monkeypatch):
    computer, coord, handler = setup_gate(monkeypatch)
    monkeypatch.setattr(hook_mod, "_interactive_approval_possible", lambda: False)
    install(handler)
    assert run(computer.execute({"action": "key", "text": "enter"})).success
    assert coord.approval_system.requests[0][3] == "deny"
    assert computer._unattended_writes_ok is False


def test_unavailable_or_malformed_host_transport_never_uses_tty_fallback(monkeypatch):
    computer, coord, handler = setup_gate(monkeypatch)
    install(handler)
    for value in (False, "true", 1, {}, lambda: True):
        coord.get_capability = lambda _name, value=value: value
        assert not run(computer.execute({"action": "key", "text": "enter"})).success
    assert not computer._backend.calls and not coord.approval_system.requests

    def unavailable(_name):
        raise RuntimeError("transport unavailable")

    coord.get_capability = unavailable
    assert hook_mod._host_approval_possible(coord) is False
    coord.get_capability = lambda _name: None
    assert hook_mod._host_approval_possible(coord) is True


@pytest.mark.parametrize(
    "selected",
    [
        "Deny",
        "deny",
        "unknown",
        "",
        None,
        True,
        EOFError("EOF"),
        TimeoutError("timeout"),
        ApprovalTimeoutError("public approval timeout"),
    ],
)
def test_denial_default_unknown_error_and_timeout_are_zero_input(monkeypatch, selected):
    computer, coord, handler = setup_gate(monkeypatch)
    coord.approval_system.selected = selected
    install(handler)
    disclosed = []
    computer._ensure_announced = lambda: disclosed.append(True)
    assert not run(computer.execute({"action": "key", "text": "enter"})).success
    assert not computer._backend.calls and not disclosed
    coord.approval_system.selected = "Allow"
    assert run(computer.execute({"action": "key", "text": "later"})).success
    assert computer._backend.calls == [("key", "later")]


def test_missing_public_approval_system_fails_closed(monkeypatch):
    computer, coord, handler = setup_gate(monkeypatch)
    del coord.approval_system
    install(handler)
    assert not run(computer.execute({"action": "key", "text": "enter"})).success
    assert not computer._backend.calls


@pytest.mark.parametrize("shape", ["flat", "batch"])
@pytest.mark.parametrize("same", [False, True])
@pytest.mark.parametrize("opposite", [False, True])
def test_overlapping_calls_have_independent_decisions(
    monkeypatch, shape, same, opposite
):
    computer, coord, handler = setup_gate(monkeypatch)

    async def scenario():
        started = asyncio.Event()
        releases = [asyncio.Future(), asyncio.Future()]
        requests = []

        class Pending:
            async def request_approval(self, prompt, options, timeout, default):
                index = len(requests)
                requests.append(prompt)
                if len(requests) == 2:
                    started.set()
                return await releases[index]

        coord.approval_system = Pending()
        a = {"action": "key", "text": "A"}
        b = {"action": "key", "text": "A" if same else "B"}
        if shape == "batch":
            a = {"version": 1, "actions": [a], "capture_after": False}
            b = {"version": 1, "actions": [b], "capture_after": False}
        originals = copy.deepcopy([a, b])
        install_data = {"tool_name": "computer", "tool_input": a}
        await handler("tool:pre", install_data)
        task_a = asyncio.create_task(computer.execute(a))
        await handler("tool:pre", {**install_data, "tool_input": b})
        task_b = asyncio.create_task(computer.execute(b))
        await asyncio.wait_for(started.wait(), 2)
        assert not computer._backend.calls
        if opposite:
            releases[1].set_result("Deny")
            assert not (await task_b).success
            releases[0].set_result("Allow")
        else:
            releases[0].set_result("Allow")
            assert (await task_a).success
            releases[1].set_result("Deny")
        ra, rb = await asyncio.gather(task_a, task_b)
        assert ra.success and not rb.success
        assert computer._backend.calls == [("key", "A")]
        assert [a, b] == originals

    run(scenario())


@pytest.mark.parametrize("shape", ["flat", "batch"])
@pytest.mark.parametrize("same", [False, True])
def test_cancelled_overlap_never_steals_allowed_call(monkeypatch, shape, same):
    computer, coord, handler = setup_gate(monkeypatch)

    async def scenario():
        ready = asyncio.Event()
        futures = [asyncio.Future(), asyncio.Future()]
        prompts = []

        class Pending:
            async def request_approval(self, prompt, options, timeout, default):
                index = len(prompts)
                prompts.append(prompt)
                if len(prompts) == 2:
                    ready.set()
                return await futures[index]

        coord.approval_system = Pending()
        await handler("tool:pre", {"tool_name": "computer"})
        a = {"action": "key", "text": "A"}
        b = {"action": "key", "text": "A" if same else "B"}
        if shape == "batch":
            a = {"version": 1, "actions": [a], "capture_after": False}
            b = {"version": 1, "actions": [b], "capture_after": False}
        task_a = asyncio.create_task(computer.execute(a))
        task_b = asyncio.create_task(computer.execute(b))
        await asyncio.wait_for(ready.wait(), 2)
        task_b.cancel()
        if shape == "flat":
            with pytest.raises(asyncio.CancelledError):
                await task_b
        else:
            result = await task_b
            assert not result.success and result.error["type"] == "CancelledError"
        assert not computer._backend.calls
        futures[0].set_result("Allow")
        assert (await task_a).success
        assert computer._backend.calls == [("key", "A")]
        coord.approval_system = Decisions("Deny")
        assert not (await computer.execute(a)).success
        assert computer._backend.calls == [("key", "A")]

    run(scenario())


@pytest.mark.parametrize("shape", ["flat", "batch"])
def test_no_hook_computer_write_fails_closed(monkeypatch, shape):
    computer, _, _ = setup_gate(monkeypatch)
    payload = {"action": "key", "text": "enter"}
    if shape == "batch":
        payload = {"version": 1, "actions": [payload], "capture_after": False}
    result = run(computer.execute(payload))
    assert not result.success and "gate_writes" in result.error["message"]
    assert not computer._backend.calls
