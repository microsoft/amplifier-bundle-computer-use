"""Unit tests for the fail-closed guard against silently-downgraded native tools.

hook-computer-use used to promote `computer`'s `native_tool_spec` to the wire itself
and inject the matching `anthropic-beta` header ("job 1"). That is now redundant and
has been removed: amplifier-module-loop-streaming PR #36 (commit f8004e0) preserves a
tool's `native_tool_spec` through its own `ToolSpec` construction, and each supported
provider carries the native form the rest of the way in its own idiom -
amplifier-module-provider-anthropic PR #79 (commit 94a4354) derives the required beta
header itself; amplifier-module-provider-openai PR #58 (commit 3af4ce1) recognises the
tool's bare `computer` type and emits it verbatim.

If the mounted provider or orchestrator predates its fix, `computer`'s native
definition silently degrades to a plain function tool - the request is still valid,
the tool still appears, and the model just gets the weaker definition, with no error
and no log line. These tests prove the fix: `_provider_derives_native_tool_betas`,
`_provider_recognizes_bare_computer_tool`, `_provider_supports_native_computer_tool`,
`_orchestrator_preserves_native_tool_spec`, and
`_fail_if_orchestrator_native_tool_spec_unsupported` detect that condition by driving
the real, installed code with throwaway probes - not by trusting a class name or
module path.

Honest, deliberate scope note (see `_provider_supports_native_computer_tool`'s
docstring): a pure capability probe cannot distinguish "this provider was never meant
to support computer-use at all" from "this IS a supported vendor, but the installed
build predates the exact fix being probed for" - both look identical from the
outside. The old `_is_anthropic()` module-name check COULD tell those apart by
trusting a claimed identity; removing it (the point of this change) means that
specific distinction is gone too. What remains loud, provably, is: a provider that
DOES demonstrate a working integration point but computes the wrong answer for it
(`_ProviderWithBrokenBetaDerivation`, `_ProviderWithBrokenBareComputerConversion`
below), and the orchestrator-side check (unaffected by any of this - the orchestrator
identity check was never in scope here).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import amplifier_module_hook_computer_use as hook_mod  # noqa: E402

# ---------------------------------------------------------------------------
# Provider-side fakes: Anthropic's dated-type convention
# (amplifier-module-provider-anthropic PR #79)
# ---------------------------------------------------------------------------


class _ProviderWithWorkingBetaDerivation:
    """Today's shape: has a working `_derive_native_tool_betas()`."""

    __module__ = "amplifier_module_provider_anthropic"

    def __init__(self) -> None:
        self.probed_tools: list[dict[str, str]] | None = None

    async def complete(self, request, **kwargs):
        return "ok"

    def _derive_native_tool_betas(self, tools):
        assert all(isinstance(tool, dict) for tool in tools)
        self.probed_tools = tools
        mapping = {"computer_20251124": "computer-use-2025-11-24"}
        return [mapping[tool["type"]] for tool in tools if tool.get("type") in mapping]


class _ProviderPredatingPR79:
    """Pre-PR-#79 shape: no `_derive_native_tool_betas()` at all, and no
    OpenAI-style `_convert_tools_from_request()` either - indistinguishable,
    to a pure capability probe, from a provider that never supported
    computer-use in the first place. See the module docstring."""

    __module__ = "amplifier_module_provider_anthropic"

    async def complete(self, request, **kwargs):
        return "ok"


class _ProviderWithBrokenBetaDerivation:
    """Has the method, but it does not actually derive anything - a broken or
    downgraded implementation, not merely an absent one. This IS distinguishable
    from "wrong vendor": the integration point exists and answers wrong."""

    __module__ = "amplifier_module_provider_anthropic"

    async def complete(self, request, **kwargs):
        return "ok"

    def _derive_native_tool_betas(self, tools):
        assert all(isinstance(tool, dict) for tool in tools)
        return []


# ---------------------------------------------------------------------------
# Provider-side fakes: OpenAI's bare-type convention
# (amplifier-module-provider-openai PR #58)
# ---------------------------------------------------------------------------


class _ProviderWithWorkingBareComputerConversion:
    """Today's shape: `_convert_tools_from_request` emits `computer` bare."""

    __module__ = "amplifier_module_provider_openai"
    tool_search_mode = "off"

    async def complete(self, request, **kwargs):
        return "ok"

    def _convert_tools_from_request(self, tools, model_name=None):
        out = []
        for tool in tools:
            if getattr(tool, "type", None) == "computer":
                out.append({"type": "computer"})
                continue
            out.append({"type": "function", "name": getattr(tool, "name", "?")})
        return out


