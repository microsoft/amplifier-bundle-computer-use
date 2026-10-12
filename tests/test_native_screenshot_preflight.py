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
from test_neutral_computer import Coordinator, hook, run

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
def test_budget_and_dispatch_share_copied_lossless_generic_view(index, typed):
    message = retained_message(index)
    message["content"] += "\n<system-reminder>retained tool context</system-reminder>"
    request = (
        ChatRequest(messages=[Message(**message)])
        if typed
        else SimpleNamespace(messages=[message])
    )
    before = copy.deepcopy(request)
    provider = Provider()
    assert hook._wrap_provider(provider, Coordinator(None), 3)
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


def test_async_budget_return_remains_awaitable():
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
    hook._wrap_provider(provider, Coordinator(None), 3)
    request = ChatRequest(messages=[Message(**retained_message())])
    assert run(provider.request_budget(request, context_estimate=3))["estimate"] == 3
    assert isinstance(provider.seen[0].messages[0].content, list)


def test_recency_policy_applies_to_generic_history_without_vendor_exception():
    messages = [retained_message(i % 2) for i in range(5)]
    original = copy.deepcopy(messages)
    projected = hook._expand_tool_results(messages, 3)
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


def test_missing_file_is_distinguished_from_recency_drop():
    msg = {
        "role": "tool",
        "tool_call_id": "gone",
        "content": json.dumps(
            {
                hook.MARKER: 1,
                "text": "capture",
                "images": ["/nonexistent-neutral-test.png"],
            }
        ),
    }
    assert "no longer available" in hook._expand_tool_results([msg], 3)[0]["content"]
    assert "superseded" in hook._expand_tool_results([msg], 0)[0]["content"]


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
