"""Neutral v1 boundary: headless real executor, fake desktop, no model transport."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from amplifier_core.coordinator import ModuleCoordinator
from amplifier_core.message_models import ToolSpec
from jsonschema import ValidationError, validate
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))
import amplifier_module_hook_computer_use as hook
import amplifier_module_tool_computer_use as cu
from amplifier_module_tool_computer_use.backend import BackendError, ScreenGeometry
from amplifier_module_tool_computer_use.coexistence_guard import HaltedError
from amplifier_module_tool_computer_use.presence import (
    Confidence,
    PresenceSnapshot,
    PresenceState,
)


def run(coro):
    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        return runner.run(coro)


class Backend:
    name = "neutral-fake"
    is_remote = False

    def __init__(self):
        self.calls = []
        self.on_input = None
        self.geometry_reads = 0

    def screen_geometry(self):
        self.geometry_reads += 1
        return ScreenGeometry(1600, 1200, -1600, 30)

    def list_monitors(self):
        raise BackendError("fake has no monitor enumeration")

    def capture(self, region=None):
        self.calls.append(("capture", region))
        size = (
            (1600, 1200)
            if region is None
            else (region[2] - region[0], region[3] - region[1])
        )
        buf = io.BytesIO()
        Image.new("RGB", size, color="navy").save(buf, format="PNG")
        return buf.getvalue()

    def click(self, x, y, button, count):
        self.calls.append(("click", x, y, button, count))
        if self.on_input:
            self.on_input()

    def move(self, x, y):
        self.calls.append(("move", x, y))
        if self.on_input:
            self.on_input()

    def key(self, text):
        self.calls.append(("key", text))
        if self.on_input:
            self.on_input()

    def type_text(self, text):
        self.calls.append(("type", text))

    def scroll(self, x, y, direction, amount):
        self.calls.append(("scroll", direction, amount))

    def hold_key(self, text, duration):
        self.calls.append(("hold_key", text, duration))

    def close(self):
        pass


@pytest.fixture
def computer(tmp_path, monkeypatch):
    monkeypatch.setattr(cu, "SHOT_DIR", tmp_path / "shots")
    tool = cu.ComputerTool(Backend(), {"max_edge": 800})
    tool.resolve_display()
    return tool


def batch(*actions, capture=True):
    return {"version": 1, "actions": list(actions), "capture_after": capture}


CLICK = {"action": "left_click", "coordinate": [20, 30]}


@pytest.mark.parametrize(
    "payload",
    [
        CLICK,
        {"action": "screenshot"},
        {"action": "key", "text": "down", "repeat": 2},
        {"action": "scroll", "direction": "down", "amount": 0},
        {"action": "zoom", "region": [0, 0, 30, 40]},
        batch(CLICK),
        batch(CLICK, capture=False),
    ],
)
def test_public_schema_accepts_flat_aliases_and_complete_batches(computer, payload):
    original = copy.deepcopy(payload)
    spec = ToolSpec(
        name="my_desktop",
        description=computer.description,
        parameters=computer.input_schema,
        **computer.native_tool_spec,
    )
    validate(payload, spec.model_dump()["parameters"])
    result = run(computer.execute(payload))
    assert result.success, result.error
    assert computer._backend.calls
    assert payload == original


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": "missing action"},
        *[
            {key: value for key, value in batch(CLICK).items() if key != missing}
            for missing in ("version", "actions", "capture_after")
        ],
        *[{key: batch(CLICK)[key]} for key in ("version", "actions", "capture_after")],
        *[
            {"action": "screenshot", key: value}
            for key, values in (
                ("version", (1, None, "malformed")),
                ("actions", ([CLICK], None, [])),
                ("capture_after", (True, False, None)),
            )
            for value in values
        ],
        {**batch(CLICK), "action": "screenshot"},
        {**batch(CLICK), "text": "flat-only root key"},
        {**batch(CLICK), "unexpected": None},
        {**batch(CLICK), "version": 2},
        {**batch(CLICK), "actions": []},
        {**batch(CLICK), "actions": [{}]},
        {**batch(CLICK), "capture_after": 1},
    ],
)
def test_runtime_rejects_empty_mixed_incomplete_and_extra_roots(computer, payload):
    # Root-shape enforcement belongs to runtime, even if a schema also rejects it.
    # Do not require future schemas to accept these malformed combinations.
    original = copy.deepcopy(payload)
    disclosed = []
    computer._ensure_announced = lambda: disclosed.append(True)
    result = run(computer.execute(payload))
    assert not result.success
    assert computer._backend.calls == []
    assert disclosed == []
    assert payload == original


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "screenshot", "version": None},
        {"action": "screenshot", "version": "malformed"},
        {"action": "screenshot", "actions": None},
        {"action": "screenshot", "actions": []},
        {"action": "screenshot", "capture_after": None},
        {**batch(CLICK), "version": 2},
        {**batch(CLICK), "actions": []},
        {**batch(CLICK), "actions": [{}]},
        {**batch(CLICK), "capture_after": 1},
    ],
)
def test_public_schema_retains_type_const_and_nested_item_constraints(
    computer, payload
):
    with pytest.raises(ValidationError):
        validate(payload, computer.input_schema)


async def core_coordinator(computer, provider=None, *, max_inline=3):
    """Real Core mounts/registry; only the platform and HTTP are test doubles."""
    coordinator = ModuleCoordinator()
    await coordinator.mount("tools", computer, name="my_desktop")
    if provider is not None:
        await coordinator.mount("providers", provider, name="test-provider")
    await hook.mount(coordinator, {"max_inline_screenshots": max_inline})
    if provider is not None:
        await coordinator.hooks.emit("provider:request", {"provider": "test-provider"})
    return coordinator


async def core_dispatch(
    coordinator, tool, arguments, *, call_id="capture", name="my_desktop"
):
    """Supported TOOL_PRE/execute/TOOL_POST plumbing, matching Loop's result dump."""
    data = {"tool_name": name, "tool_call_id": call_id, "tool_input": arguments}
    pre = await coordinator.hooks.emit("tool:pre", data)
    pre = await coordinator.process_hook_result(pre, "tool:pre", name)
    assert pre.action != "deny", pre.reason
    result = await tool.execute(arguments)
    await coordinator.hooks.emit("tool:post", {**data, "result": result.model_dump()})
    return result