class _ProviderPredatingPR58:
    """Pre-PR-#58 shape: no `_convert_tools_from_request()` at all - same
    "indistinguishable from wrong vendor" honesty note as `_ProviderPredatingPR79`."""

    __module__ = "amplifier_module_provider_openai"

    async def complete(self, request, **kwargs):
        return "ok"


class _ProviderWithBrokenBareComputerConversion:
    """Has `_convert_tools_from_request`, but it degrades `computer` into a
    function tool instead of emitting it bare - a real, observable bug."""

    __module__ = "amplifier_module_provider_openai"
    tool_search_mode = "off"

    async def complete(self, request, **kwargs):
        return "ok"

    def _convert_tools_from_request(self, tools, model_name=None):
        return [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in tools
        ]


class _LegacyBareComputerConverter:
    """A pre-seam provider shape that accepts only ToolSpec-style attributes."""

    def __init__(self, mode: object = "off") -> None:
        self.tool_search_mode = mode
        self.calls = 0
        self.probe = None

    def _convert_tools_from_request(self, tools, model_name=None):
        self.calls += 1
        self.probe = tools[0]
        if tools[0].type == "computer":
            return [{"type": "computer"}]
        return [{"type": "function", "name": tools[0].name}]


class _LegacyFunctionFallback(_LegacyBareComputerConverter):
    def _convert_tools_from_request(self, tools, model_name=None):
        self.calls += 1
        self.probe = tools[0]
        return [{"type": "function", "name": tools[0].name}]


class _InvalidNativeComputerSpec:
    tool_search_mode = "off"

    def __init__(self) -> None:
        self.calls = 0

    def get_native_computer_tool_spec(self):
        return {"type": "computer", "unexpected": True}

    def _convert_tools_from_request(self, tools, model_name=None):
        self.calls += 1
        return [{"type": "computer"}]


class _RaisingNativeComputerSpec(_InvalidNativeComputerSpec):
    def get_native_computer_tool_spec(self):
        raise RuntimeError("probe failure")


class _DictSubclassNativeComputerSpec(_InvalidNativeComputerSpec):
    def get_native_computer_tool_spec(self):
        class _BareComputerSpec(dict):
            pass

        return _BareComputerSpec(type="computer")


def test_provider_derives_native_tool_betas_returns_its_canonical_type():
    provider = _ProviderWithWorkingBetaDerivation()

    assert hook_mod._provider_derives_native_tool_betas(provider) == "computer_20251124"
    assert provider.probed_tools == [{"type": "computer_20251124", "name": "computer"}]


def test_real_anthropic_provider_derives_native_computer_beta():
    """Optional cross-repo check; standalone bundle CI does not import it."""
    provider_module = pytest.importorskip("amplifier_module_provider_anthropic")
    provider_class = getattr(provider_module, "AnthropicProvider", None)
    if provider_class is None:
        pytest.skip("requires amplifier-module-provider-anthropic")
    provider = provider_class(api_key="test", config={})

    assert hook_mod._provider_derives_native_tool_betas(provider) == "computer_20251124"


def test_provider_derives_native_tool_betas_returns_none_when_method_absent():
    """A pure capability probe cannot tell "predates the fix" apart from "wrong
    vendor entirely" - both simply lack the integration point. See module
    docstring for why that is an accepted, honest trade-off of removing the
    module-name check."""
    assert (
        hook_mod._provider_derives_native_tool_betas(_ProviderPredatingPR79()) is None
    )


def test_provider_derives_native_tool_betas_returns_none_when_broken():
    assert (
        hook_mod._provider_derives_native_tool_betas(
            _ProviderWithBrokenBetaDerivation()
        )
        is None
    )


def test_provider_recognizes_bare_computer_tool_returns_its_canonical_type():
    assert (
        hook_mod._provider_recognizes_bare_computer_tool(
            _ProviderWithWorkingBareComputerConversion()
        )
        == "computer"
    )


def test_provider_recognizes_bare_computer_tool_returns_none_when_method_absent():
    assert (
        hook_mod._provider_recognizes_bare_computer_tool(_ProviderPredatingPR58())
        is None
    )


def test_provider_recognizes_bare_computer_tool_returns_none_when_broken(caplog):
    with caplog.at_level("DEBUG", logger=hook_mod.__name__):
        result = hook_mod._provider_recognizes_bare_computer_tool(
            _ProviderWithBrokenBareComputerConversion()
        )

    assert result is None
    assert all(record.exc_info is None for record in caplog.records)
    assert "Traceback" not in caplog.text


