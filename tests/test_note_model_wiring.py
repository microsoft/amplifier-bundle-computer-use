"""Unit tests for provider-native tool selection in hook-computer-use.

`ComputerTool.note_model()` existed with zero callers before this fix - its own
docstring and a comment in tool-computer-use's `__init__.py` (~line 190) both
assert that hook-computer-use calls it on every `provider:request` with the
model actually about to be used. It never did: `_tool_version` was resolved
once at mount from `config["model"]` and never corrected, even though the
exact defect `tool_versions.py` exists to prevent (a model/tool_version
mismatch 400s *every* request) requires exactly this correction to fire.

The hook selects a provider's wire dialect before the orchestrator reads the
mounted tool's native spec, then reselects it only for wrapped requests that
declare the computer tool.
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import amplifier_module_hook_computer_use as hook_mod
import pytest
from amplifier_module_tool_computer_use import ComputerTool
from amplifier_module_tool_computer_use.geometry import Display


class _FakeBackend:
    """Minimal stand-in satisfying `ComputerTool.__init__`'s only two backend
    reads: `is_remote` (attribute, defaults False if absent) and `type_text`
    (its signature is inspected once at construction)."""

    is_remote = False

    def type_text(self, text: str) -> None:  # pragma: no cover - never invoked
        pass


def _with_resolved_display(computer: ComputerTool) -> ComputerTool:
    """`native_tool_spec` requires display geometry to already be resolved
    (normally done once by `mount()`, via `resolve_display()`, against a real
    backend). These tests exercise tool_version resolution only, so a fixed
    `Display` is set directly rather than driving a fake backend through the
    full monitor-enumeration path."""
    computer._display = Display(
        screen_width=1920, screen_height=1080, model_width=1280, model_height=720
    )
    return computer


class _AnthropicProviderNoStream:
    """Today's real shape: `complete()` plus a working
    `_derive_native_tool_betas()` (PR #79) - must wrap successfully.

    `default_model` mirrors the real provider attribute the fix reads:
    the model this provider instance actually answers with when no
    per-request override is set (`ChatRequest.model` is `None`, the common
    case - see `_note_model_on_computer_tool`'s docstring).
    """

    __module__ = "amplifier_module_provider_anthropic"

    def __init__(self, default_model: str | None = None) -> None:
        self.default_model = default_model

    async def complete(self, request, **kwargs):
        return "ok"

    def _derive_native_tool_betas(self, tools):
        return ["computer-use-2025-11-24"] if tools else []


class _OpenAIProviderNoStream:
    __module__ = "amplifier_module_provider_openai"
    tool_search_mode = "off"

    def __init__(self, default_model: str | None = None) -> None:
        self.default_model = default_model
        self.helper_calls = 0

    async def complete(self, request, **kwargs):
        return "ok"

    def get_native_computer_tool_spec(self):
        self.helper_calls += 1
        return {"type": "computer"}

    def _convert_tools_from_request(self, tools, model_name=None):
        converted = []
        for tool in tools:
            if getattr(tool, "type", None) == "computer":
                converted.append({"type": "computer"})
            else:
                converted.append(
                    {
                        "type": "function",
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    }
                )
        return converted


class _OpenAIProviderFunctionFallback(_OpenAIProviderNoStream):
    """A valid function-tool conversion proves the probe is ToolSpec-shaped."""

    get_native_computer_tool_spec = None

    def __init__(self) -> None:
        super().__init__()
        self.probe_fields: list[tuple[str, str, dict, str | None]] = []

    def _convert_tools_from_request(self, tools, model_name=None):
        self.probe_fields = [
            (tool.name, tool.description, tool.parameters, getattr(tool, "type", None))
            for tool in tools
        ]
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in tools
        ]


class _FakeRequest:
    def __init__(self, model: str | None, tools: list | None = None) -> None:
        self.messages: list = []
        self.model = model
        self.tools = [] if tools is None else tools


def _computer_tool() -> dict[str, str]:
    return {"name": "computer"}


class _HookRegistry:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def register(self, event, handler, **kwargs) -> None:
        self.handlers[event] = handler


class _FakeCoordinator:
    """Same shape used across the existing hook-computer-use test suite
    (test_gate_hook.py, test_hook_stream_guard.py): `get("tools", name)`
    resolves a mounted tool by name; anything else (e.g. `"orchestrator"`)
    returns `None`, so `_fail_if_native_tool_passthrough_unsupported`'s
    orchestrator probe finds nothing to probe and skips it."""

    def __init__(self, tools: dict, providers: dict | None = None) -> None:
        self._tools = tools
        self._providers = providers or {}
        self.hooks = _HookRegistry()

    def get(self, mount_point, name=None):
        if mount_point == "tools":
            return self._tools.get(name) if name else self._tools
        if mount_point == "providers":
            return self._providers.get(name) if name else self._providers
        return None


def _run(coro):
    """Run a coroutine without clearing the suite's current event loop.

    Some later tests use ``asyncio.get_event_loop().run_until_complete(...)``.
    Unlike ``asyncio.run()``, this leaves that shared compatibility loop
    available after this file's synchronous tests complete.
    """
    return asyncio.get_event_loop().run_until_complete(coro)


def _mount_and_dispatch_provider_request(
    coordinator: _FakeCoordinator, provider_name: str
) -> None:
    _run(hook_mod.mount(coordinator))
    handler = coordinator.hooks.handlers[hook_mod.PROVIDER_REQUEST]
    _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": provider_name}))


@pytest.mark.parametrize(
    "default_model",
    [
        "provider-alpha",
        "provider-beta",
        "provider-beta-max",
        "opaque-model-a",
        "opaque-model-b",
        "opaque-model-v5.10-a",
        "opaque-model-v10-b",
        "opaque-future-model",
    ],
)
def test_provider_request_selects_bare_openai_spec_for_current_and_future_aliases(
    default_model: str,
):
    """The provider behavior, not alias/version parsing, selects bare computer."""
    computer = _with_resolved_display(ComputerTool(_FakeBackend(), {}))
    provider = _OpenAIProviderNoStream(default_model)
    coordinator = _FakeCoordinator({"computer": computer}, {"openai": provider})

    _mount_and_dispatch_provider_request(coordinator, "openai")

    assert computer.native_tool_spec == {"type": "computer"}
    assert computer.native_beta_header is None
    assert _run(provider.complete(_FakeRequest("claude-sonnet-4-5-20250929"))) == "ok"
    assert computer.native_tool_spec == {"type": "computer"}


def test_complete_valid_function_fallback_is_a_negative_probe_without_traceback(caplog):
    provider = _OpenAIProviderFunctionFallback()
    computer = _with_resolved_display(ComputerTool(_FakeBackend(), {}))
    coordinator = _FakeCoordinator({"computer": computer}, {"openai": provider})

    with caplog.at_level(logging.DEBUG, logger=hook_mod.__name__):
        _mount_and_dispatch_provider_request(coordinator, "openai")

    assert provider.probe_fields == [
        (
            "__computer_use_native_computer_probe__",
            "native computer-use compatibility probe",
            {"type": "object", "properties": {}},
            "computer",
        )
    ]
    assert all(record.exc_info is None for record in caplog.records)
    assert "Traceback" not in caplog.text
    assert any(record.levelno == logging.WARNING for record in caplog.records)


@pytest.mark.parametrize("with_stub", [False, True])
@pytest.mark.parametrize(
    "provider_factory", [_OpenAIProviderNoStream, _OpenAIProviderFunctionFallback]
)
def test_provider_request_without_computer_skips_integration(
    with_stub, provider_factory, caplog
):
    provider = provider_factory()
    original_complete = provider.complete
    tools = {"computer_use_unavailable": object()} if with_stub else {}
    lookups = []

    class Coordinator(_FakeCoordinator):
        def get(self, mount_point, name=None):
            lookups.append((mount_point, name))
            return super().get(mount_point, name)

    coordinator = Coordinator(tools, {"openai": provider})
    _run(hook_mod.mount(coordinator))
    handler = coordinator.hooks.handlers[hook_mod.PROVIDER_REQUEST]
    lookups.clear()
    with caplog.at_level(logging.WARNING, logger=hook_mod.__name__):
        for _ in range(2):
            result = _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "openai"}))
            assert result.action == "continue"

    assert lookups == [("tools", "computer"), ("tools", "computer")]
    assert provider.complete == original_complete
    assert not caplog.records
    for flag in (
        hook_mod._WRAPPED_FLAG,
        hook_mod._UNSUPPORTED_WARNING_FLAG,
        hook_mod._NATIVE_COMPUTER_DIALECT_CACHE_ATTR,
    ):
        assert not hasattr(provider, flag)


def test_provider_request_rechecks_computer_after_activation_and_removal(caplog):
    provider = _OpenAIProviderNoStream()
    coordinator = _FakeCoordinator(
        {"computer_use_unavailable": object()}, {"openai": provider}
    )
    _run(hook_mod.mount(coordinator))
    handler = coordinator.hooks.handlers[hook_mod.PROVIDER_REQUEST]
    _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "openai"}))
    assert provider.helper_calls == 0
    assert not getattr(provider, hook_mod._WRAPPED_FLAG, False)

    computer = _with_resolved_display(ComputerTool(_FakeBackend(), {}))
    coordinator._tools = {"computer": computer}
    _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "openai"}))
    assert provider.helper_calls == 1
    assert getattr(provider, hook_mod._WRAPPED_FLAG) is True
    assert computer.native_tool_spec == {"type": "computer"}

    # No cached "present" decision: removal must stop even provider lookup.
    coordinator._tools.clear()
    coordinator._providers.clear()
    with caplog.at_level(logging.WARNING, logger=hook_mod.__name__):
        result = _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "openai"}))
    assert result.action == "continue"
    assert not caplog.records


def test_unsupported_warning_remains_once_after_computer_activation(caplog):
    provider = _OpenAIProviderFunctionFallback()
    coordinator = _FakeCoordinator({}, {"openai": provider})
    _run(hook_mod.mount(coordinator))
    handler = coordinator.hooks.handlers[hook_mod.PROVIDER_REQUEST]
    with caplog.at_level(logging.WARNING, logger=hook_mod.__name__):
        _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "openai"}))
        assert not caplog.records
        assert not hasattr(provider, hook_mod._UNSUPPORTED_WARNING_FLAG)
        coordinator._tools["computer"] = _with_resolved_display(
            ComputerTool(_FakeBackend(), {})
        )
        for _ in range(2):
            _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "openai"}))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "did not prove native computer passthrough" in warnings[0].getMessage()
    assert not getattr(provider, hook_mod._WRAPPED_FLAG, False)


def test_computer_lookup_failure_warns_and_preserves_provider_checks(caplog):
    provider = _OpenAIProviderNoStream()
    computer = _with_resolved_display(ComputerTool(_FakeBackend(), {}))

    class Coordinator(_FakeCoordinator):
        failing = True

        def get(self, mount_point, name=None):
            if (mount_point, name) == ("tools", "computer") and self.failing:
                raise RuntimeError("test lookup failure")
            return super().get(mount_point, name)

    coordinator = Coordinator({"computer": computer}, {"openai": provider})
    _run(hook_mod.mount(coordinator))
    handler = coordinator.hooks.handlers[hook_mod.PROVIDER_REQUEST]
    with caplog.at_level(logging.WARNING, logger=hook_mod.__name__):
        result = _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "openai"}))
    assert result.action == "continue"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "computer tool lookup failed" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None
    assert provider.helper_calls == 1
    assert getattr(provider, hook_mod._WRAPPED_FLAG) is True

    coordinator.failing = False
    _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "openai"}))
    assert provider.helper_calls == 1
    assert computer.native_tool_spec == {"type": "computer"}


def test_provider_request_reselects_shared_tool_dialect_and_keeps_anthropic_continuity():
    computer = _with_resolved_display(ComputerTool(_FakeBackend(), {}))
    anthropic = _AnthropicProviderNoStream("claude-sonnet-4-5-20250929")
    openai = _OpenAIProviderNoStream("opaque-future-model")
    coordinator = _FakeCoordinator(
        {"computer": computer}, {"anthropic": anthropic, "openai": openai}
    )
    _run(hook_mod.mount(coordinator))
    handler = coordinator.hooks.handlers[hook_mod.PROVIDER_REQUEST]

    _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "anthropic"}))
    assert computer.native_tool_spec["type"] == "computer_20250124"

    _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "anthropic"}))
    assert computer.native_tool_spec["type"] == "computer_20250124"

    _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "openai"}))
    assert computer.native_tool_spec == {"type": "computer"}

    _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "openai"}))
    assert computer.native_tool_spec == {"type": "computer"}
    assert openai.helper_calls == 1

    # The same, already-wrapped provider comes back with an unknown model after
    # a cross-dialect switch, so it resets to its canonical seed.
    anthropic.default_model = "opaque-future-model"
    _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "anthropic"}))
    assert computer.native_tool_spec["type"] == "computer_20251124"

    # Once a same-dialect model resolves, an unknown successor keeps it.
    anthropic.default_model = "claude-sonnet-4-5-20250929"
    _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "anthropic"}))
    assert computer.native_tool_spec["type"] == "computer_20250124"
    anthropic.default_model = "opaque-future-model"
    _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "anthropic"}))
    assert computer.native_tool_spec["type"] == "computer_20250124"


def test_provider_selection_preserves_same_dialect_overrides_and_ignores_cross_dialect(
    caplog: pytest.LogCaptureFixture,
):
    computer = _with_resolved_display(
        ComputerTool(_FakeBackend(), {"tool_version": "computer_20250124"})
    )
    anthropic = _AnthropicProviderNoStream("opaque-future-model")
    openai = _OpenAIProviderNoStream("opaque-future-model")
    coordinator = _FakeCoordinator(
        {"computer": computer}, {"anthropic": anthropic, "openai": openai}
    )
    _run(hook_mod.mount(coordinator))
    handler = coordinator.hooks.handlers[hook_mod.PROVIDER_REQUEST]

    _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "anthropic"}))
    assert computer.native_tool_spec["type"] == "computer_20250124"
    _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "openai"}))
    assert computer.native_tool_spec == {"type": "computer"}

    cross_dialect = _with_resolved_display(
        ComputerTool(_FakeBackend(), {"tool_version": "computer"})
    )
    cross_coordinator = _FakeCoordinator(
        {"computer": cross_dialect}, {"anthropic": anthropic}
    )
    _run(hook_mod.mount(cross_coordinator))
    cross_handler = cross_coordinator.hooks.handlers[hook_mod.PROVIDER_REQUEST]
    caplog.set_level(logging.INFO, logger="amplifier_module_tool_computer_use")
    _run(cross_handler(hook_mod.PROVIDER_REQUEST, {"provider": "anthropic"}))
    _run(cross_handler(hook_mod.PROVIDER_REQUEST, {"provider": "anthropic"}))

    assert cross_dialect.native_tool_spec["type"] == "computer_20251124"
    assert (
        sum(
            "ignoring cross-dialect configured tool_version" in record.message
            for record in caplog.records
        )
        == 1
    )


def test_provider_request_uses_note_model_for_a_legacy_tool_without_selection_api():
    class _LegacyTool:
        def __init__(self) -> None:
            self.models: list[str | None] = []

        def note_model(self, model: str | None) -> None:
            self.models.append(model)

    legacy_tool = _LegacyTool()
    provider = _AnthropicProviderNoStream("claude-opus-5")
    coordinator = _FakeCoordinator({"computer": legacy_tool}, {"anthropic": provider})

    _mount_and_dispatch_provider_request(coordinator, "anthropic")
    assert legacy_tool.models == ["claude-opus-5"]
    assert (
        _run(
            provider.complete(
                _FakeRequest("claude-sonnet-4-5-20250929", [_computer_tool()])
            )
        )
        == "ok"
    )
    assert legacy_tool.models == ["claude-opus-5", "claude-sonnet-4-5-20250929"]


def test_note_model_is_never_called_without_the_fix_baseline_sanity():
    """Sanity check on ComputerTool itself: mount-time config alone resolves
    `_tool_version` once and never corrects it on its own - only `note_model()`
    does. Establishes the baseline the rest of this file proves
    hook-computer-use now drives for real."""
    computer = ComputerTool(_FakeBackend(), {"model": "claude-opus-5"})
    assert computer._tool_version == "computer_20251124"


def test_wrapped_complete_forwards_request_model_to_note_model_and_corrects_tool_version(
    caplog: pytest.LogCaptureFixture,
):
    """The core reachable defect from Plan A1: mount-time config says
    claude-opus-5 (-> computer_20251124), but the model actually about to
    receive THIS request is claude-sonnet-4-5 (-> computer_20250124). Driving
    the wrapped `provider.complete()` must correct `_tool_version` - this is
    exactly the live-session scenario `note_model`'s own docstring promises,
    and before this fix could never happen because nothing ever called it.

    Bug-hunt defect A: the correction is logged at INFO
    (`amplifier_module_tool_computer_use`, where `note_model` actually lives),
    not WARNING - it is expected, working-as-designed behavior, not an
    operator-facing signal. See `test_tool_version_correction_does_not_reach_console_at_default_level`
    below for the console-visibility half of this fix.
    """
    computer = ComputerTool(_FakeBackend(), {"model": "claude-opus-5"})
    assert computer._tool_version == "computer_20251124"  # mount-time baseline

    coord = _FakeCoordinator({"computer": computer})
    provider = _AnthropicProviderNoStream()
    assert hook_mod._wrap_provider(provider, coord, max_inline=3) is True

    caplog.set_level(logging.INFO, logger="amplifier_module_tool_computer_use")
    request = _FakeRequest(model="claude-sonnet-4-5-20250929", tools=[_computer_tool()])
    result = _run(provider.complete(request))

    assert result == "ok"
    assert computer._tool_version == "computer_20250124"
    assert any(
        "correcting" in rec.message
        and "computer_20250124" in rec.message
        and rec.levelno == logging.INFO
        for rec in caplog.records
    )


def test_tool_version_correction_does_not_reach_console_at_default_level():
    """Bug-hunt defect A: an internal self-correction (right information,
    wrong audience - same class as the mount-noise fix) must not print to a
    real user's console. This app's DEFAULT logging configuration has no
    handlers anywhere and a root effective level of WARNING (measured via
    plain `logging.getLogger()` with no fixtures involved) - simulate that
    real console with our own `StreamHandler` rather than trusting `caplog`
    (which installs its own capture regardless of app config) and prove
    nothing is written to it when a real correction fires.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = [handler]
    root.setLevel(logging.WARNING)  # this app's measured default
    try:
        computer = ComputerTool(_FakeBackend(), {"model": "claude-opus-5"})
        coord = _FakeCoordinator({"computer": computer})
        provider = _AnthropicProviderNoStream(default_model="claude-haiku-4-5-20251001")
        # Wrap-time priming alone triggers a real correction (mount-time
        # config says claude-opus-5, this provider's default is Haiku) -
        # exactly the Haiku sub-agent scenario reported in the defect.
        hook_mod._wrap_provider(provider, coord, max_inline=3)
    finally:
        root.handlers, root.level = saved_handlers, saved_level

    assert computer._tool_version == "computer_20250124"  # correction still happened
    assert stream.getvalue() == "", (
        f"expected nothing on the console at the default level, got: "
        f"{stream.getvalue()!r}"
    )


def test_wrapped_complete_never_raises_when_request_has_no_model_attribute():
    """A request shape with no `model` attribute at all must not break the
    request - `note_model(None)` is a documented, tested no-op/keep-previous
    case (see tests/test_tool_versions.py)."""
    computer = ComputerTool(_FakeBackend(), {"model": "claude-opus-5"})
    coord = _FakeCoordinator({"computer": computer})
    provider = _AnthropicProviderNoStream()
    hook_mod._wrap_provider(provider, coord, max_inline=3)

    class _RequestNoModel:
        messages: list = []

    result = _run(provider.complete(_RequestNoModel()))
    assert result == "ok"
    assert computer._tool_version == "computer_20251124"  # unchanged, no flapping


# -- Issue #1: mount-time priming closes the "one turn late" gap -----------


def test_wrap_provider_primes_tool_version_before_any_request_is_sent():
    """The defect that 400s a sub-agent's FIRST and only request: mount-time
    config says claude-opus-5 (-> computer_20251124), but this provider
    instance's `default_model` is actually claude-haiku-4-5 (->
    computer_20250124, issue #1's verified row). Before this fix, nothing
    corrected `_tool_version` until AFTER `provider.complete()` ran once -
    one turn too late for a session that only gets one turn.

    `_wrap_provider` must correct it as a side effect of wrapping alone,
    with `complete()` never having been called at all.
    """
    computer = ComputerTool(_FakeBackend(), {"model": "claude-opus-5"})
    assert computer._tool_version == "computer_20251124"  # mount-time baseline

    coord = _FakeCoordinator({"computer": computer})
    provider = _AnthropicProviderNoStream(default_model="claude-haiku-4-5-20251001")
    assert hook_mod._wrap_provider(provider, coord, max_inline=3) is True

    # No request sent yet - priming alone must have already corrected this.
    assert computer._tool_version == "computer_20250124"


def test_native_tool_spec_read_before_the_first_request_is_already_correct():
    """The ordering bug, demonstrated the way the orchestrator actually
    triggers it: `native_tool_spec` (which the orchestrator's ToolSpec
    construction reads to build the tool list) is read BEFORE
    `provider.complete()` is ever called for the turn. A correction that
    only takes effect *inside* `complete()` arrives one read too late for a
    sub-agent's single turn - this test fails without wrap-time priming,
    because reading `native_tool_spec` here happens strictly before any
    `complete()` call exists to correct it.
    """
    computer = _with_resolved_display(
        ComputerTool(_FakeBackend(), {"model": "claude-opus-5"})
    )
    coord = _FakeCoordinator({"computer": computer})
    provider = _AnthropicProviderNoStream(default_model="claude-haiku-4-5-20251001")
    hook_mod._wrap_provider(provider, coord, max_inline=3)

    # Simulates the orchestrator building this turn's ToolSpec: read BEFORE
    # complete() is ever invoked. A sub-agent that 400s on this exact request
    # never gets a second read - this must already be right.
    assert computer.native_tool_spec["type"] == "computer_20250124"


def test_wrap_provider_priming_leaves_a_verified_model_unchanged():
    """No regression to the parent/long-lived-session path: an
    already-verified model (claude-opus-5) must still resolve to
    computer_20251124 after wrap-time priming - the fix must not flip a
    correct pairing to something else."""
    computer = _with_resolved_display(
        ComputerTool(_FakeBackend(), {"model": "claude-opus-5"})
    )
    coord = _FakeCoordinator({"computer": computer})
    provider = _AnthropicProviderNoStream(default_model="claude-opus-5")
    hook_mod._wrap_provider(provider, coord, max_inline=3)

    assert computer._tool_version == "computer_20251124"
    assert computer.native_tool_spec["type"] == "computer_20251124"


def test_wrapped_complete_falls_back_to_provider_default_model_when_request_model_is_none():
    """`request.model` is normally `None` (no per-request override) - the
    wrapped `complete()` must still resolve the correct tool_version from
    `provider.default_model` on every request, not only at wrap time. Proves
    the per-request half of the fix independently of mount-time priming."""
    computer = ComputerTool(_FakeBackend(), {"model": "claude-opus-5"})
    coord = _FakeCoordinator({"computer": computer})
    provider = _AnthropicProviderNoStream(default_model="claude-haiku-4-5-20251001")
    hook_mod._wrap_provider(provider, coord, max_inline=3)

    # Reset to the wrong value as if priming had not run, to isolate what
    # complete() alone corrects.
    computer._tool_version = "computer_20251124"
    result = _run(provider.complete(_FakeRequest(model=None, tools=[_computer_tool()])))

    assert result == "ok"
    assert computer._tool_version == "computer_20250124"


def test_wrapped_complete_prefers_an_explicit_request_model_override_over_default_model():
    """When a caller DOES set a per-request override, it is more specific
    than the provider-wide default and must win."""
    computer = ComputerTool(_FakeBackend(), {"model": "claude-opus-5"})
    coord = _FakeCoordinator({"computer": computer})
    provider = _AnthropicProviderNoStream(default_model="claude-opus-5")
    hook_mod._wrap_provider(provider, coord, max_inline=3)

    request = _FakeRequest(model="claude-sonnet-4-5-20250929", tools=[_computer_tool()])
    result = _run(provider.complete(request))

    assert result == "ok"
    assert computer._tool_version == "computer_20250124"


def test_wrapped_noncomputer_request_does_not_change_shared_tool_version():
    """An unrelated background request must not poison the next computer turn.

    The provider object is shared with the root session. A Haiku request with no
    ``computer`` declaration must leave the Opus-selected native tool type intact.
    """
    computer = ComputerTool(_FakeBackend(), {"model": "claude-opus-5"})
    coord = _FakeCoordinator({"computer": computer})
    provider = _AnthropicProviderNoStream(default_model="claude-opus-5")
    hook_mod._wrap_provider(provider, coord, max_inline=3)

    request = _FakeRequest(
        model="claude-haiku-4-5-20251001", tools=[{"name": "read_file"}]
    )
    result = _run(provider.complete(request))

    assert result == "ok"
    assert computer._tool_version == "computer_20251124"


def test_wrap_provider_re_primes_tool_version_on_every_turn_even_when_already_wrapped():
    """Multi-provider routing-matrix defect (the user's exact wire error).

    A session's routing matrix can route different turns to DIFFERENT
    provider instances (e.g. `claude-opus-5` for `reasoning`, a Haiku
    sub-agent for `fast` utility work) that all share ONE coordinator and
    therefore ONE mounted `computer` tool. `_wrap_provider` only primes
    `_tool_version` the FIRST time it wraps a GIVEN provider instance
    (gated by `_WRAPPED_FLAG`); once opus is wrapped and has run a turn, a
    LATER wrap of a *different* provider instance (Haiku) re-primes the
    SAME shared tool for Haiku's model. When the routing matrix comes back
    to opus for its next turn, `_wrap_provider(opus_provider, ...)` is
    called again (PROVIDER_REQUEST fires every turn) but early-returns
    immediately (already wrapped) WITHOUT re-priming - so the orchestrator's
    `native_tool_spec` read for opus's next turn still sees Haiku's stale
    `computer_20250124`, one full turn before opus's own wrapped
    `complete()` ever runs to correct it back. That stale read is what goes
    out on the wire and gets rejected: "claude-opus-5 does not support tool
    types: computer_20250124".
    """
    computer = _with_resolved_display(
        ComputerTool(_FakeBackend(), {"model": "claude-opus-5"})
    )
    coord = _FakeCoordinator({"computer": computer})

    opus_provider = _AnthropicProviderNoStream(default_model="claude-opus-5")
    assert hook_mod._wrap_provider(opus_provider, coord, max_inline=3) is True
    assert computer._tool_version == "computer_20251124"  # opus turn 1: correct

    # A DIFFERENT provider instance (a Haiku sub-agent under the same
    # session's routing matrix) wraps for its own first turn - the SAME
    # shared `computer` tool, primed from Haiku's default_model.
    haiku_provider = _AnthropicProviderNoStream(
        default_model="claude-haiku-4-5-20251001"
    )
    assert hook_mod._wrap_provider(haiku_provider, coord, max_inline=3) is True
    assert computer._tool_version == "computer_20250124"  # now wrong for opus

    # The routing matrix comes back to opus for its NEXT turn:
    # PROVIDER_REQUEST fires for opus again (already wrapped), and
    # native_tool_spec is read BEFORE opus's own complete() runs. This must
    # already be correct - not one turn late.
    hook_mod._wrap_provider(opus_provider, coord, max_inline=3)
    assert computer._tool_version == "computer_20251124"
    assert computer.native_tool_spec["type"] == "computer_20251124"


def test_wrapped_complete_tolerates_a_coordinator_that_cannot_find_the_tool():
    """No `computer` tool mounted (e.g. lookup races mount order, or this
    session never mounted computer-use at all) - must degrade to a no-op,
    never raise mid-request."""
    coord = _FakeCoordinator({})
    provider = _AnthropicProviderNoStream()
    hook_mod._wrap_provider(provider, coord, max_inline=3)

    result = _run(provider.complete(_FakeRequest(model="claude-sonnet-4-5-20250929")))
    assert result == "ok"


@pytest.mark.parametrize("already_wrapped", [False, True])
def test_request_effective_model_primes_native_spec_before_request_is_built(
    already_wrapped,
):
    computer = _with_resolved_display(ComputerTool(_FakeBackend(), {}))
    provider = _AnthropicProviderNoStream("claude-opus-5")
    other = _AnthropicProviderNoStream("claude-opus-5")
    openai = _OpenAIProviderNoStream("opaque-model")
    coordinator = _FakeCoordinator(
        {"computer": computer}, {"openai": openai, "first": other, "selected": provider}
    )
    _run(hook_mod.mount(coordinator))
    handler = coordinator.hooks.handlers[hook_mod.PROVIDER_REQUEST]
    if already_wrapped:
        _run(handler(hook_mod.PROVIDER_REQUEST, {"provider": "selected"}))
        assert computer.native_tool_spec["type"] == "computer_20251124"
    for model, expected in (
        ("claude-sonnet-4-5-20250929", "computer_20250124"),
        ("claude-opus-5", "computer_20251124"),
    ):
        _run(
            handler(hook_mod.PROVIDER_REQUEST, {"provider": "selected", "model": model})
        )
        # This declaration is copied into ChatRequest before complete() is
        # called; correcting only the mutable tool during complete is too late.
        declaration = dict(computer.native_tool_spec)
        assert declaration["type"] == expected
        request = _FakeRequest(model, [_computer_tool()])
        assert _run(provider.complete(request)) == "ok"
        assert declaration == computer.native_tool_spec
    assert provider.default_model == other.default_model == "claude-opus-5"
    assert not getattr(other, hook_mod._WRAPPED_FLAG, False)
    assert not getattr(openai, hook_mod._WRAPPED_FLAG, False)


def test_request_falls_back_to_kernel_model_metadata_before_legacy_attribute():
    from types import SimpleNamespace

    computer = _with_resolved_display(ComputerTool(_FakeBackend(), {}))
    provider = _AnthropicProviderNoStream("claude-opus-5")
    provider.get_info = lambda: SimpleNamespace(
        defaults={"model": "claude-sonnet-4-5-20250929"}
    )
    coordinator = _FakeCoordinator({"computer": computer}, {"selected": provider})
    _mount_and_dispatch_provider_request(coordinator, "selected")
    assert computer.native_tool_spec["type"] == "computer_20250124"