def test_explicit_intent_real_toolspec_cached_dimensions_and_no_discovery(computer):
    expected = computer.native_tool_spec
    assert expected["type"] == "function"
    assert expected["computer_use"] == {
        "version": 1,
        "operations": cu.COMPUTER_OPERATIONS,
        "unavailable_operations": {},
        "image_space": {"width": 800, "height": 600, "unit": "pixel"},
    }
    for _ in range(3):
        spec = ToolSpec(
            name="my_desktop",
            description=computer.description,
            parameters=computer.input_schema,
            **computer.native_tool_spec,
        )
        assert spec.model_dump()["computer_use"] == expected["computer_use"]
    assert computer._backend.geometry_reads == 1
    assert computer._backend.calls == []


def test_intent_unknown_geometry_does_not_guess_and_local_limitations_are_explicit():
    tool = cu.ComputerTool(Backend(), {"read_only": True, "enable_zoom": False})
    intent = tool.native_tool_spec["computer_use"]
    assert "image_space" not in intent
    assert "zoom" not in intent["operations"]
    assert set(cu.MUTATING).isdisjoint(intent["operations"])
    assert set(intent["unavailable_operations"]) == (cu.MUTATING - {"focus_window"}) | {
        "zoom"
    }
    result = run(tool.execute(batch(CLICK)))
    assert not result.success
    assert "geometry" in result.error["message"]
    assert tool._backend.calls == []