def test_legacy_converter_probe_is_attribute_only_and_schema_complete():
    provider = _LegacyBareComputerConverter()

    assert hook_mod._provider_recognizes_bare_computer_tool(provider) == "computer"
    assert provider.calls == 1
    assert not isinstance(provider.probe, dict)
    assert provider.probe.name == "__computer_use_native_computer_probe__"
    assert provider.probe.description == "native computer-use compatibility probe"
    assert provider.probe.type == "computer"
    assert provider.probe.parameters == {"type": "object", "properties": {}}
    assert provider.probe.input_schema == {"type": "object", "properties": {}}


def test_legacy_converter_probe_schema_is_fresh_for_each_probe():
    first = hook_mod._NativeComputerToolProbe("computer")
    second = hook_mod._NativeComputerToolProbe("computer")

    first.parameters["properties"]["mutated"] = {"type": "string"}

    assert second.parameters == {"type": "object", "properties": {}}
    assert first.input_schema is first.parameters
    assert second.input_schema is second.parameters


def test_legacy_function_fallback_is_negative_without_traceback(caplog):
    provider = _LegacyFunctionFallback()

    with caplog.at_level("DEBUG", logger=hook_mod.__name__):
        result = hook_mod._provider_recognizes_bare_computer_tool(provider)

    assert result is None
    assert provider.calls == 1
    assert all(record.exc_info is None for record in caplog.records)
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("mode", [None, "namespaced", "unknown"])
def test_legacy_converter_is_not_called_without_explicit_off_mode(mode, caplog):
    provider = _LegacyBareComputerConverter(mode)
    if mode is None:
        del provider.tool_search_mode

    with caplog.at_level("DEBUG", logger=hook_mod.__name__):
        result = hook_mod._provider_recognizes_bare_computer_tool(provider)

    assert result is None
    assert provider.calls == 0
    assert all(record.exc_info is None for record in caplog.records)
    assert "Traceback" not in caplog.text


def test_invalid_or_raising_seam_uses_safe_legacy_converter_only(caplog):
    invalid = _InvalidNativeComputerSpec()
    raising = _RaisingNativeComputerSpec()

    with caplog.at_level("DEBUG", logger=hook_mod.__name__):
        assert hook_mod._provider_recognizes_bare_computer_tool(invalid) == "computer"
        assert hook_mod._provider_recognizes_bare_computer_tool(raising) == "computer"

    assert invalid.calls == raising.calls == 1
    assert all(record.exc_info is None for record in caplog.records)
    assert "Traceback" not in caplog.text


def test_helper_accepts_only_an_exact_bare_dict_before_safe_legacy_fallback(caplog):
    provider = _DictSubclassNativeComputerSpec()

    with caplog.at_level("DEBUG", logger=hook_mod.__name__):
        assert hook_mod._provider_recognizes_bare_computer_tool(provider) == "computer"

    assert provider.calls == 1
    assert all(record.exc_info is None for record in caplog.records)
    assert "Traceback" not in caplog.text


def test_native_dialect_probe_is_cached_per_provider_instance():
    provider = _LegacyBareComputerConverter()

    assert hook_mod._provider_supports_native_computer_tool(provider) == "computer"
    assert hook_mod._provider_supports_native_computer_tool(provider) == "computer"
    assert provider.calls == 1


def test_real_namespaced_openai_provider_uses_pure_seam_without_state_mutation():
    """Optional cross-repo check; standalone bundle CI does not import it."""
    provider_module = pytest.importorskip("amplifier_module_provider_openai")
    provider_class = getattr(provider_module, "OpenAIProvider", None)
    if provider_class is None:
        pytest.skip("requires amplifier-module-provider-openai")
    coordinator = MagicMock()
    coordinator.get_capability.return_value = None
    provider = provider_class(
        api_key="test-key",
        config={"tool_search": {"mode": "namespaced"}},
        coordinator=coordinator,
    )
    state_before = (
        provider._tool_search_roster,
        dict(provider._tool_search_extra),
        provider._pending_additional_tools_item,
        provider._apply_patch_native,
    )
    provider._convert_tools_from_request = MagicMock(
        side_effect=AssertionError("pure seam must not call converter")
    )
    provider.get_native_computer_tool_spec = MagicMock(
        wraps=provider.get_native_computer_tool_spec
    )

    assert hook_mod._provider_supports_native_computer_tool(provider) == "computer"
    assert hook_mod._provider_supports_native_computer_tool(provider) == "computer"
    assert (
        provider._tool_search_roster,
        provider._tool_search_extra,
        provider._pending_additional_tools_item,
        provider._apply_patch_native,
    ) == state_before
    provider._convert_tools_from_request.assert_not_called()
    provider.get_native_computer_tool_spec.assert_called_once_with()


