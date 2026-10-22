"""Observed selection keeps canonical approval, freshness, and read-only gates."""

import asyncio
import io
import time
from types import SimpleNamespace

import pytest
from amplifier_module_tool_computer_use import ComputerTool
from amplifier_module_tool_computer_use.observed_control import (
    CURRENT_PROOF,
    ObservedControl,
)
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
async def test_expired_observation_unobserved_candidate_and_remote_target():
    tool, backend = setup()
    tool._observed_control.observations["proof"]["observation"]["created_at"] -= 20
    assert (await act(tool)).output["status"] == "stale"
    assert (
        await tool.execute(
            {"action": "act", "revision": "invented", "candidate": "click"}
        )
    ).output["status"] == "stale"
    assert not backend.calls


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