@pytest.mark.parametrize(
    "bad",
    [
        {"action": "not_an_action"},
        {"action": "key", "text": "down", "repeat": True},
        {"action": "mouse_move"},
        {"action": "left_click", "coordinate": None},
        {"action": "left_click", "coordinate": [float("nan"), 5]},
        {"action": "left_click", "coordinate": [99999, 5]},
        {"action": "zoom", "region": [1, 2]},
        {"action": "wait", "duration": -1},
        {"action": "wait", "duration": 1e30},
        {"action": "hold_key", "text": "down", "duration": 61},
        {"action": "left_click", "coordinate": [10**400, 3]},
        {"action": "type", "text": 3},
        {"action": "scroll", "scroll_direction": "diagonal"},
        {"action": "scroll", "scroll_direction": ["down"]},
        {"action": "scroll", "scroll_direction": {"direction": "down"}},
        {"action": "scroll", "scroll_direction": 1},
        {"action": "scroll", "scroll_direction": True},
        {"action": "scroll", "direction": ["down"]},
        {"action": "scroll", "direction": {"direction": "down"}},
        {"action": "focus_window"},
        {"action": "key", "text": "enter", "keys": ["shift"]},
        {"type": "click", "x": 1, "y": 2},
        None,
    ],
)
def test_invalid_final_action_validates_upfront_zero_input_and_no_disclosure(
    computer, bad
):
    disclosed = []
    computer._ensure_announced = lambda: disclosed.append(True)
    payload = batch(CLICK, bad)
    result = run(computer.execute(payload))
    assert not result.success
    assert result.error["type"] == "ValueError"
    assert "actions[1]" in result.error["message"]
    assert computer._backend.calls == []
    assert disclosed == []


@pytest.mark.parametrize(
    "bad",
    [
        {"version": True, "actions": [CLICK], "capture_after": True},
        {"version": 2, "actions": [CLICK], "capture_after": True},
        {"version": 1, "actions": [], "capture_after": True},
        {"version": 1, "actions": [CLICK], "capture_after": 1},
        {"actions": [{"type": "click", "x": 20, "y": 30}]},
        {
            "version": 1,
            "actions": [CLICK],
            "capture_after": True,
            "pending_safety_checks": [],
        },
    ],
)
def test_malformed_or_vendor_batch_is_not_interpreted(computer, bad):
    assert not run(computer.execute(bad)).success
    assert computer._backend.calls == []


@pytest.mark.parametrize(
    "actions, captures",
    [
        ([{"action": "screenshot"}, CLICK], 2),
        ([{"action": "zoom", "region": [0, 0, 30, 40]}, CLICK], 2),
        ([CLICK, {"action": "screenshot"}], 1),
        ([{"action": "screenshot"}, {"action": "zoom", "region": [0, 0, 30, 40]}], 3),
    ],
)
def test_only_terminal_full_capture_satisfies_final_observation(
    computer, actions, captures
):
    original = copy.deepcopy(actions)
    result = run(computer.execute(batch(*actions)))
    assert result.success, result.error
    payload = json.loads(result.output)
    assert payload["progress"] == {
        "completed": len(actions),
        "total": len(actions),
        "uncertain_in_flight": None,
    }
    assert len([c for c in computer._backend.calls if c[0] == "capture"]) == captures
    assert computer._backend.calls[-1] == ("capture", None)
    with Image.open(payload["images"][0]) as image:
        assert image.size == (800, 600)
    assert actions == original
    if CLICK in actions:
        assert ("click", -1560, 90, "left", 1) in computer._backend.calls


def test_capture_after_false_does_not_capture(computer):
    result = run(computer.execute(batch(CLICK, capture=False)))
    assert result.success
    assert json.loads(result.output)["images"] == []
    assert [c[0] for c in computer._backend.calls] == ["click"]