@pytest.mark.parametrize(
    "native_tool_spec",
    [
        {"type": "computer"},
        {
            "type": "computer_20251124",
            "name": "computer",
            "display_width_px": 1280,
            "display_height_px": 720,
        },
    ],
)
def test_real_loop_build_tool_spec_preserves_both_computer_dialects(
    native_tool_spec: dict[str, object],
):
    """Exercise loop-streaming's actual builder, not this file's stub."""
    loop_module = pytest.importorskip("amplifier_module_loop_streaming")
    build_tool_spec = getattr(loop_module, "_build_tool_spec", None)
    if not callable(build_tool_spec):
        pytest.skip("requires amplifier-module-loop-streaming")

    class _NativeComputerTool:
        name = "computer"
        description = "native computer tool"
        input_schema = {"type": "object", "properties": {}}

        @property
        def native_tool_spec(self) -> dict[str, object]:
            return native_tool_spec

    emitted = build_tool_spec(_NativeComputerTool()).model_dump()

    for key, value in native_tool_spec.items():
        assert emitted[key] == value


# ---------------------------------------------------------------------------
# _provider_supports_native_computer_tool - the _is_anthropic() replacement
# ---------------------------------------------------------------------------


def test_provider_supports_native_computer_tool_selects_anthropic_type():
    assert (
        hook_mod._provider_supports_native_computer_tool(
            _ProviderWithWorkingBetaDerivation()
        )
        == "computer_20251124"
    )


def test_provider_supports_native_computer_tool_selects_openai_type():
    assert (
        hook_mod._provider_supports_native_computer_tool(
            _ProviderWithWorkingBareComputerConversion()
        )
        == "computer"
    )


def test_provider_supports_native_computer_tool_returns_none_for_neither_shape():
    class _TotallyUnrelatedProvider:
        __module__ = "some_other_vendor.provider"

        async def complete(self, request, **kwargs):
            return "ok"

    assert (
        hook_mod._provider_supports_native_computer_tool(_TotallyUnrelatedProvider())
        is None
    )


# ---------------------------------------------------------------------------
# Orchestrator-side fakes (amplifier-module-loop-streaming PR #36) - unchanged,
# `_is_loop_streaming` staying a module-name check is deliberate (out of scope
# here - see `_is_loop_streaming`'s docstring).
# ---------------------------------------------------------------------------


class _ToolSpecLike:
    """Minimal stand-in for `amplifier_core.message_models.ToolSpec` - just
    enough for `.model_dump(exclude_none=True)` to report back whatever fields
    a real `ToolSpec` (which is `extra="allow"`) would carry through."""

    def __init__(self, **fields: object) -> None:
        self._fields = fields

    def model_dump(self, exclude_none: bool = True) -> dict[str, object]:
        if exclude_none:
            return {k: v for k, v in self._fields.items() if v is not None}
        return dict(self._fields)


def _new_build_tool_spec(tool):
    """Mimics the FIXED `_build_tool_spec` (PR #36, commit f8004e0): preserves
    `native_tool_spec` fields as `ToolSpec` extras."""
    native = getattr(tool, "native_tool_spec", None)
    if isinstance(native, dict) and native.get("type"):
        return _ToolSpecLike(**native)
    return _ToolSpecLike(name=tool.name)


def _old_build_tool_spec(tool):
    """Mimics the PRE-PR-#36 behaviour: only name/description/parameters,
    silently dropping `native_tool_spec` entirely."""
    return _ToolSpecLike(name=tool.name)


def _register_fake_orchestrator_module(module_name: str, build_tool_spec=None):
    """Register a throwaway module in `sys.modules` under `module_name`, standing
    in for a real loop-streaming install, so `_orchestrator_preserves_native_tool_spec`
    (which looks the function up via `sys.modules[type(orchestrator).__module__]`)
    finds exactly the `_build_tool_spec` shape a given test wants to simulate."""
    module = types.ModuleType(module_name)
    if build_tool_spec is not None:
        module._build_tool_spec = build_tool_spec  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    return module


