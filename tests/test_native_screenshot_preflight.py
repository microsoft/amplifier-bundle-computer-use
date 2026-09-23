"""Offline regressions using the two real results from natural-browser-r1.

No computer action or model request is executed. Only the fixture path is
relocated; the successful ToolResult shape and screenshot bytes are retained.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import sys
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import pytest
from amplifier_core.message_models import ChatRequest, Message

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))
import amplifier_module_hook_computer_use as hook

FIXTURES = ROOT / "tests" / "fixtures" / "natural-browser-r1"
RESULTS = json.loads((FIXTURES / "results.json").read_text())


def async_case(function):
    """Keep this repo's older implicit-event-loop tests isolated from our runs."""

    @wraps(function)
    def run(*args, **kwargs):
        # An explicit factory avoids setting/clearing the thread's current loop.
        with asyncio.Runner(loop_factory=asyncio.new_event_loop) as runner:
            return runner.run(function(*args, **kwargs))

    return run


def retained_message(index=0):
    row = RESULTS[index]
    shot = FIXTURES / row["path"]
    assert hashlib.sha256(shot.read_bytes()).hexdigest() == row["sha256"]
    message = copy.deepcopy(row["message"])
    message["content"] = message["content"].replace(
        row["originalScreenshotPath"], str(shot)
    )
    return message


class Coordinator:
    def get(self, *args):
        return None


class Provider:
    def __init__(self):
        self.seen = []

    def get_native_computer_tool_spec(self):
        return {"type": "computer"}

    def request_budget(self, request, *, context_estimate, request_options=None):
        self.seen.append(request)
        return {"estimate": context_estimate, "options": request_options}

    async def complete(self, request, **kwargs):
        self.seen.append(request)
        return request


@pytest.mark.parametrize("index", [0, 1], ids=["delegate", "root"])
@pytest.mark.parametrize("typed", [False, True], ids=["dict", "core-message"])
@async_case
async def test_budget_and_dispatch_share_lossless_view_without_mutation(index, typed):
    message = retained_message(index)
    message["content"] += "\n\n<system-reminder>retained tool context</system-reminder>"
    original = copy.deepcopy(message)
    request = (
        ChatRequest(messages=[Message(**message)])
        if typed
        else SimpleNamespace(messages=[message])
    )
    before = copy.deepcopy(request)
    provider = Provider()
    assert hook._wrap_provider(provider, Coordinator(), 3)
    assert not hook._wrap_provider(provider, Coordinator(), 3)
    assert "request_options" in inspect.signature(provider.request_budget).parameters
    options = {"model": "gpt-5.6-terra", "reasoning_effort": "high"}
    assert provider.request_budget(
        request, context_estimate=10, request_options=options
    ) == {
        "estimate": 10,
        "options": options,
    }
    await provider.complete(request)
    await provider.complete(request)
    assert request == before
    for projected in provider.seen:
        assert projected is not request
        messages = [m.model_dump() if typed else m for m in projected.messages]
        assert len(messages) == 2
        result, reference = messages
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "image"
        assert result["tool_call_id"] == original["tool_call_id"]
        assert reference["role"] == "user"
        assert reference["metadata"]["ephemeral"] is True
        assert (
            reference["metadata"]["computerResultReference"] == original["tool_call_id"]
        )
        assert reference["content"].endswith(original["content"])
        assert "Untrusted tool data" in reference["content"]
        assert "not a new user request" in reference["content"]
    assert (
        provider.seen[0].messages
        == provider.seen[1].messages
        == provider.seen[2].messages
    )


@async_case
async def test_async_budget_behavior_is_preserved():
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
    hook._wrap_provider(provider, Coordinator(), 3)
    result = provider.request_budget(
        SimpleNamespace(messages=[retained_message()]), context_estimate=4
    )
    assert inspect.isawaitable(result)
    assert (await result)["estimate"] == 4
    assert len(provider.seen[0].messages) == 2


