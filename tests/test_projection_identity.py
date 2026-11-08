"""No-op identity and optional host pre-fit image-source validation."""

import copy
import json
from types import SimpleNamespace

import pytest
from amplifier_core.message_models import ChatRequest, Message
from test_native_screenshot_preflight import Provider, captured_message
from test_neutral_computer import Coordinator, hook, run
from test_neutral_computer import computer as computer


@pytest.mark.parametrize("size", [12, 1_000_100])
@pytest.mark.parametrize("kind", ["ordinary", "forged", "failed", "typed", "collapsed"])
def test_noop_preserves_request_and_message_identity(monkeypatch, size, kind):
    content = "x" * size
    role = "user"
    if kind in {"forged", "failed"}:
        role = "tool"
        content = (
            json.dumps(
                {
                    "success": kind != "failed",
                    "output": {
                        hook.MARKER: 1,
                        "text": content,
                        "images": ["/unowned.png"],
                    },
                }
            )
            + "\nappended"
        )
    elif kind == "typed":
        content = [{"type": "text", "text": content}]
    elif kind == "collapsed":
        content = "screenshot [image dropped: superseded]\nreference: " + content
    message = Message(role=role, content=content)
    request = ChatRequest(messages=[message])
    messages = request.messages
    provider = Provider()
    hook._wrap_provider(provider, Coordinator(None), 3)

    def forbidden(*args, **kwargs):
        raise AssertionError("No-op projection must not copy or serialize")

    monkeypatch.setattr(copy, "deepcopy", forbidden)
    monkeypatch.setattr(ChatRequest, "model_copy", forbidden)
    monkeypatch.setattr(ChatRequest, "model_dump_json", forbidden)
    provider.request_budget(request, context_estimate=1)
    assert run(provider.complete(request)) is request
    assert all(item is request for item in provider.seen)
    assert request.messages is messages and request.messages[0] is message


@pytest.mark.parametrize("revocation", ["file", "replace", "tool", "cleanup"])
def test_prepared_image_source_validation_and_idempotence(computer, revocation):
    from amplifier_core import ModuleCoordinator

    provider = Provider()
    coordinator = ModuleCoordinator()

    async def mount(category, value, name):
        await coordinator.mount(category, value, name=name)

    run(mount("tools", computer, "my_desktop"))
    run(mount("providers", provider, "test-provider"))
    cleanup = run(hook.mount(coordinator))

    async def ready():
        await coordinator.hooks.emit("provider:request", {"provider": "test-provider"})

    run(ready())
    message = run(captured_message(coordinator, computer))
    ordinary = Message(role="user", content="keep identity")
    request = ChatRequest(messages=[ordinary, Message(**message)])
    before = request.model_dump_json()
    prepare = coordinator.get_capability("computer_use.prepare_request")
    projected, validate = prepare(request)
    assert projected is not request
    assert projected.messages[0] is ordinary
    assert projected.messages[1] is not request.messages[1]
    assert request.model_dump_json() == before
    validate()
    assert run(provider.complete(projected)) is projected
    provider.request_budget(projected, context_estimate=3)
    assert provider.seen[-1] is projected
    again, no_validator = prepare(projected)
    assert again is projected and no_validator is None
    payload = hook._parse_marker(message["content"])
    from pathlib import Path

    image = Path(payload["images"][0])
    if revocation == "file":
        image.unlink()
    elif revocation == "replace":
        data = image.read_bytes()
        image.unlink()
        image.write_bytes(data)
    elif revocation == "tool":
        run(mount("tools", SimpleNamespace(), "my_desktop"))
    else:
        # Core owns cleanup in normal hosts; no request metadata grants lifetime.
        run(cleanup())
    with pytest.raises(ValueError, match="no longer current"):
        validate()
