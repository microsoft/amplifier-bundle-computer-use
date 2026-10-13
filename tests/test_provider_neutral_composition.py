"""Real qualified provider wheels + real SDK parsing, intercepted HTTP only.

No private provenance is seeded, no provider source is changed, no SDK dependency
is skipped. Full declarations exercise supported ordinary fallback; deliberately
narrowed request intent exercises the provider's native subset independently.
These offline traces are NOT fresh wire, model or hardware acceptance.
"""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import httpx2
import pytest
from amplifier_core.message_models import ChatRequest, Message, ToolSpec
from amplifier_module_provider_anthropic import AnthropicProvider
from amplifier_module_provider_openai import OpenAIProvider
from anthropic import AsyncAnthropic
from jsonschema import validate
from openai import AsyncOpenAI
from test_neutral_computer import (
    CLICK,
    batch,
    core_coordinator,
    core_dispatch,
    hook,
    run,
)
from test_neutral_computer import computer as computer


@pytest.mark.parametrize("shape", ["full_intent_flat", "ordinary_canonical_batch"])
@pytest.mark.parametrize("same", [False, True])
@pytest.mark.parametrize("cancel_second", [False, True])
def test_actual_session_execute_call_local_approval(
    computer, shape, same, cancel_second, tmp_path
):
    """Actual Core Session + installed loop/context + SDK Mock, not native/live proof."""
    from amplifier_core import AmplifierSession, HookResult, ModuleLoader
    from test_neutral_computer import cu

    class OrdinaryBatchComputer(cu.ComputerTool):
        @property
        def native_tool_spec(self):
            # Explicit ordinary ToolSpec, recognized by the existing legacy
            # safety name. Do not claim full-intent OpenAI accepts batch replies.
            return {}

    tool = (
        computer
        if shape == "full_intent_flat"
        else OrdinaryBatchComputer(computer._backend, {"max_edge": 800})
    )
    if tool is not computer:
        tool.resolve_display()
    tool._gate_writes = True
    a = copy.deepcopy(CLICK)
    b = (
        copy.deepcopy(CLICK)
        if same
        else {"action": "left_click", "coordinate": [40, 50]}
    )
    inputs = (
        [a, b]
        if shape == "full_intent_flat"
        else [batch(a, capture=False), batch(b, capture=False)]
    )
    original = copy.deepcopy(inputs)
    trace, requests, caller_pairs = [], [], []

    def respond(request):
        body = json.loads(request.content)
        if "input_tokens" in request.url.path:
            return httpx.Response(200, json={"input_tokens": 100})
        requests.append(body)
        if len(requests) == 1:
            declaration = body["tools"][0]
            assert declaration["type"] == "function"
            assert declaration["name"] == "computer"
            output = [
                {
                    "type": "function_call",
                    "id": f"item{i}",
                    "call_id": f"call{i}",
                    "name": "computer",
                    "arguments": json.dumps(value),
                    "status": "completed",
                }
                for i, value in enumerate(inputs)
            ]
        else:
            output = [
                {
                    "type": "message",
                    "id": "done",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "observed", "annotations": []}
                    ],
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": f"response{len(requests)}",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "gpt-5.5",
                "output": output,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            },
        )

    async def scenario():
        allowed_finished = asyncio.Event()

        class Decisions:
            async def request_approval(self, prompt, options, timeout, default):
                index = sum(row["stage"] == "approval" for row in trace)
                trace.append(
                    {
                        "stage": "approval",
                        "ordinal": index,
                        "prompt": prompt,
                        "options": options,
                        "default": default,
                    }
                )
                assert options == ["Allow", "Deny"] and default == "deny"
                prepared = json.loads(prompt.split("\n", 1)[1])
                params = (
                    prepared if shape == "full_intent_flat" else prepared["actions"][0]
                )
                allowed = index == 0 if same else params["coordinate"] == [20, 30]
                if not allowed and cancel_second:
                    # Observe A actually finished before cancelling the other
                    # call; do not assume Core schedules SDK output in order.
                    await asyncio.wait_for(allowed_finished.wait(), 2)
                    raise asyncio.CancelledError()
                return "Allow" if allowed else "Deny"

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        sdk = AsyncOpenAI(
            api_key="offline-test", http_client=http_client, max_retries=0
        )
        provider = OpenAIProvider(
            api_key="offline-test",
            client=sdk,
            config={
                "default_model": "gpt-5.5",
                "use_streaming": False,
                "auto_continue": False,
                "tool_search": {"mode": "off"},
                "rate_limit_state_path": str(tmp_path / "rate-limit.json"),
            },
        )
        session = AmplifierSession(
            {
                "session": {
                    "orchestrator": {
                        "module": "loop-streaming",
                        "config": {"max_iterations": 3},
                    },
                    "context": {"module": "context-simple"},
                },
                "providers": [],
                "tools": [],
                "hooks": [],
            }
        )
        coordinator = session.coordinator
        coordinator.loader = ModuleLoader(coordinator=coordinator)
        execute = tool.execute

        async def observed(arguments):
            before = copy.deepcopy(arguments)
            try:
                result = await execute(arguments)
                if result.success:
                    allowed_finished.set()
                return result
            finally:
                caller_pairs.append((before, copy.deepcopy(arguments)))
                trace.append({"stage": "execute", "arguments": before})

        async def observe(event, data):
            trace.append({"stage": event, "data": copy.deepcopy(data)})
            return HookResult(action="continue")

        tool.execute = observed
        try:
            await session.initialize()
            coordinator.approval_system = Decisions()
            coordinator.register_capability("approval.interactive", True)
            await coordinator.mount("providers", provider, name="openai")
            await coordinator.mount("tools", tool, name="computer")
            await hook.mount(coordinator, {})
            coordinator.hooks.register(
                "tool:pre", observe, priority=1, name="trace-pre"
            )
            coordinator.hooks.register(
                "tool:post", observe, priority=100, name="trace-post"
            )
            try:
                await asyncio.wait_for(
                    session.execute("Execute two canonical requests."), 10
                )
            except asyncio.CancelledError:
                assert cancel_second
            except RuntimeError as exc:
                # Core2's public Session boundary maps coroutine cancellation
                # to a RuntimeError, rather than preserving CancelledError.
                assert (
                    cancel_second
                    and str(exc).strip() == "Execution failed: CancelledError:"
                )
                trace.append({"stage": "session-cancellation", "error": str(exc)})
            assert sum(row["stage"] == "approval" for row in trace) == 2
            assert tool._backend.calls == [("click", -1560, 90, "left", 1)]
            assert inputs == original
            assert caller_pairs and all(
                before == after for before, after in caller_pairs
            )
            assert len([row for row in trace if row["stage"] == "tool:pre"]) == 2
        finally:
            tool.execute = execute
            await session.cleanup()
            await sdk.close()
            (tmp_path / "session-approval-trace.json").write_text(
                json.dumps(
                    {
                        "scope": shape,
                        "same": same,
                        "cancel_second": cancel_second,
                        "events": trace,
                        "requests": requests,
                        "caller_pairs": caller_pairs,
                    },
                    default=str,
                )
            )

    run(scenario())