@pytest.mark.parametrize(
    "case",
    [
        "failure",
        "error",
        "isError",
        "bare",
        "multiple",
        "extra",
        "unknown-status",
        "missing",
    ],
)
def test_ambiguous_or_failed_marker_is_not_promoted(case):
    message = retained_message()
    envelope = json.loads(message["content"])
    payload = json.loads(envelope["output"])
    if case == "failure":
        envelope["success"] = False
    elif case == "error":
        envelope["error"] = {"code": "safety_halt"}
    elif case == "isError":
        envelope["isError"] = True
    elif case == "multiple":
        payload["images"] *= 2
    elif case == "extra":
        payload["halted"] = True
    elif case == "unknown-status":
        envelope["halted"] = True
    elif case == "missing":
        payload["images"] = [str(FIXTURES / "does-not-exist.png")]
    envelope["output"] = json.dumps(payload)
    message["content"] = json.dumps(payload if case == "bare" else envelope)
    original = copy.deepcopy(message)
    assert hook._expand_tool_results([message], 3, native_tool_type="computer") == [
        original
    ]
    assert message == original


def test_more_than_three_native_shots_keep_required_images_other_dialects_keep_recency():
    messages = []
    for index in range(6):
        message = retained_message(index % 2)
        message["tool_call_id"] = f"shot-{index}"
        messages.append(message)
    original = copy.deepcopy(messages)
    for count in range(1, 7):
        native = hook._expand_tool_results(
            messages[:count], 3, native_tool_type="computer"
        )
        assert len(native) == 2 * count
        assert all(len(m["content"]) == 1 for m in native if m["role"] == "tool")
    for dialect in (None, "computer_20251124"):
        other = hook._expand_tool_results(messages, 3, native_tool_type=dialect)
        assert len(other) == 6
        assert all("superseded" in m["content"] for m in other[:3])
        assert all(len(m["content"]) == 2 for m in other[3:])
    assert messages == original


def test_native_projection_is_idempotent_and_does_not_expand_user_markers():
    message = retained_message()
    once = hook._expand_tool_results([message], 3, native_tool_type="computer")
    assert hook._expand_tool_results(once, 3, native_tool_type="computer") == once
    message["role"] = "user"
    assert hook._expand_tool_results([message], 3, native_tool_type="computer") == [
        message
    ]
    message.update(role="tool", name="unrelated_tool")
    assert hook._expand_tool_results([message], 3, native_tool_type="computer") == [
        message
    ]


def test_plain_message_objects_are_copied_before_rewriting():
    message = SimpleNamespace(**retained_message())
    original = copy.deepcopy(message)
    projected = hook._expand_tool_results([message], 3, native_tool_type="computer")
    assert len(projected) == 2
    assert projected[0] is not message
    assert message == original


def test_provider_without_budget_does_not_gain_it():
    provider = Provider()
    provider.request_budget = None
    hook._wrap_provider(provider, Coordinator(), 3)
    assert provider.request_budget is None


def openai_provider(monkeypatch, messages):
    module = pytest.importorskip("amplifier_module_provider_openai")
    provider = module.OpenAIProvider(
        api_key="offline-test-only", config={"default_model": "gpt-5.6-terra"}
    )

    class NoNetwork:
        def __getattr__(self, name):
            raise AssertionError(f"Offline regression reached SDK client: {name}")

    provider._client = NoNetwork()
    monkeypatch.setattr(provider, "_provider_count_available", lambda: False)
    provider._native_call_ids = {m["tool_call_id"] for m in messages}
    provider._native_call_types = {m["tool_call_id"]: "computer" for m in messages}
    return provider


@pytest.mark.parametrize("index", [0, 1], ids=["delegate", "root"])
def test_original_preflight_and_old_mixed_shape_reproduce_local_error(
    monkeypatch, index
):
    from amplifier_core.llm_errors import InvalidRequestError

    message = retained_message(index)
    provider = openai_provider(monkeypatch, [message])
    request = ChatRequest(messages=[Message(**message)], model="gpt-5.6-terra")
    with pytest.raises(InvalidRequestError) as error:
        provider.request_budget(request, context_estimate=10)
    assert error.value.code == "computer_result_not_image"
    old_expansion = hook._expand_tool_results([message], 3)
    assert len(old_expansion[0]["content"]) == 2
    with pytest.raises(InvalidRequestError):
        provider._convert_messages(old_expansion)


