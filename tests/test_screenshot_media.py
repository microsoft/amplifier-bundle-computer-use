"""Synthetic screenshots only: no backend discovery or real desktop operations."""

import asyncio
import base64
import copy
import hashlib
import io
import json
import threading
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest
from PIL import Image
from test_neutral_computer import hook
from test_screenshot_permissions import _make_computer

import amplifier_module_tool_computer_use as cu

NS = "experimental.media.v1"


def run(coro):
    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        return runner.run(coro)


class Media:
    version = 1
    enabled = True
    session_id = "native-session"

    def __init__(self):
        self.calls = []
        self.on_ingest = None

    async def ingest(self, value, *, source_format, origin, policy):
        self.calls.append((copy.deepcopy(origin), source_format, policy))
        if self.on_ingest:
            self.on_ingest()
        raw = base64.b64decode(value["data"], validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
        ref = {
            "v": 1,
            "session_id": self.session_id,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "mime": "image/png",
            "bytes": len(raw),
            "width": width,
            "height": height,
            "provenance": origin,
        }
        return {"value": ref, "staged_refs": [ref]}


def setup(tmp_path, monkeypatch, cap=None, policy=True):
    tool = _make_computer(tmp_path, monkeypatch)
    cap = cap if cap is not None else Media()
    caps = {NS: cap}
    if policy:
        caps["computer_use.media_policy"] = tool._validate_media_ref
    tool._media_get_capability = caps.get
    tool._media_coordinator = SimpleNamespace(_tool_dispatch_contexts={})
    captures = []
    original = tool._backend.capture

    def capture(*args, **kwargs):
        captures.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(tool._backend, "capture", capture)
    return tool, cap, captures, caps


def execute(tool, value=None, call_id="real-call"):
    async def run():
        contexts = tool._media_coordinator._tool_dispatch_contexts
        if call_id is not None:
            contexts[asyncio.current_task()] = {"tool_call_id": call_id}
        try:
            return await tool.execute(value or {"action": "screenshot"})
        finally:
            contexts.pop(asyncio.current_task(), None)

    with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
        return runner.run(run())


def test_mapping_refs_original_pixels_no_legacy_files(tmp_path, monkeypatch):
    tool, cap, captures, _ = setup(tmp_path, monkeypatch)
    result = execute(tool)
    assert result.success
    ref = result.output[NS]["refs"][0]
    assert ref["provenance"]["id"] == "real-call"
    assert ref["session_id"] == "native-session" != tool._session_id
    assert ref["width"] == ref["height"] == 4
    assert len(captures) == len(cap.calls) == 1
    assert not cu.SHOT_DIR.exists()
    saved = result.model_dump_json()
    assert "base64" not in saved and "iVBOR" not in saved
    assert cu.MARKER not in result.output and "images" not in result.output
    assert tool._validate_media_ref(ref, purpose="model") is True
    # Capture UUID is independent of the real tool call ID.
    assert result.output["computer_use"]["capture_id"] != "real-call"


@pytest.mark.parametrize("case", ["disabled", "version", "policy", "origin", "ingest"])
def test_unavailable_never_persists_or_recaptures(tmp_path, monkeypatch, case):
    tool, cap, captures, caps = setup(tmp_path, monkeypatch)
    if case == "disabled":
        cap.enabled = False
    elif case == "version":
        cap.version = True
    elif case == "policy":
        caps.pop("computer_use.media_policy")
    elif case == "ingest":
        def fail():
            raise OSError("reply or packaging failed")
        cap.on_ingest = fail
    result = execute(tool, call_id=None if case == "origin" else "real-call")
    assert result.success  # The original capture completed; never invite replay.
    assert NS not in result.output
    assert "no pixels supplied" in result.output["text"]
    assert len(captures) == 1 and not cu.SHOT_DIR.exists()
    assert len(cap.calls) == (1 if case == "ingest" else 0)


@pytest.mark.parametrize("change", ["policy", "cleanup", "pause", "halt", "expired"])
def test_owner_validation_refuses_without_backend_calls(tmp_path, monkeypatch, change):
    tool, _, captures, _ = setup(tmp_path, monkeypatch)
    guard = SimpleNamespace(
        halted=False, pause=SimpleNamespace(is_paused=False)
    )
    tool._coexistence_guard = guard
    ref = execute(tool).output[NS]["refs"][0]
    if change == "policy":
        tool._binding = replace(tool._binding, read_only=True)
    elif change == "cleanup":
        tool._cleanup_started = True
    elif change == "halt":
        guard.halted = True
    elif change == "pause":
        guard.pause.is_paused = True
    else:
        original = cu.time.time
        monkeypatch.setattr(cu.time, "time", lambda: original() + cu.SHOT_TTL_SECONDS + 1)
    with pytest.raises(ValueError, match="no longer current"):
        tool._validate_media_ref(ref, purpose="transport")
    assert captures == [1]


def test_original_ttl_per_observation_and_copy_refusal(tmp_path, monkeypatch):
    tool, cap, captures, _ = setup(tmp_path, monkeypatch)
    first = execute(tool, call_id="call1")
    second = execute(tool, call_id="call2")
    refs = [r.output[NS]["refs"][0] for r in (first, second)]
    assert refs[0]["sha256"] == refs[1]["sha256"]
    assert refs[0]["provenance"] != refs[1]["provenance"]
    for ref in refs:
        provenance = ref["provenance"]
        assert (
            datetime.fromisoformat(provenance["expires_at"])
            - datetime.fromisoformat(provenance["created_at"])
        ).total_seconds() == cu.SHOT_TTL_SECONDS
    # A new tool mount/resume/fork is not a continuation of screen consent.
    child = _make_computer(tmp_path / "child", monkeypatch)
    with pytest.raises(ValueError, match="not issued"):
        child._validate_media_ref(copy.deepcopy(refs[0]), purpose="model")
    forged = copy.deepcopy(refs[0])
    forged["provenance"]["expires_at"] = refs[1]["provenance"]["expires_at"]
    with pytest.raises(ValueError, match="not issued"):
        tool._validate_media_ref(forged, purpose="model")
    assert len(captures) == len(cap.calls) == 2


def test_revocation_during_ingest_does_not_return_ref(tmp_path, monkeypatch):
    tool, cap, captures, _ = setup(tmp_path, monkeypatch)
    cap.on_ingest = lambda: setattr(tool, "_cleanup_started", True)
    result = execute(tool)
    assert result.success and NS not in result.output
    assert len(captures) == len(cap.calls) == 1


def test_batch_progress_and_zoom_use_actual_raster(tmp_path, monkeypatch):
    tool, _, captures, _ = setup(tmp_path, monkeypatch)
    result = execute(
        tool,
        {"version": 1, "actions": [{"action": "screenshot"}], "capture_after": True},
    )
    assert result.output["progress"]["completed"] == 1
    assert result.output["progress"]["total"] == 1
    assert result.output[NS]["refs"][0]["width"] == 4
    assert len(captures) == 1
    zoom = execute(
        tool, {"action": "zoom", "coordinate": [0, 0, 2, 2]}, call_id="zoom-call"
    )
    assert zoom.output[NS]["refs"][0]["width"] == 4  # fixture returns a 4px PNG
    assert zoom.output["computer_use"]["source"] == "zoom"


def test_legacy_still_uses_exact_owner_marker(tmp_path, monkeypatch):
    tool = _make_computer(tmp_path, monkeypatch)
    result = run(tool.execute({"action": "screenshot"}))
    assert isinstance(result.output, str)
    assert json.loads(result.output)[cu.MARKER] == 1
    assert tool._take_issued_capture_result(result.model_dump())


@pytest.mark.parametrize("redaction", [None, {}, "plain replacement", []])
def test_result_redaction_never_resurrects_private_staging(tmp_path, monkeypatch, redaction):
    tool, _, _, _ = setup(tmp_path, monkeypatch)
    result = execute(tool)
    refs = result.output[NS]["refs"]
    assert refs  # Staging is not publication.
    # The producer and legacy hook never publish refs or discover them in text.
    request = SimpleNamespace(messages=[{
        "role": "tool", "name": "computer", "tool_call_id": "real-call",
        "content": json.dumps(redaction),
    }])
    projected = hook._project_request(request, 3, None)
    assert projected is request
    assert not tool._issued_capture_results


def test_generic_media_skips_legacy_projection_even_with_marker_text():
    message = {
        "role": "tool", "content": json.dumps({
            cu.MARKER: 1, "images": ["/forged.png"], "text": "untrusted text",
        }),
        "metadata": {NS: {"refs": [{"unsupported": "still generic, not legacy"}]}},
    }

    def forbidden(*args):
        raise AssertionError("Generic media may not enter legacy image hydration")

    request = SimpleNamespace(messages=[message])
    assert hook._project_request(request, 3, forbidden) is request


def test_generic_host_does_not_stack_legacy_provider_wrapper(tmp_path, monkeypatch):
    from amplifier_core import ModuleCoordinator

    tool, cap, _, _ = setup(tmp_path, monkeypatch)
    coordinator = ModuleCoordinator()

    class Provider:
        async def complete(self, request):
            return request

        async def stream(self, request):
            yield request

    provider = Provider()

    async def check():
        await coordinator.mount("tools", tool, name="computer")
        await coordinator.mount("providers", provider, name="media")
        coordinator.register_capability(NS, cap)
        cleanup = await hook.mount(coordinator)
        original = provider.complete
        try:
            await coordinator.hooks.emit(hook.PROVIDER_REQUEST, {"provider": "media"})
            assert provider.complete == original
            assert not getattr(provider, hook._WRAPPED_FLAG, False)
        finally:
            await cleanup()

    run(check())


@pytest.mark.parametrize("field", ["id", "expires_at"])
def test_ingest_cannot_mutate_expected_original_provenance(tmp_path, monkeypatch, field):
    tool, cap, captures, _ = setup(tmp_path, monkeypatch)
    original_ingest = cap.ingest

    async def mutate(value, *, source_format, origin, policy):
        origin[field] = (
            "substituted-call" if field == "id" else "2099-01-01T00:00:00+00:00"
        )
        return await original_ingest(
            value, source_format=source_format, origin=origin, policy=policy
        )

    cap.ingest = mutate
    result = execute(tool)
    assert result.success and NS not in result.output
    assert len(captures) == len(cap.calls) == 1
    assert not tool._issued_media


@pytest.mark.parametrize("which", [NS, "computer_use.media_policy"])
def test_capability_replacement_during_ingest_refuses_original_result(
    tmp_path, monkeypatch, which
):
    tool, cap, captures, caps = setup(tmp_path, monkeypatch)
    cap.on_ingest = lambda: caps.__setitem__(which, object())
    result = execute(tool)
    assert result.success and NS not in result.output
    assert len(captures) == len(cap.calls) == 1


def test_cancelled_ingest_retains_completed_batch_without_replay(tmp_path, monkeypatch):
    tool, cap, captures, _ = setup(tmp_path, monkeypatch)

    async def scenario():
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            cap.calls.append("attempt")
            entered.set()
            await asyncio.Event().wait()

        cap.ingest = blocked

        async def execute_batch():
            tool._media_coordinator._tool_dispatch_contexts[asyncio.current_task()] = {
                "tool_call_id": "batch-call"
            }
            return await tool.execute({
                "version": 1,
                "actions": [{"action": "screenshot"}],
                "capture_after": True,
            })

        task = asyncio.create_task(execute_batch())
        await entered.wait()
        task.cancel()
        result = await task
        assert not result.success and result.error["type"] == "CancelledError"
        assert result.output["progress"]["completed"] == 1
        assert result.output["progress"]["uncertain_in_flight"] is None
        assert NS not in result.output
        assert not tool._issued_media

    run(scenario())
    assert len(captures) == len(cap.calls) == 1


def test_mapping_cleanup_cancellation_retains_progress_without_staging(tmp_path, monkeypatch):
    tool, cap, captures, _ = setup(tmp_path, monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    exits = []
    tool._band_enter = lambda: "owned-cleanup"

    def exit_band(token):
        exits.append(token)
        entered.set()
        release.wait(3)
        finished.set()

    tool._band_exit = exit_band

    async def scenario():
        task = asyncio.create_task(tool.execute({
            "version": 1,
            "actions": [{"action": "screenshot"}],
            "capture_after": True,
        }))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            result = await task
            assert not result.success and result.error["type"] == "CancelledError"
            assert result.output["progress"]["completed"] == 1
            assert NS not in result.output
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 1)

    run(scenario())
    assert captures == [1] and not cap.calls and exits == ["owned-cleanup"]


def test_generic_screenshot_recency_is_private_and_preserves_other_media(tmp_path, monkeypatch):
    from amplifier_core import ModuleCoordinator
    from amplifier_core.message_models import ChatRequest, Message

    tool, _, _, _ = setup(tmp_path, monkeypatch)
    results = [execute(tool, call_id=f"call-{i}") for i in range(4)]
    other = {"owned-by": "another-producer"}
    messages = [
        Message(
            role="tool", name="computer", tool_call_id=f"call-{i}",
            content=result.output["text"],
            metadata={
                "other-namespace": {"keep": i},
                NS: {"refs": [*result.output[NS]["refs"], *([other] if i == 0 else [])]},
            },
        )
        for i, result in enumerate(results)
    ]
    request = ChatRequest(messages=messages)
    before = request.model_dump_json()
    coordinator = ModuleCoordinator()

    async def scenario():
        await coordinator.mount("tools", tool, name="computer")
        cleanup = await hook.mount(coordinator, {"max_inline_screenshots": 3})
        try:
            prepare = coordinator.get_capability("computer_use.prepare_request")
            projected, _ = prepare(request)
            assert projected is not request
            assert projected.messages[0].metadata[NS]["refs"] == [other]
            assert projected.messages[0].metadata["other-namespace"] == {"keep": 0}
            assert "superseded" in projected.messages[0].content
            for i in range(1, 4):
                assert projected.messages[i] is request.messages[i]
            assert request.model_dump_json() == before
        finally:
            await cleanup()

    run(scenario())


def test_mounted_host_without_media_or_legacy_reader_retains_no_pixels(tmp_path, monkeypatch):
    tool = _make_computer(tmp_path, monkeypatch)
    tool._media_get_capability = {}.get
    result = run(tool.execute({"action": "screenshot"}))
    assert result.success and isinstance(result.output, dict)
    assert "Image unavailable" in result.output["text"]
    assert not cu.SHOT_DIR.exists()


def test_capture_encoding_delay_does_not_renew_original_ttl(tmp_path, monkeypatch):
    tool, _, _, _ = setup(tmp_path, monkeypatch)
    wall, mono = [1000.0], [2000.0]
    monkeypatch.setattr(cu.time, "time", lambda: wall[0])
    monkeypatch.setattr(cu.time, "monotonic", lambda: mono[0])
    original = cu.capture_scaled_b64

    def slow_capture(*args):
        encoded = original(*args)
        wall[0] += 120
        mono[0] += 120
        return encoded

    monkeypatch.setattr(cu, "capture_scaled_b64", slow_capture)
    # Exercise actual capture boundary synchronously; no event-loop fake clock.
    _, image = tool._run("screenshot", {}, binding=tool._binding)
    assert image.capture.created_at == 1000
    assert image.capture.expires_at == 1000 + cu.SHOT_TTL_SECONDS
    assert image.capture.deadline == 2000 + cu.SHOT_TTL_SECONDS


def test_legacy_historical_marker_is_protected_not_original_capture_authority(monkeypatch):
    from amplifier_module_hook_computer_use.legacy_media import (
        legacy_screenshot_origin_for,
        recognize_legacy_screenshot_marker,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Recognition cannot read a path or sample a clock")

    monkeypatch.setattr(cu.time, "time", forbidden)
    monkeypatch.setattr(cu.os, "stat", forbidden)
    value = {cu.MARKER: 1, "text": "screenshot captured", "images": ["/untrusted.png"]}
    before = copy.deepcopy(value)
    held = recognize_legacy_screenshot_marker(value)
    assert held == {
        "owner": "computer-use", "format": "legacy-screenshot-marker-v1",
        "status": "held", "reason": "missing-original-capture-receipt",
        "origin": None, "image_count": 1, "pixels_converted": 0,
    }
    snapshot = {
        "source_sha256": "a" * 64, "record_index": 1,
        "path": "/untrusted.png", "image_index": 0, "sha256": "b" * 64,
        "bytes": 100, "session_id": "native-session",
    }
    assert legacy_screenshot_origin_for(snapshot) is None
    assert value == before and "/untrusted.png" not in json.dumps(held)


@pytest.mark.parametrize("value", [
    None,
    "__amplifier_computer_use__ in human prose",
    json.dumps({cu.MARKER: 1, "text": "captured", "images": ["/shot.png"]}),
    {cu.MARKER: True, "text": "captured", "images": ["/shot.png"]},
    {cu.MARKER: 2, "text": "captured", "images": ["/shot.png"]},
    {cu.MARKER: 1, "text": "no image", "images": []},
    {cu.MARKER: 1, "text": "captured", "images": [False]},
    {cu.MARKER: 1, "text": "captured", "images": [""]},
    {cu.MARKER: 1, "text": None, "images": ["/shot.png"]},
    {cu.MARKER: 1, "text": "captured", "images": ["/shot.png"], "created_at": "today"},
    {"output": {cu.MARKER: 1, "text": "captured", "images": ["/shot.png"]}},
    {NS: {"refs": []}},
])
def test_legacy_historical_adapter_does_not_guess_or_parse(value):
    from amplifier_module_hook_computer_use.legacy_media import (
        recognize_legacy_screenshot_marker,
    )

    assert recognize_legacy_screenshot_marker(value) is None