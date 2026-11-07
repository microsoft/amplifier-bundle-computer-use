"""Observed selection keeps canonical approval, freshness, and read-only gates."""

import asyncio
import io
import threading
import time
from types import SimpleNamespace

import pytest
from amplifier_module_tool_computer_use import ComputerTool
from amplifier_module_tool_computer_use.coexistence_guard import CoexistenceGuard
from amplifier_module_tool_computer_use.observed_control import (
    CURRENT_PROOF,
    ObservedControl,
)
from amplifier_module_tool_computer_use.presence import PresenceMonitor
from PIL import Image
from test_computer_tool_coexistence import _FakeBackend


class Backend(_FakeBackend):
    def __init__(self):
        super().__init__()
        self.color = "white"

    def capture(self, region=None):
        buf = io.BytesIO()
        Image.new("RGB", (800, 600), self.color).save(buf, format="PNG")
        return buf.getvalue()


def setup(config=None, *, backend=None):
    backend = backend or Backend()
    tool = ComputerTool(backend, {"gate_writes": True, **(config or {})})
    tool.resolve_display()
    tool._ensure_announced()
    tool._observed_control = ObservedControl(tool)
    # Supply observed candidates independently of OCR recognition quality.
    raw, digest, w, h = tool._observed_control.capture(tool._binding)
    observed = {
        "revision": "proof",
        "state": {"elements": [{"id": "button", "rect": [10, 20, 100, 30]}]},
        "candidates": [{"id": "click", "operation": "CLICK", "target": "button"}],
        "created_at": time.monotonic(),
    }
    tool._observed_control.observations["proof"] = {
        "observation": observed,
        "binding": tool._binding,
        "digest": digest,
    }
    return tool, backend


async def act(tool):
    return await tool.execute(
        {"action": "act", "revision": "proof", "candidate": "click"}
    )


@pytest.mark.asyncio
async def test_exact_canonical_action_is_approved_before_click():
    tool, backend = setup()
    seen = []

    async def approve(prepared, binding):
        seen.append(prepared)
        return True

    tool._write_approval = approve
    result = await act(tool)
    assert result.success and result.output["status"] == "completed"
    assert seen == [{"action": "left_click", "coordinate": [60, 35]}]
    assert backend.calls == [("click", 60, 35, "left", 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["denied", "read_only", "missing_approval"])
async def test_existing_gate_prevents_observed_write(mode):
    tool, backend = setup({"read_only": mode == "read_only"})
    if mode != "missing_approval":

        async def approve(prepared, binding):
            return mode != "denied"

        tool._write_approval = approve
    result = await act(tool)
    assert result.output["status"] == "denied" and not backend.calls


@pytest.mark.asyncio
async def test_screen_change_during_approval_stops_before_input():
    tool, backend = setup()

    async def approve(prepared, binding):
        backend.color = "black"
        return True

    tool._write_approval = approve
    result = await act(tool)
    assert result.output["status"] == "stale" and not backend.calls


@pytest.mark.asyncio
async def test_presence_wait_precedes_final_observation_check():
    tool, backend = setup()

    class Guard:
        def check_start_permission(self):
            pass

        def bind_target(self):
            pass

        def before_event(self, **kw):
            backend.color = "black"

        def after_event(self):
            pass

        def release_target(self):
            pass

    tool._coexistence_guard = Guard()
    # Policy installation replaces the binding; re-observe that intended target.
    tool._observed_control.observations["proof"]["binding"] = tool._binding

    async def approve(prepared, binding):
        return True

    tool._write_approval = approve
    result = await act(tool)
    assert result.output["status"] == "stale" and not backend.calls


@pytest.mark.asyncio
async def test_unobserved_candidate_stays_refused():
    tool, backend = setup()
    assert (
        await tool.execute(
            {"action": "act", "revision": "invented", "candidate": "click"}
        )
    ).output["status"] == "stale"
    assert not backend.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [20, 3600])