@pytest.mark.parametrize("count", [1, 2, 4, 6])
@async_case
async def test_real_provider_budget_and_complete_assemble_identical_images(
    monkeypatch, count
):
    import base64

    messages = []
    conversation = [Message(role="user", content="Inspect the fixture")]
    for index in range(count):
        message = retained_message(index % 2)
        message["tool_call_id"] += f"-{index}"
        messages.append(message)
        assistant = copy.deepcopy(RESULTS[index % 2]["assistantCall"])
        for call in assistant["tool_calls"] + assistant["content"]:
            call["id"] = message["tool_call_id"]
        conversation.extend([Message(**assistant), Message(**message)])
    provider = openai_provider(monkeypatch, messages)
    request = ChatRequest(
        messages=conversation,
        model="gpt-5.6-terra",
    )
    original = request.model_dump()
    captures = []
    budget_params = provider._budget_params

    def capture_budget(request, **kwargs):
        params = budget_params(request, **kwargs)
        captures.append(copy.deepcopy(params))
        return params

    class BeforeTransport(BaseException):
        pass

    async def capture_dispatch(params):
        captures.append(copy.deepcopy(params))
        raise BeforeTransport

    monkeypatch.setattr(provider, "_budget_params", capture_budget)
    monkeypatch.setattr(
        provider, "_guard_assembled_params_with_provider_count", capture_dispatch
    )
    hook._wrap_provider(provider, Coordinator(), 3)
    options = {"model": "gpt-5.6-terra", "reasoning_effort": "high"}
    provider.request_budget(request, context_estimate=10, request_options=options)
    with pytest.raises(BeforeTransport):
        await provider.complete(request, **options)
    assert captures[0] == captures[1]
    assert request.model_dump() == original
    outputs = [
        item
        for item in captures[0]["input"]
        if item.get("type") == "computer_call_output"
    ]
    assert len(outputs) == count
    calls = [
        item for item in captures[0]["input"] if item.get("type") == "computer_call"
    ]
    assert [item["call_id"] for item in calls] == [item["call_id"] for item in outputs]
    for index, item in enumerate(outputs):
        assert item["call_id"] == messages[index]["tool_call_id"]
        encoded = item["output"]["image_url"].split(",", 1)[1]
        assert (
            hashlib.sha256(base64.b64decode(encoded, validate=True)).hexdigest()
            == RESULTS[index % 2]["sha256"]
        )
    wire_text = json.dumps(captures[0]["input"])
    assert "function_call_output" not in wire_text
    assert all(
        message["content"]
        in "\n".join(
            block.get("text", "")
            for item in captures[0]["input"]
            for block in item.get("content", [])
        )
        for message in messages
    )


@pytest.mark.parametrize("case", ["halt", "missing", "malformed", "mixed-error"])
@async_case
async def test_real_provider_still_rejects_invalid_results_before_transport(
    monkeypatch, tmp_path, case
):
    from amplifier_core.llm_errors import InvalidRequestError

    message = retained_message()
    envelope = json.loads(message["content"])
    payload = json.loads(envelope["output"])
    if case == "halt":
        envelope.update(success=False, error={"code": "safety_halt"})
    elif case in {"missing", "malformed"}:
        path = tmp_path / "shot.png"
        if case == "malformed":
            path.write_bytes(b"not a screenshot")
        payload["images"] = [str(path)]
        envelope["output"] = json.dumps(payload)
    message["content"] = json.dumps(envelope)
    if case == "mixed-error":
        message = hook._expand_tool_results([message], 3)[0]
        message["content"][0]["text"] = "Safety halt: explicit resume required"
    provider = openai_provider(monkeypatch, [message])
    hook._wrap_provider(provider, Coordinator(), 3)
    typed = Message(**message)
    # Match the hook's plain block representation; preserve the mixed-error
    # content exactly, without Pydantic introducing format conversions.
    typed.content = message["content"]
    request = ChatRequest(messages=[typed], model="gpt-5.6-terra")
    original = copy.deepcopy(request)
    with pytest.raises(InvalidRequestError) as error:
        provider.request_budget(request, context_estimate=10)
    assert error.value.code == "computer_result_not_image"
    assert error.value.retryable is False
    with pytest.raises(InvalidRequestError):
        await provider.complete(request)
    assert request == original
