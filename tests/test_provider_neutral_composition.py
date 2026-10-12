"""Real qualified provider wheels + real SDK parsing, intercepted HTTP only.

No private provenance is seeded, no provider source is changed, no SDK dependency
is skipped. Full declarations exercise supported ordinary fallback; deliberately
narrowed request intent exercises the provider's native subset independently.
These offline traces are NOT fresh wire, model or hardware acceptance.
"""

from __future__ import annotations

import copy
import json

import httpx
import httpx2
import pytest
from amplifier_core.message_models import ChatRequest, Message, ToolSpec
from amplifier_module_provider_anthropic import AnthropicProvider
from amplifier_module_provider_openai import OpenAIProvider
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from test_neutral_computer import CLICK, Coordinator, batch, hook, run
from test_neutral_computer import computer as computer


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
            assert hook._wrap_provider(provider, Coordinator(computer), 3)
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
            result = await computer.execute(call.arguments)
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