@pytest.mark.parametrize("flat", [False, True])
def test_real_persistence_obstruction_is_failed_image_free_result_with_cleanup(
    computer, tmp_path, monkeypatch, flat
):
    occupied = tmp_path / "occupied-output-directory"
    occupied.write_text("real filesystem obstruction")
    monkeypatch.setattr(cu, "SHOT_DIR", occupied)
    exits = []
    computer._band_enter = lambda: "normal-cleanup"
    computer._band_exit = lambda token: exits.append(token)
    result = run(computer.execute({"action": "screenshot"} if flat else batch(CLICK)))
    assert not result.success
    assert result.error["type"] == "FileExistsError"
    assert "screenshot persistence failed" in result.error["message"]
    assert exits == ["normal-cleanup"]
    assert not computer._issued_capture_results
    assert occupied.read_text() == "real filesystem obstruction"
    if flat:
        # Core's normal ToolResult supplies error text as output when omitted.
        assert result.output == result.error["message"]
        assert result.content is None
        assert computer._backend.calls == [("capture", None)]
    else:
        payload = json.loads(result.output)
        assert payload["images"] == []
        assert payload["progress"] == {
            "completed": 1,
            "total": 1,
            "uncertain_in_flight": None,
        }
        assert "left_click at [20, 30]" in payload["text"]
        assert [c[0] for c in computer._backend.calls] == ["click", "capture"]


@pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
@pytest.mark.parametrize("key", ["scroll_direction", "direction"])
def test_scroll_direction_aliases_and_zero_amount_are_valid(computer, direction, key):
    result = run(
        computer.execute(
            batch({"action": "scroll", key: direction, "amount": 0}, capture=False)
        )
    )
    assert result.success, result.error
    assert computer._backend.calls == [("scroll", direction, 0)]


def test_flat_canonical_compatibility(computer):
    result = run(computer.execute(CLICK))
    assert result.success
    assert result.output == "left_click at [20, 30]"
    assert [c[0] for c in computer._backend.calls] == ["click"]


@pytest.mark.parametrize("change", ["read_only", "binding", "failure"])
def test_batch_stops_partial_without_capture_replay_or_new_target_input(
    computer, change
):
    backend = computer._backend
    if change == "read_only":
        backend.on_input = lambda: setattr(
            computer, "_binding", replace(computer._binding, read_only=True)
        )
    elif change == "binding":
        backend.on_input = lambda: setattr(computer, "_backend", Backend())
    else:

        def fail():
            raise BackendError("injection outcome unknown")

        backend.on_input = fail
    result = run(computer.execute(batch(CLICK, {"action": "key", "text": "enter"})))
    assert not result.success
    progress = json.loads(result.output)["progress"]
    assert progress["completed"] == (0 if change == "failure" else 1)
    assert progress["uncertain_in_flight"] == (0 if change == "failure" else None)
    assert [c[0] for c in backend.calls] == ["click"]
    assert json.loads(result.output)["images"] == []


def test_halt_preserves_notice_and_completed_progress_without_final_capture(
    computer, monkeypatch
):
    monkeypatch.setattr(cu, "record_halt", lambda *a, **kw: None)
    snapshot = PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="test",
        last_human_input_ago_ms=1,
        margin_ms=1,
        guard_ms=5,
        guard_measured=True,
        sample_interval_ms=60,
        latched_until_ms=None,
    )

    def halt():
        raise HaltedError(snapshot)

    computer._backend.on_input = halt
    result = run(computer.execute(batch(CLICK, CLICK)))
    assert not result.success
    assert result.error["type"] == "HaltedError"
    assert len(computer.halt_notices) == 1
    assert [c[0] for c in computer._backend.calls] == ["click"]


class Coordinator:
    def __init__(self, tool, interactive=False):
        self.tools = {"my_desktop": tool}
        self.interactive = interactive

    def get(self, category, name=None):
        if category == "tools":
            return self.tools if name is None else self.tools.get(name)
        return None

    def get_capability(self, name):
        return self.interactive