async def test_older_selection_uses_fresh_pixels_and_one_canonical_approval(age):
    tool, backend = setup()
    control = tool._observed_control
    control.observations["proof"]["observation"]["created_at"] -= age
    seen = []

    def no_new_selection(*args):
        raise AssertionError("fresh validation must not repeat OCR selection")

    control.observe = no_new_selection

    async def approve(prepared, binding):
        seen.append(prepared)
        return True

    tool._write_approval = approve
    assert (await act(tool)).output["status"] == "completed"
    assert seen == [{"action": "left_click", "coordinate": [60, 35]}]
    assert backend.calls == [("click", 60, 35, "left", 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["screen", "target", "read_only", "denied"])
async def test_older_selection_does_not_renew_authority_or_changed_target(change):
    tool, backend = setup()
    tool._observed_control.observations["proof"]["observation"]["created_at"] -= 3600
    if change == "screen":
        backend.color = "black"
    elif change == "target":
        tool._backend = Backend()  # Identical pixels on a different target.
    elif change == "read_only":
        tool._read_only = True

    async def approve(prepared, binding):
        return change != "denied"

    tool._write_approval = approve
    assert (await act(tool)).output["status"] == (
        "denied" if change == "denied" else "stale"
    )
    assert not backend.calls and not tool._backend.calls


@pytest.mark.asyncio
async def test_target_replacement_during_fresh_read_refuses_before_approval():
    tool, backend = setup()
    capture = backend.capture

    def replaced_capture(region=None):
        result = capture(region)
        tool._backend = Backend()
        return result

    backend.capture = replaced_capture
    tool._observed_control.observations["proof"]["observation"]["created_at"] -= 3600

    async def no_approval(prepared, binding):
        raise AssertionError("changed observation cannot reach approval")

    tool._write_approval = no_approval
    assert (await act(tool)).output["status"] == "stale"
    assert not backend.calls and not tool._backend.calls


@pytest.mark.asyncio
async def test_cancel_during_fresh_read_never_dispatches_input():
    tool, backend = setup()
    tool._observed_control.observations["proof"]["observation"]["created_at"] -= 3600
    entered, release, finished = (threading.Event() for _ in range(3))
    capture = backend.capture

    def waiting_capture(region=None):
        entered.set()
        assert release.wait(2), "test failed to release capture"
        try:
            return capture(region)
        finally:
            finished.set()

    backend.capture = waiting_capture
    task = asyncio.create_task(act(tool))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    assert not backend.calls


@pytest.mark.asyncio
async def test_older_selection_does_not_replay_unknown_input():
    tool, backend = setup({"gate_writes": False})
    tool._observed_control.observations["proof"]["observation"]["created_at"] -= 3600
    click = backend.click

    def lost_reply(*args, **kwargs):
        click(*args, **kwargs)
        raise RuntimeError("input reply lost")

    backend.click = lost_reply
    assert (await act(tool)).output["status"] == "unknown"
    assert backend.calls == [("click", 60, 35, "left", 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["human", "pause", "halt", "unreadable"])
async def test_fresh_pixels_cannot_clear_existing_presence_controls(
    control, monkeypatch, tmp_path
):
    import amplifier_module_tool_computer_use as cu
    from amplifier_module_tool_computer_use import halt_state

    monkeypatch.setattr(
        cu,
        "record_halt",
        lambda platform, snapshot, *, reason: halt_state.record_halt(
            platform, snapshot, reason=reason, state_dir=tmp_path / "halt"
        ),
    )
    tool, backend = setup({"gate_writes": False})

    def idle():
        if control == "unreadable":
            raise OSError("presence unavailable")
        return 0 if control in {"human", "halt"} else 999_999

    monitor = PresenceMonitor(idle_source=idle, platform="linux-x11")
    guard = CoexistenceGuard(presence=monitor, release_all=lambda reason: [])
    if control == "pause":
        guard.pause.set("overlay_click", reason="user stopped input")
    elif control == "halt":
        guard.seed_halted(monitor.sample())
    tool._coexistence_guard = guard
    saved = tool._observed_control.observations["proof"]
    saved["binding"] = tool._binding
    saved["observation"]["created_at"] -= 3600
    assert (await act(tool)).output["status"] == "unknown"
    assert not backend.calls
    assert control not in {"human", "halt"} or guard._halted


def test_extension_is_absent_by_default():
    tool = ComputerTool(Backend(), {})
    assert "observe" not in tool.input_schema["properties"]["action"]["enum"]
    assert tool._observed_control is None


@pytest.mark.asyncio
async def test_approval_reading_time_keeps_exact_unchanged_target():
    tool, backend = setup()

    async def approve(prepared, binding):
        tool._observed_control.observations["proof"]["observation"]["created_at"] -= 20
        return True

    tool._write_approval = approve
    assert (await act(tool)).output["status"] == "completed"
    assert backend.calls == [("click", 60, 35, "left", 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate,canonical",
    [
        (
            {"id": "key", "operation": "KEY", "key": "Tab"},
            {"action": "key", "text": "Tab"},
        ),
        (
            {"id": "text", "operation": "TYPE_TEXT"},
            {"action": "type", "text": "Example"},
        ),
        (
            {"id": "scroll", "operation": "SCROLL", "direction": "down"},
            {"action": "scroll", "scroll_direction": "down", "scroll_amount": 3},
        ),
    ],
)
async def test_observed_keyboard_and_scroll_keep_canonical_approval(
    candidate, canonical
):
    tool, backend = setup()
    seen = []
    tool._observed_control.observations["proof"]["observation"]["candidates"] = [
        candidate
    ]

    async def deny(prepared, binding):
        seen.append(prepared)
        return False

    tool._write_approval = deny
    result = await tool.execute(
        {
            "action": "act",
            "revision": "proof",
            "candidate": candidate["id"],
            "text": "Example",
        }
    )
    assert (
        result.output["status"] == "denied"
        and seen == [canonical]
        and not backend.calls
    )


class TypingBackend(Backend):
    """Fake only input, retaining the local backend's per-character guard order."""

    def type_text(self, text, guard=None):
        for char in text:
            if guard is not None:
                guard.before_event()
            self.calls.append(("character", char))
            # Successful input changes the captured screen, as a real field does.
            self.color = "black"
            if guard is not None:
                guard.after_event()


class TypingGuard:
    def __init__(self):
        self.events = []
        self.on_before = lambda: None
        self.presence = SimpleNamespace(guard_ms=10)

    def check_start_permission(self):
        self.events.append("start")

    def bind_target(self):
        self.events.append("bind")

    def before_event(self, **kwargs):
        self.events.append("before")
        self.on_before()

    def after_event(self):
        self.events.append("after")

    def release_target(self):
        self.events.append("release")


def setup_typing(*, guarded, pacing=1):
    tool, backend = setup({"type_pacing_ms": pacing}, backend=TypingBackend())
    guard = TypingGuard() if guarded else None
    tool._coexistence_guard = guard
    saved = tool._observed_control.observations["proof"]
    saved["binding"] = tool._binding
    saved["observation"]["candidates"] = [{"id": "text", "operation": "TYPE_TEXT"}]
    return tool, backend, guard


async def type_observed(tool):
    return await tool.execute(
        {"action": "act", "revision": "proof", "candidate": "text", "text": "Ada"}
    )


def change_typing_authority(change, tool, backend):
    if change == "screen":
        backend.color = "black"
    elif change == "revoked":
        CURRENT_PROOF.get()["active"] = False
    elif change == "retarget":
        tool._backend = TypingBackend()


@pytest.mark.asyncio
@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize("change", ["screen", "revoked", "retarget", "unchanged"])
async def test_observed_typing_rechecks_after_delayed_approval(guarded, change):
    tool, backend, guard = setup_typing(guarded=guarded)
    waiting, resume = asyncio.Event(), asyncio.Event()
    seen = []

    async def approve(prepared, binding):
        seen.append(prepared)
        waiting.set()
        await resume.wait()
        # An initially fresh observation may survive a long human reading pause.
        tool._observed_control.observations["proof"]["observation"]["created_at"] -= 20
        change_typing_authority(change, tool, backend)
        return True

    tool._write_approval = approve
    task = asyncio.create_task(type_observed(tool))
    await asyncio.wait_for(waiting.wait(), timeout=2)
    assert not backend.calls and not task.done()
    resume.set()
    result = await asyncio.wait_for(task, timeout=2)
    assert seen == [{"action": "type", "text": "Ada"}]
    if change == "unchanged":
        assert result.output["status"] == "completed"
        assert backend.calls == [("character", char) for char in "Ada"]
        if guarded:
            assert guard.events == ["start", "bind"] + ["before", "after"] * 3 + [
                "release"
            ]
    else:
        assert result.output["status"] == (
            "unknown" if change == "retarget" else "stale"
        )
        assert not backend.calls and not tool._backend.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["screen", "revoked", "retarget"])
async def test_observed_typing_rechecks_after_first_presence_wait(change):
    tool, backend, guard = setup_typing(guarded=True)
    guard.on_before = lambda: change_typing_authority(change, tool, backend)

    async def approve(prepared, binding):
        return True

    tool._write_approval = approve
    result = await type_observed(tool)
    assert result.output["status"] == "stale"
    assert not backend.calls and not tool._backend.calls
    assert guard.events == ["start", "bind", "before", "release"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pacing", [0, 1])
async def test_observed_typing_allows_its_own_screen_changes(pacing):
    tool, backend, guard = setup_typing(guarded=True, pacing=pacing)

    async def approve(prepared, binding):
        return True

    tool._write_approval = approve
    result = await type_observed(tool)
    assert result.output["status"] == "completed"
    assert backend.calls == [("character", char) for char in "Ada"]
    assert guard.events == ["start", "bind"] + ["before", "after"] * 3 + ["release"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "retarget", "presence"])
@pytest.mark.parametrize("pacing", [0, 1])
async def test_observed_typing_stops_between_characters_without_replaying(
    change, pacing
):
    tool, backend, guard = setup_typing(guarded=True, pacing=pacing)

    def interrupt_after_first_character():
        if backend.calls:
            if change == "presence":
                raise RuntimeError("human presence interrupted typing")
            change_typing_authority(change, tool, backend)

    guard.on_before = interrupt_after_first_character

    async def approve(prepared, binding):
        return True

    tool._write_approval = approve
    result = await type_observed(tool)
    assert result.output["status"] == "unknown"
    assert backend.calls == [("character", "A")]
    assert guard.events == ["start", "bind", "before", "after", "before", "release"]
    if change == "retarget":
        assert not tool._backend.calls