@pytest.mark.parametrize("provider_name", ["anthropic", "openai"])
def test_public_provider_complete_transmits_ordinary_schema_unchanged(
    computer, provider_name, tmp_path
):
    schema = computer.input_schema
    request = ChatRequest(
        messages=[Message(role="user", content="ordinary function schema")],
        tools=[
            ToolSpec(
                name="my_desktop", description=computer.description, parameters=schema
            )
        ],
    )
    original = copy.deepcopy(request.model_dump())
    seen = []
    http = httpx if provider_name == "openai" else httpx2

    def respond(wire_request):
        body = json.loads(wire_request.content)
        if (
            "input_tokens" in wire_request.url.path
            or "count_tokens" in wire_request.url.path
        ):
            return http.Response(200, json={"input_tokens": 1})
        seen.append(body)
        declaration = body["tools"][0]
        transmitted = declaration[
            "parameters" if provider_name == "openai" else "input_schema"
        ]
        assert declaration["name"] == "my_desktop"
        assert transmitted == schema
        validate(CLICK, transmitted)
        validate(batch(CLICK), transmitted)
        if provider_name == "openai":
            reply = {
                "id": "schema-response",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "gpt-5.5",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
        else:
            reply = {
                "id": "schema-message",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [{"type": "text", "text": "observed"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        return http.Response(200, json=reply)

    async def scenario():
        http_client = http.AsyncClient(transport=http.MockTransport(respond))
        config = {
            "use_streaming": False,
            "max_retries": 0,
            "auto_continue": False,
            "rate_limit_state_path": str(tmp_path / "rate-limit.json"),
        }
        if provider_name == "openai":
            sdk = AsyncOpenAI(
                api_key="offline-test", http_client=http_client, max_retries=0
            )
            provider = OpenAIProvider(
                api_key="offline-test",
                client=sdk,
                config={
                    **config,
                    "default_model": "gpt-5.5",
                    "tool_search": {"mode": "off"},
                },
            )
        else:
            sdk = AsyncAnthropic(
                api_key="offline-test", http_client=http_client, max_retries=0
            )
            provider = AnthropicProvider(
                api_key="offline-test",
                config={**config, "default_model": "claude-sonnet-5-5"},
            )
            provider._client = sdk
        try:
            await provider.complete(request)
            assert len(seen) == 1
            assert request.model_dump() == original
            assert computer._backend.calls == []
            disclosed = []
            computer._ensure_announced = lambda: disclosed.append(True)
            for bad in ({}, {**CLICK, "version": 1}, {"actions": [CLICK]}):
                original_bad = copy.deepcopy(bad)
                result = await computer.execute(bad)
                assert not result.success
                assert computer._backend.calls == []
                assert disclosed == []
                assert bad == original_bad
                assert request.model_dump() == original
        finally:
            await sdk.close()

    run(scenario())


@pytest.mark.parametrize(
    "provider_name,native",
    [
        ("anthropic", True),
        ("openai", False),
        ("openai", True),
    ],
)
@pytest.mark.parametrize("failure", [False, True])
def test_actual_sdk_reply_tool_execution_hook_budget_and_replay(
    computer, provider_name, native, failure, tmp_path
):
    # Use the actual emitted ToolSpec, including its alias and canonical schema.
    extra = computer.native_tool_spec
    if provider_name == "openai" and native:
        # OpenAI's provider correctly falls back for unsupported native semantics.
        # This separate request tests its explicitly narrower native contract;
        # it is NOT a claim that the full tool's declaration becomes native.
        extra["computer_use"]["operations"] = ["screenshot", "left_click", "key"]
    spec = ToolSpec(
        name="my_desktop",
        description=computer.description,
        parameters=computer.input_schema,
        **extra,
    )
    expected_arguments = batch(CLICK) if native else CLICK
    original_spec = copy.deepcopy(spec.model_dump())
    seen = []
    http = httpx if provider_name == "openai" else httpx2

    def respond(request):
        body = json.loads(request.content)
        if "input_tokens" in request.url.path or "count_tokens" in request.url.path:
            return http.Response(200, json={"input_tokens": 1})
        seen.append(body)
        if provider_name == "openai":
            if len(seen) == 1:
                output = (
                    [
                        {
                            "id": "cu_item",
                            "type": "computer_call",
                            "call_id": "call1",
                            "actions": [
                                {"type": "click", "x": 20, "y": 30, "button": "left"}
                            ],
                            "pending_safety_checks": [],
                            "status": "completed",
                        }
                    ]
                    if native
                    else [
                        {
                            "id": "fn_item",
                            "type": "function_call",
                            "call_id": "call1",
                            "name": "my_desktop",
                            "arguments": json.dumps(CLICK),
                            "status": "completed",
                        }
                    ]
                )
            else:
                output = [
                    {
                        "id": "msg2",
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "observed",
                                "annotations": [],
                            }
                        ],
                        "status": "completed",
                    }
                ]
            response = {
                "id": f"resp{len(seen)}",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "gpt-5.5",
                "output": output,
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
        else:
            content = (
                [
                    {
                        "type": "tool_use",
                        "id": "call1",
                        "name": "left_click",
                        "input": {"coordinate": [20, 30]},
                        "toolset_name": "computer",
                    }
                ]
                if len(seen) == 1
                else [{"type": "text", "text": "observed"}]
            )
            response = {
                "id": f"msg{len(seen)}",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": content,
                "stop_reason": "tool_use" if len(seen) == 1 else "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        return http.Response(200, json=response)

    async def scenario():
        transport = http.MockTransport(respond)
        http_client = http.AsyncClient(transport=transport)
        config = {
            "use_streaming": False,
            "max_retries": 0,
            "auto_continue": False,
            "rate_limit_state_path": str(tmp_path / "rate-limit.json"),
        }
        if provider_name == "openai":
            sdk = AsyncOpenAI(
                api_key="offline-test", http_client=http_client, max_retries=0
            )
            provider = OpenAIProvider(
                api_key="offline-test",
                client=sdk,
                config={
                    **config,
                    "default_model": "gpt-5.5",
                    "tool_search": {"mode": "off"},
                },
            )
        else:
            sdk = AsyncAnthropic(
                api_key="offline-test", http_client=http_client, max_retries=0
            )
            provider = AnthropicProvider(
                api_key="offline-test",
                config={
                    **config,
                    "default_model": "claude-sonnet-5-5",
                    "refusal_fallback_enabled": False,
                },
            )
            # Inject only the SDK transport, not native provenance/history.
            provider._client = sdk
        try:
            coordinator = await core_coordinator(computer, provider)
            assert getattr(provider, hook._WRAPPED_FLAG)
            request = ChatRequest(
                messages=[Message(role="user", content="click the visible button")],
                tools=[spec],
            )
            before = copy.deepcopy(request.model_dump())
            reply = await provider.complete(request)
            assert request.model_dump() == before
            call = reply.tool_calls[0]
            assert call.name == "my_desktop"
            assert call.arguments == expected_arguments
            if failure:

                def fail():
                    from amplifier_module_tool_computer_use.backend import BackendError

                    raise BackendError("offline injected failure after possible input")

                computer._backend.on_input = fail
            result = await core_dispatch(
                coordinator, computer, call.arguments, call_id=call.id, name=call.name
            )
            assert result.success is not failure
            # Carry exactly the provider-produced blocks/metadata, never private seeding.
            history = [
                request.messages[0],
                Message(
                    role="assistant",
                    content=reply.content,
                    **({"metadata": reply.metadata} if reply.metadata else {}),
                ),
                Message(
                    role="tool",
                    tool_call_id=call.id,
                    name=call.name,
                    content=result.model_dump_json(),
                ),
            ]
            follow = ChatRequest(messages=history, tools=[spec])
            before_follow = copy.deepcopy(follow.model_dump())
            if failure and native and provider_name == "openai":
                # The provider's native wire requires a successful final image.
                # An execution error is preserved locally and refused safely,
                # not recaptured, promoted to success, or sent as malformed wire.
                from amplifier_core.llm_errors import InvalidRequestError

                projected = hook._expand_tool_results(follow.messages, 3)
                assert projected[-1].content == history[-1].content
                with pytest.raises(InvalidRequestError, match="failed computer result"):
                    provider.request_budget(follow, context_estimate=10)
                with pytest.raises(InvalidRequestError, match="failed computer result"):
                    await provider.complete(follow)
                assert follow.model_dump() == before_follow
                assert len(seen) == 1
                assert [c[0] for c in computer._backend.calls] == ["click"]
                return
            budget = provider.request_budget(follow, context_estimate=10)
            if hasattr(budget, "__await__"):
                await budget
            assert follow.model_dump() == before_follow
            await provider.complete(follow)
            assert follow.model_dump() == before_follow
            assert spec.model_dump() == original_spec
            assert len(seen) == 2

            def check_keys(value):
                if isinstance(value, dict):
                    assert "computer_use" not in value
                    for child in value.values():
                        check_keys(child)
                elif isinstance(value, list):
                    for child in value:
                        check_keys(child)

            check_keys(seen)
            wire = json.dumps(seen[-1])
            if failure:
                assert "offline injected failure" in wire
                assert "data:image/png" not in wire and '"type": "image"' not in wire
            elif native:
                assert "data:image/png" in wire or '"type": "image"' in wire
            assert [c[0] for c in computer._backend.calls] == (
                ["click", "capture"] if native and not failure else ["click"]
            )
        finally:
            await sdk.close()

    run(scenario())


@pytest.mark.parametrize("provider_name", ["anthropic", "openai"])
@pytest.mark.parametrize("name", ["computer", "screenshot"])
def test_real_request_assembly_keeps_ordinary_name_collisions_ordinary(
    provider_name, name
):
    request = ChatRequest(
        messages=[Message(role="user", content="ordinary function request")],
        tools=[
            ToolSpec(
                name=name,
                description="ordinary",
                parameters={"type": "object", "properties": {}},
            )
        ],
    )
    before = copy.deepcopy(request.model_dump())
    if provider_name == "openai":
        sdk = AsyncOpenAI(
            api_key="offline-test",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: (_ for _ in ()).throw(
                        AssertionError("no HTTP expected")
                    )
                )
            ),
        )
        provider = OpenAIProvider(
            api_key="offline-test",
            client=sdk,
            config={
                "default_model": "gpt-5.5",
                "tool_search": {"mode": "off"},
            },
        )
        try:
            params, _, _ = provider._assemble_initial_responses_params(request)
            assert params["tools"][0]["type"] == "function"
            assert params["tools"][0]["name"] == name
        finally:
            run(sdk.close())
    else:
        provider = AnthropicProvider(
            api_key="offline-test", config={"default_model": "claude-sonnet-5-5"}
        )
        assembly = provider._assemble_request_params(
            request,
            request_options={"model": "claude-sonnet-5-5"},
            request_caps=provider._get_capabilities("claude-sonnet-5-5"),
        )
        declaration = assembly.params["tools"][0]
        assert declaration.get("type") in (None, "custom")
        assert declaration["name"] == name
        assert "input_schema" in declaration
    assert request.model_dump() == before