@pytest.mark.parametrize(
    "interactive, unattended, expected",
    [
        (False, False, "deny"),
        (True, False, "ask_user"),
        (False, True, "continue"),
    ],
)
def test_nested_write_gate_alias_and_exact_batch_failsafe(
    computer, interactive, unattended, expected
):
    computer._gate_writes = True
    payload = batch(CLICK)
    coord = Coordinator(computer, interactive)
    gate = hook._make_gate_handler(coord, unattended_writes_ok=unattended)
    result = run(gate("tool:pre", {"tool_name": "my_desktop", "tool_input": payload}))
    assert result.action == expected
    if expected == "deny":
        assert not run(computer.execute(payload)).success
        assert computer._backend.calls == []
    else:
        assert run(computer.execute(payload)).success
        if expected == "ask_user":
            assert result.approval_default == "deny"
            assert not run(computer.execute(payload)).success  # consumed


def test_approval_for_different_batch_cannot_authorize_input(computer):
    computer._gate_writes = True
    gate = hook._make_gate_handler(Coordinator(computer, True))
    assert (
        run(
            gate("tool:pre", {"tool_name": "my_desktop", "tool_input": batch(CLICK)})
        ).action
        == "ask_user"
    )
    assert not run(computer.execute(batch({"action": "key", "text": "enter"}))).success
    assert computer._backend.calls == []


def test_batch_cancellation_reports_uncertainty_no_next_input_and_defers_band_cleanup(
    computer,
):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    exits = []
    original = computer._backend.click

    def blocking(*args, **kwargs):
        original(*args, **kwargs)
        started.set()
        release.wait(3)
        finished.set()

    computer._backend.click = blocking
    computer._band_enter = lambda: "fake-band-token"
    computer._band_exit = lambda token: exits.append(token)

    async def scenario():
        task = asyncio.create_task(computer.execute(batch(CLICK, CLICK)))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        result = await task
        assert not result.success
        assert result.error["type"] == "CancelledError"
        assert json.loads(result.output)["progress"]["uncertain_in_flight"] == 0
        assert exits == []
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await asyncio.sleep(0.01)

    run(scenario())
    assert exits == ["fake-band-token"]
    assert [c[0] for c in computer._backend.calls] == ["click"]


def test_zero_scroll_and_hold_duration_never_become_nonzero_input(computer):
    result = run(
        computer.execute(
            batch(
                {"action": "scroll", "scroll_direction": "down", "scroll_amount": 0},
                {"action": "hold_key", "text": "down", "duration": 0},
                capture=False,
            )
        )
    )
    assert result.success, result.error
    assert computer._backend.calls == [("scroll", "down", 0), ("hold_key", "down", 0.0)]


def test_repeat_partial_progress_retains_known_completed_presses(computer):
    backend = computer._backend

    def fail_second():
        if len(backend.calls) == 2:
            raise BackendError("second key outcome unknown")

    backend.on_input = fail_second
    result = run(
        computer.execute(batch({"action": "key", "text": "down", "repeat": 4}))
    )
    assert not result.success
    progress = json.loads(result.output)["progress"]
    assert progress["completed"] == 0
    assert progress["completed_key_presses_in_stopped_action"] == 1
    assert progress["uncertain_in_flight"] == 0
    assert len(backend.calls) == 2


def test_cancelled_band_acquisition_releases_late_token_once_without_input(computer):
    started, release, exited = threading.Event(), threading.Event(), threading.Event()
    tokens = []

    def acquire():
        started.set()
        release.wait(3)
        return "late-token"

    def exit_band(token):
        tokens.append(token)
        exited.set()

    computer._band_enter = acquire
    computer._band_exit = exit_band

    async def scenario():
        task = asyncio.create_task(computer.execute(batch(CLICK)))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        result = await task
        assert not result.success
        assert result.error["type"] == "CancelledError"
        assert json.loads(result.output)["progress"]["completed"] == 0
        assert tokens == []
        release.set()
        assert await asyncio.to_thread(exited.wait, 1)

    run(scenario())
    assert tokens == ["late-token"]
    assert computer._backend.calls == []