def test_orchestrator_preserves_native_tool_spec_true_for_fixed_loop_streaming():
    _register_fake_orchestrator_module(
        "_fake_loop_streaming_fixed", build_tool_spec=_new_build_tool_spec
    )

    class _FixedOrchestrator:
        __module__ = "_fake_loop_streaming_fixed"

    assert (
        hook_mod._orchestrator_preserves_native_tool_spec(_FixedOrchestrator()) is True
    )


def test_orchestrator_preserves_native_tool_spec_false_for_old_loop_streaming():
    _register_fake_orchestrator_module(
        "_fake_loop_streaming_old", build_tool_spec=_old_build_tool_spec
    )

    class _OldOrchestrator:
        __module__ = "_fake_loop_streaming_old"

    assert (
        hook_mod._orchestrator_preserves_native_tool_spec(_OldOrchestrator()) is False
    )


def test_orchestrator_preserves_native_tool_spec_false_when_build_tool_spec_missing():
    """Identified as loop-streaming (module name matches) but doesn't even have
    `_build_tool_spec` - an even older shape than PR #36 anticipated. Still a
    definite "no", not an unknown."""
    _register_fake_orchestrator_module("_fake_loop_streaming_ancient")

    class _AncientOrchestrator:
        __module__ = "_fake_loop_streaming_ancient"

    assert (
        hook_mod._orchestrator_preserves_native_tool_spec(_AncientOrchestrator())
        is False
    )


def test_orchestrator_preserves_native_tool_spec_none_for_unrelated_orchestrator():
    """A totally different, unrelated orchestrator is simply not this check's
    concern - it must not be treated as either confirmed-compatible or
    confirmed-incompatible."""

    class _SomeOtherOrchestrator:
        __module__ = "amplifier_module_loop_basic"

    assert (
        hook_mod._orchestrator_preserves_native_tool_spec(_SomeOtherOrchestrator())
        is None
    )


# ---------------------------------------------------------------------------
# End-to-end: _fail_if_orchestrator_native_tool_spec_unsupported / _wrap_provider
# ---------------------------------------------------------------------------


class _FakeComputerTool:
    """Minimal stand-in exposing only what `_resolve_native_tool_type` reads:
    a `native_tool_spec` dict carrying the `type` this session's `computer`
    tool is actually configured to declare (Anthropic-versioned, or OpenAI's
    bare `"computer"`)."""

    def __init__(self, tool_type: str) -> None:
        self._tool_type = tool_type

    @property
    def native_tool_spec(self) -> dict[str, object]:
        return {"type": self._tool_type, "name": "computer"}


class _FakeCoordinatorWithOrchestrator:
    def __init__(self, orchestrator, tool_type: str | None = None) -> None:
        self._orchestrator = orchestrator
        self._computer_tool = _FakeComputerTool(tool_type) if tool_type else None

    def get(self, mount_point, name=None):
        if mount_point == "orchestrator":
            return self._orchestrator
        if mount_point == "tools" and name == "computer":
            return self._computer_tool
        return None


def test_fail_if_orchestrator_native_tool_spec_unsupported_raises_for_old_orchestrator():
    _register_fake_orchestrator_module(
        "_fake_loop_streaming_old_e2e", build_tool_spec=_old_build_tool_spec
    )

    class _OldOrchestrator:
        __module__ = "_fake_loop_streaming_old_e2e"

    coord = _FakeCoordinatorWithOrchestrator(orchestrator=_OldOrchestrator())
    with pytest.raises(
        hook_mod.ComputerUseNativeToolPassthroughUnsupportedError
    ) as excinfo:
        hook_mod._fail_if_orchestrator_native_tool_spec_unsupported(coord)
    message = str(excinfo.value)
    assert "f8004e0" in message
    assert "loop-streaming" in message.lower()


def test_fail_if_orchestrator_native_tool_spec_unsupported_is_a_noop_when_compatible():
    _register_fake_orchestrator_module(
        "_fake_loop_streaming_fixed_e2e", build_tool_spec=_new_build_tool_spec
    )

    class _FixedOrchestrator:
        __module__ = "_fake_loop_streaming_fixed_e2e"

    coord = _FakeCoordinatorWithOrchestrator(orchestrator=_FixedOrchestrator())
    # Must not raise.
    hook_mod._fail_if_orchestrator_native_tool_spec_unsupported(coord)


def test_fail_if_orchestrator_native_tool_spec_unsupported_is_a_noop_with_no_orchestrator():
    """No orchestrator mounted (e.g. still starting up) must not be confused with
    an incompatible one - nothing to probe means nothing to fail on."""
    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None)
    # Must not raise.
    hook_mod._fail_if_orchestrator_native_tool_spec_unsupported(coord)


