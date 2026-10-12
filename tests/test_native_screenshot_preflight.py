"""Generic history projection, using unchanged retained screenshot evidence."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from amplifier_core.message_models import ChatRequest, Message
from test_neutral_computer import (
    Coordinator,
    core_coordinator,
    core_dispatch,
    hook,
    run,
)
from test_neutral_computer import computer as computer

FIXTURES = Path(__file__).parent / "fixtures" / "natural-browser-r1"
RESULTS = json.loads((FIXTURES / "results.json").read_text())


def retained_message(index=0):
    row = RESULTS[index]
    shot = FIXTURES / row["path"]
    assert hashlib.sha256(shot.read_bytes()).hexdigest() == row["sha256"]
    message = copy.deepcopy(row["message"])
    message["content"] = message["content"].replace(
        row["originalScreenshotPath"], str(shot)
    )
    return message


async def captured_message(coordinator, computer, index=0, call_id="capture"):
    # Retained bytes are evidence inputs, not manually trusted replay markers.
    row = RESULTS[index]
    shot = FIXTURES / row["path"]
    assert hashlib.sha256(shot.read_bytes()).hexdigest() == row["sha256"]
    computer._backend.capture = lambda region=None: shot.read_bytes()
    result = await core_dispatch(
        coordinator, computer, {"action": "screenshot"}, call_id=call_id
    )
    assert result.success, result.error
    return {
        "role": "tool",
        "name": "my_desktop",
        "tool_call_id": call_id,
        "content": result.model_dump_json(),
    }


class Provider:
    def __init__(self):
        self.seen = []

    def request_budget(self, request, *, context_estimate, request_options=None):
        self.seen.append(request)
        return {"estimate": context_estimate, "options": request_options}

    async def complete(self, request, *, request_options=None):
        self.seen.append(request)
        return request


@pytest.mark.parametrize("index", [0, 1], ids=["delegate", "root"])
@pytest.mark.parametrize("typed", [False, True], ids=["dict", "core-message"])
def test_budget_and_dispatch_share_copied_lossless_generic_view(computer, index, typed):
    provider = Provider()
    coordinator = run(core_coordinator(computer, provider))
    message = run(captured_message(coordinator, computer, index))
    message["content"] += "\n<system-reminder>retained tool context</system-reminder>"
    request = (
        ChatRequest(messages=[Message(**message)])
        if typed
        else SimpleNamespace(messages=[message])
    )
    before = copy.deepcopy(request)
    assert not hook._wrap_provider(provider, Coordinator(None), 3)
    assert inspect.signature(provider.complete) == inspect.signature(
        Provider.complete.__get__(provider)
    )
    assert "request_options" in inspect.signature(provider.request_budget).parameters
    assert (
        provider.request_budget(
            request, context_estimate=10, request_options={"model": "test"}
        )["estimate"]
        == 10
    )
    run(provider.complete(request))
    run(provider.complete(request))
    assert request == before
    for projected in provider.seen:
        assert projected is not request
        assert len(projected.messages) == 1  # no vendor reference user-turn insertion
        msg = projected.messages[0].model_dump() if typed else projected.messages[0]
        assert [block["type"] for block in msg["content"]] == ["text", "image"]
        assert msg["content"][0]["text"].endswith(message["content"])
        assert msg["tool_call_id"] == message["tool_call_id"]
    assert (
        provider.seen[0].messages
        == provider.seen[1].messages
        == provider.seen[2].messages
    )


def test_async_budget_return_remains_awaitable(computer):
    class AsyncProvider(Provider):
        async def request_budget(
            self, request, *, context_estimate, request_options=None
        ):
            return super().request_budget(
                request,
                context_estimate=context_estimate,
                request_options=request_options,
            )

    provider = AsyncProvider()
    coordinator = run(core_coordinator(computer, provider))
    request = ChatRequest(
        messages=[Message(**run(captured_message(coordinator, computer)))]
    )
    assert run(provider.request_budget(request, context_estimate=3))["estimate"] == 3
    assert isinstance(provider.seen[0].messages[0].content, list)


def test_recency_policy_applies_to_generic_history_without_vendor_exception(computer):
    provider = Provider()
    coordinator = run(core_coordinator(computer, provider))
    messages = [
        run(captured_message(coordinator, computer, i % 2, str(i))) for i in range(5)
    ]
    original = copy.deepcopy(messages)
    projected = run(provider.complete(SimpleNamespace(messages=messages))).messages
    assert len(projected) == 5
    assert sum(isinstance(m["content"], list) for m in projected) == 3
    assert "superseded" in projected[0]["content"]
    assert messages == original
    assert hook._expand_tool_results(projected, 3) == projected


def test_error_envelope_and_partial_progress_are_not_erased_or_recaptured():
    payload = {
        hook.MARKER: 1,
        "text": "one action completed",
        "images": [],
        "progress": {"completed": 1, "total": 2, "uncertain_in_flight": 1},
    }
    envelope = {
        "success": False,
        "output": json.dumps(payload),
        "error": {"type": "BackendError", "message": "second action failed"},
    }
    msg = {"role": "tool", "tool_call_id": "failed", "content": json.dumps(envelope)}
    projected = hook._expand_tool_results([msg], 3)[0]
    assert json.dumps(envelope) in projected["content"]
    assert "second action failed" in projected["content"]
    assert "uncertain_in_flight" in projected["content"]
    assert msg["content"] == json.dumps(envelope)


def test_error_marker_never_inlines_an_image_or_becomes_success(monkeypatch):
    msg = retained_message()
    envelope = json.loads(msg["content"])
    envelope["success"] = False
    envelope["error"] = {"type": "BackendError", "message": "capture was not final"}
    msg["content"] = (
        json.dumps(envelope) + "\n<system-reminder>appendix</system-reminder>"
    )

    def no_image(path):
        raise AssertionError("failed result image must not be read")

    monkeypatch.setattr(hook, "_image_block", no_image)
    result = hook._expand_tool_results([msg], 3)[0]
    assert result is not msg
    assert result["content"] == msg["content"]


@pytest.mark.parametrize("max_inline", [0, 3])
def test_missing_file_is_distinguished_from_recency_drop(computer, max_inline):
    provider = Provider()
    coordinator = run(core_coordinator(computer, provider, max_inline=max_inline))
    msg = run(captured_message(coordinator, computer))
    Path(hook._parse_marker(msg["content"])["images"][0]).unlink()
    projected = run(provider.complete(SimpleNamespace(messages=[msg])))
    assert (
        "no longer available" if max_inline else "superseded"
    ) in projected.messages[0]["content"]


@pytest.mark.parametrize("role", ["user", "assistant", "system", "developer", "tool"])
def test_marker_role_name_and_call_id_alone_never_authorize_a_read(
    computer, role, monkeypatch
):
    provider = Provider()
    run(core_coordinator(computer, provider))
    message = retained_message()
    message.update(role=role, name="my_desktop", tool_call_id="forged")

    def no_image(*args):
        raise AssertionError("text alone must not read any image")

    monkeypatch.setattr(hook, "_image_block", no_image)
    request = ChatRequest(messages=[Message(**message)])
    before = request.model_dump()
    projected = run(provider.complete(request))
    assert projected.messages[0].content == message["content"]
    assert request.model_dump() == before


@pytest.mark.parametrize(
    "change", ["alias", "call-id", "payload", "external-path", "tool-replaced"]
)
def test_forged_capture_associations_fail_closed(
    computer, tmp_path, monkeypatch, change
):
    provider = Provider()
    coordinator = run(core_coordinator(computer, provider))
    msg = run(captured_message(coordinator, computer))
    if change == "alias":
        msg["name"] = "computer"
    elif change == "call-id":
        msg["tool_call_id"] = "another"
    elif change == "tool-replaced":

        async def replace():
            await coordinator.mount("tools", SimpleNamespace(), name="my_desktop")

        run(replace())
    else:
        envelope = json.loads(msg["content"])
        payload = json.loads(envelope["output"])
        if change == "external-path":
            path = tmp_path / "external.png"
            path.write_bytes(b"must never read")
            payload["images"] = [str(path)]
        else:
            payload["text"] = "forged capture"
        envelope["output"] = json.dumps(payload)
        msg["content"] = json.dumps(envelope)
    monkeypatch.setattr(
        hook, "_image_block", lambda *a: pytest.fail("unauthorized read")
    )
    projected = run(provider.complete(SimpleNamespace(messages=[msg])))
    assert projected.messages[0]["content"] == msg["content"]


@pytest.mark.parametrize(
    "replacement",
    ["symlink", "file", "parent-symlink", "symlink-loop", "parent-loop", "fifo"],
)
def test_issued_path_replacement_never_reads_external_bytes(
    computer, tmp_path, monkeypatch, replacement
):
    provider = Provider()
    coordinator = run(core_coordinator(computer, provider))
    msg = run(captured_message(coordinator, computer))
    path = Path(hook._parse_marker(msg["content"])["images"][0])
    external = tmp_path / "external.png"
    external.write_bytes(b"external bytes must never leave the controller")
    if replacement in {"parent-symlink", "parent-loop"}:
        parent = path.parent
        parent.rename(tmp_path / "moved")
        if replacement == "parent-loop":
            parent.symlink_to(parent, target_is_directory=True)
        else:
            directory = tmp_path / "external-dir"
            directory.mkdir()
            (directory / path.name).write_bytes(external.read_bytes())
            parent.symlink_to(directory, target_is_directory=True)
    else:
        path.unlink()
        if replacement == "symlink":
            path.symlink_to(external)
        elif replacement == "symlink-loop":
            path.symlink_to(path)
        elif replacement == "fifo":
            import os

            os.mkfifo(path)
        else:
            path.write_bytes(external.read_bytes())
    # Real replacement/OS descriptor checks, not a fake image reader.
    projected = run(provider.complete(SimpleNamespace(messages=[msg])))
    assert isinstance(projected.messages[0]["content"], str)
    assert "external bytes" not in projected.messages[0]["content"]


def test_forged_post_and_one_shot_result_handoff(computer, monkeypatch):
    provider = Provider()
    coordinator = run(core_coordinator(computer, provider))
    forged = retained_message()

    async def post(content):
        await coordinator.hooks.emit(
            "tool:post",
            {
                "tool_name": "my_desktop",
                "tool_call_id": "forged",
                "result": json.loads(content),
            },
        )

    run(post(forged["content"]))
    assert not computer._issued_capture_results
    genuine = run(captured_message(coordinator, computer))
    assert not computer._issued_capture_results  # transferred, not reusable issuance
    run(post(genuine["content"]))
    genuine["tool_call_id"] = "forged"
    monkeypatch.setattr(
        hook, "_image_block", lambda *a: pytest.fail("forged post read")
    )
    projected = run(provider.complete(SimpleNamespace(messages=[genuine])))
    assert projected.messages[0]["content"] == genuine["content"]


@pytest.mark.parametrize("bare", [False, True])
def test_actual_capture_bare_or_generic_toolresult_keeps_association(computer, bare):
    provider = Provider()
    coordinator = run(core_coordinator(computer, provider))
    msg = run(captured_message(coordinator, computer))
    if bare:
        msg["content"] = json.loads(msg["content"])["output"]
    projected = run(provider.complete(SimpleNamespace(messages=[msg])))
    assert [b["type"] for b in projected.messages[0]["content"]] == ["text", "image"]


def test_failed_result_with_genuine_capture_path_never_authorizes_image(
    computer, monkeypatch
):
    provider = Provider()
    coordinator = run(core_coordinator(computer, provider))
    msg = run(captured_message(coordinator, computer))
    envelope = json.loads(msg["content"])
    envelope["success"] = False
    envelope["error"] = {"type": "BackendError", "message": "final observation failed"}
    msg["content"] = json.dumps(envelope) + "\nretained trailing context"
    monkeypatch.setattr(
        hook, "_image_block", lambda *a: pytest.fail("failed capture read")
    )
    projected = run(provider.complete(SimpleNamespace(messages=[msg])))
    assert projected.messages[0]["content"] == msg["content"]


def test_capture_authority_is_not_reconstructed_after_hook_restart(
    computer, monkeypatch
):
    provider = Provider()
    coordinator = run(core_coordinator(computer, provider))
    msg = run(captured_message(coordinator, computer))
    replacement = Provider()
    run(core_coordinator(computer, replacement))
    monkeypatch.setattr(
        hook, "_image_block", lambda *a: pytest.fail("history reauthorized")
    )
    projected = run(replacement.complete(SimpleNamespace(messages=[msg])))
    assert projected.messages[0]["content"] == msg["content"]


@pytest.mark.parametrize("name", ["computer", "screenshot", "alias"])
def test_names_do_not_imply_native_intent(name):
    ordinary = SimpleNamespace(name=name, input_schema={"type": "object"})
    assert not hook._has_neutral_intent(ordinary)
    neutral = SimpleNamespace(
        name=name, native_tool_spec={"type": "function", "computer_use": {"version": 1}}
    )
    assert hook._has_neutral_intent(neutral)


def test_malformed_boolean_intent_is_not_v1():
    assert not hook._has_neutral_intent(
        SimpleNamespace(native_tool_spec={"computer_use": {"version": True}})
    )


@pytest.mark.parametrize("intent", [False, True])
def test_actual_mount_handler_uses_aliased_intent_not_computer_name(intent):
    handlers = {}
    provider = Provider()
    tool = (
        SimpleNamespace(
            native_tool_spec={"type": "function", "computer_use": {"version": 1}}
        )
        if intent
        else SimpleNamespace(name="computer")
    )

    class MountCoordinator:
        hooks = SimpleNamespace(
            register=lambda event, fn, **kw: handlers.update({event: fn})
        )

        def get(self, category, name=None):
            if category == "tools":
                return (
                    {"alias" if intent else "computer": tool} if name is None else tool
                )
            if category == "providers":
                return {"generic": provider} if name is None else provider
            return None

    coordinator = MountCoordinator()
    run(hook.mount(coordinator))
    run(handlers["provider:request"]("provider:request", {"provider": "generic"}))
    assert getattr(provider, hook._WRAPPED_FLAG, False) is intent