def test_cancelled_worker_halt_is_recorded_once_against_pinned_target(
    computer, monkeypatch
):
    started, release, recorded = threading.Event(), threading.Event(), threading.Event()
    keys = []

    def record(name, snapshot, **kwargs):
        keys.append(name)
        recorded.set()

    monkeypatch.setattr(cu, "record_halt", record)
    backend = computer._backend
    snapshot = PresenceSnapshot(
        state=PresenceState.HUMAN_ACTIVE,
        confidence=Confidence.HIGH,
        basis="test",
        last_human_input_ago_ms=1,
        margin_ms=1,
        guard_ms=5,
        guard_measured=True,
        sample_interval_ms=60,
        latched_until_ms=None,
    )

    def halt_late():
        started.set()
        release.wait(3)
        raise HaltedError(snapshot)

    backend.on_input = halt_late

    async def scenario():
        task = asyncio.create_task(computer.execute(batch(CLICK, CLICK)))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        result = await task
        assert result.error["type"] == "CancelledError"
        replacement = Backend()
        replacement.name = "replacement-fake"
        computer._backend = replacement
        release.set()
        assert await asyncio.to_thread(recorded.wait, 1)
        await asyncio.sleep(0.01)
        assert len(computer.halt_notices) == 1
        assert replacement.calls == []

    run(scenario())
    assert keys == [backend.name]
    assert [call[0] for call in backend.calls] == ["click"]


@pytest.mark.parametrize("capture", [False, True])
def test_cancellation_during_band_cleanup_retains_completed_progress(computer, capture):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    exits = []
    computer._band_enter = lambda: "cleanup-token"

    def exit_band(token):
        exits.append(token)
        started.set()
        release.wait(3)
        finished.set()

    computer._band_exit = exit_band

    async def scenario():
        task = asyncio.create_task(computer.execute(batch(CLICK, capture=capture)))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        result = await task
        assert result.error["type"] == "CancelledError"
        assert not result.success
        assert json.loads(result.output)["progress"]["completed"] == 1
        assert json.loads(result.output)["progress"]["uncertain_in_flight"] is None
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)

    run(scenario())
    assert exits == ["cleanup-token"]
    assert [call[0] for call in computer._backend.calls] == (
        ["click", "capture"] if capture else ["click"]
    )
    assert not computer._issued_capture_results


def test_provider_private_probes_and_model_observation_are_absent():
    for module in (cu, hook):
        source = Path(module.__file__).read_text()
        for name in (
            "_derive_native_tool_betas",
            "_convert_tools_from_request",
            "get_native_computer_tool_spec",
            "note_model",
            "tool_versions",
            "from .providers",
        ):
            assert name not in source
    assert not (Path(cu.__file__).parent / "providers.py").exists()
    assert not (Path(cu.__file__).parent / "tool_versions.py").exists()
    assert (Path(cu.__file__).parent / "wire.py").exists()


def test_stream_guard_is_generic_and_refuses_before_any_wrap():
    class Provider:
        async def complete(self, request, *, request_options=None):
            return request

        async def stream(self, request):
            yield request

    provider = Provider()
    original = provider.complete
    with pytest.raises(hook.ComputerUseHookIncompatibleProviderError, match="stream"):
        hook._wrap_provider(provider, Coordinator(None), 3)
    assert provider.complete == original
    assert not getattr(provider, hook._WRAPPED_FLAG, False)


@pytest.mark.parametrize("preserve", [False, True])
def test_orchestrator_guard_checks_actual_core_extras_not_vendor_type(
    monkeypatch, preserve
):
    import types

    name = "test_loop_streaming_neutral"
    module = types.ModuleType(name)

    def build(tool):
        return ToolSpec(
            name=tool.name,
            description=tool.description,
            parameters=tool.input_schema,
            **(tool.native_tool_spec if preserve else {}),
        )

    module._build_tool_spec = build
    monkeypatch.setitem(sys.modules, name, module)
    orchestrator = type("Orchestrator", (), {"__module__": name})()
    assert hook._orchestrator_preserves_native_tool_spec(orchestrator) is preserve
    assert hook._orchestrator_preserves_native_tool_spec(SimpleNamespace()) is None