def test_wrap_provider_skips_quietly_for_provider_predating_pr79():
    """Full integration through `_wrap_provider`, the actual mount-time seam.

    Behavior change from the old `_is_anthropic()`-gated design, deliberate and
    documented (see module docstring and `_provider_supports_native_computer_tool`):
    a provider indistinguishable from "wrong vendor" is skipped quietly, logged,
    not raised. Loud failure is reserved for a provider that demonstrates a real,
    working integration point but computes the wrong answer, or an orchestrator
    that positively fails its own probe (see the other tests in this file)."""
    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None)
    provider = _ProviderPredatingPR79()

    wrapped = hook_mod._wrap_provider(provider, coord, max_inline=3)

    assert wrapped is False
    assert not getattr(provider, hook_mod._WRAPPED_FLAG, False)


def test_wrap_provider_skips_quietly_for_provider_predating_pr58():
    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None)
    provider = _ProviderPredatingPR58()

    wrapped = hook_mod._wrap_provider(provider, coord, max_inline=3)

    assert wrapped is False
    assert not getattr(provider, hook_mod._WRAPPED_FLAG, False)


def test_capability_probe_rejection_is_operator_actionable(caplog):
    """The capability-probe rejection (`_provider_supports_native_computer_tool`
    returning False in `_wrap_provider`) is a real, silent capability gap for
    this session - 'computer' just quietly runs as a plain function tool - and
    it used to be logged at INFO, a level routinely filtered out of default
    log verbosity. This is fail-loud-to-the-system without being fail-loud-to-
    the-human: refusing to promote the tool is the right call, but nothing
    told an operator it happened, why, or what to do about it.

    Proves three things about the log line this path now produces: it is
    loud enough to see by default (WARNING, not INFO), it is honest about the
    ambiguity `_provider_supports_native_computer_tool`'s own docstring
    describes (a negative result cannot distinguish "wrong vendor" from "right
    vendor, old build"), and it tells a human what to do about each case.
    """
    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None)
    provider = _ProviderPredatingPR79()

    with caplog.at_level("WARNING", logger=hook_mod.__name__):
        wrapped = hook_mod._wrap_provider(provider, coord, max_inline=3)

    assert wrapped is False
    records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert records, "capability-probe rejection must be visible at WARNING, not INFO"
    message = records[0].getMessage()
    assert "native computer use is disabled" in message.lower()
    assert "_derive_native_tool_betas" in message
    assert "get_native_computer_tool_spec" in message
    assert "_convert_tools_from_request" in message
    assert "tool_search_mode='off'" in message


def test_unsupported_warning_is_once_per_provider_instance(caplog):
    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None)
    first = _ProviderPredatingPR79()
    second = _ProviderPredatingPR79()
    original = first.complete
    with caplog.at_level("WARNING", logger=hook_mod.__name__):
        for provider in (first, first, second, first, second):
            assert hook_mod._wrap_provider(provider, coord, max_inline=3) is False

    warnings = [
        r for r in caplog.records if "did not prove native computer" in r.getMessage()
    ]
    assert len(warnings) == 2
    assert first.complete == original
    assert not getattr(first, hook_mod._WRAPPED_FLAG, False)


def test_unsupported_warning_does_not_break_immutable_provider(caplog):
    class FrozenProvider:
        __slots__ = ()

        async def complete(self, request, **kwargs):
            return "ok"

    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None)
    provider = FrozenProvider()
    # If a provider cannot retain the marker, repeat rather than hide the gap.
    assert hook_mod._wrap_provider(provider, coord, max_inline=3) is False
    assert hook_mod._wrap_provider(provider, coord, max_inline=3) is False
    assert "did not prove native computer" in caplog.text


def test_wrap_provider_wraps_openai_shaped_provider():
    """Regression guard for the whole point of this change: an OpenAI-shaped
    provider with working bare-computer-tool passthrough gets wrapped, exactly
    like an Anthropic-shaped one already does elsewhere in this suite.

    The provider probe chooses its own bare canonical type, independent of
    any mounted tool state."""
    coord = _FakeCoordinatorWithOrchestrator(orchestrator=None)
    provider = _ProviderWithWorkingBareComputerConversion()

    wrapped = hook_mod._wrap_provider(provider, coord, max_inline=3)

    assert wrapped is True
    assert getattr(provider, hook_mod._WRAPPED_FLAG, False) is True
