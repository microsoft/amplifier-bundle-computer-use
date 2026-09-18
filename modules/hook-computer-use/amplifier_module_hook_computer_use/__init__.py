"""Amplifier hook module: make the NATIVE `computer` tool work end to end,
across whichever provider (Anthropic, OpenAI, ...) is actually mounted.

One thing used to stand between a mounted `computer` tool and real computer use,
and it lived in the orchestrator, which we did not want to fork: tool results are
collapsed to ``str`` before they reach the provider, so a screenshot can never
travel back as an image content block.

That is fixed at a single seam: the provider's ``complete()`` call. This hook wraps
it and, on the way through:

* expands screenshot markers in tool results into real base64 image blocks,
* keeps only the most recent screenshots inline, so long sessions stay affordable.

Nothing is forked, nothing is patched on disk, and removing the hook degrades the
tool cleanly back to an ordinary function tool.

Tool-spec promotion (rewriting ``computer`` into a provider's native wire form and
injecting whatever header/shape that provider requires) used to live here too, but
is now handled upstream: `amplifier-module-loop-streaming` preserves a tool's
``native_tool_spec`` through its own `ToolSpec` construction, and each supported
provider carries the native form the rest of the way in its own idiom -
`amplifier-module-provider-anthropic` derives the required `anthropic-beta` header
from the native tool types present on the request; `amplifier-module-provider-openai`
recognises the tool's bare `computer` type and emits it verbatim. This hook now only
*verifies* that support is present for whichever provider is actually in play
(`_provider_supports_native_computer_tool`,
`_fail_if_orchestrator_native_tool_spec_unsupported`) rather than doing the work
itself - see those functions' docstrings for why a silent degradation is not
acceptable here, and why this gate no longer asks a provider's name before asking
what it can actually do.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from amplifier_core import HookResult

try:  # event name is a plain constant, but tolerate kernels that move it
    from amplifier_core.events import PROVIDER_REQUEST  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    PROVIDER_REQUEST = "provider:request"

try:
    from amplifier_core.events import TOOL_PRE  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    TOOL_PRE = "tool:pre"

try:
    from amplifier_core.events import TOOL_POST  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    TOOL_POST = "tool:post"

logger = logging.getLogger(__name__)

__version__ = "0.1.0"

MARKER = "__amplifier_computer_use__"
_WRAPPED_FLAG = "_amplifier_computer_use_wrapped"
_UNSUPPORTED_WARNING_FLAG = "_amplifier_computer_use_unsupported_warned"
_DEFAULT_MAX_INLINE_IMAGES = 3

#: Set AMPLIFIER_COMPUTER_USE_TRACE=<path> to record what this hook did and when.
#: Invaluable when "the model says it cannot see the screen" and you need to know
#: whether the hook mounted, fired, wrapped, and rewrote - without reading events.jsonl.
_TRACE_PATH = os.environ.get("AMPLIFIER_COMPUTER_USE_TRACE")


def _trace(msg: str) -> None:
    if not _TRACE_PATH:
        return
    try:
        with open(_TRACE_PATH, "a", encoding="utf-8") as fh:
            fh.write(
                f"{datetime.now(tz=timezone.utc).isoformat(timespec='seconds')} [{os.getpid()}] {msg}\n"
            )
    except OSError:
        pass


class ComputerUseHookIncompatibleProviderError(RuntimeError):
    """Raised when the provider we are about to wrap would make this hook a silent no-op.

    This hook only patches `Provider.complete()`. The bundled orchestrator
    (loop-streaming, `amplifier_module_loop_streaming/__init__.py:827`) branches on
    `hasattr(provider, "stream")`: whenever a provider exposes `stream()`, the
    orchestrator calls THAT method instead of `complete()` - our wrap never runs.
    Today this "works" only because `provider-anthropic` happens not to define
    `stream()`. There is no exception, no log line, and no event the day that changes:
    the hook still reports "wrapped provider ... for native computer use", mount()
    still succeeds, and computer-use silently goes blind while the session otherwise
    looks completely healthy.

    A computer-use bundle that silently stops driving the computer is worse than one
    that refuses to load. So: refuse. Loudly, unmissably, the first time a provider
    with `stream()` is seen - not a warning that scrolls past in a log stream.
    """


def _fail_if_stream_incompatible(provider: Any) -> None:
    if hasattr(provider, "stream"):
        raise ComputerUseHookIncompatibleProviderError(
            f"computer-use: provider {type(provider).__name__} "
            f"({type(provider).__module__}) now exposes stream() - hook-computer-use "
            "only wraps complete(), and the orchestrator prefers stream() whenever it "
            "is present. Wrapping this provider would silently do nothing: no "
            "screenshot inlining, no error, no log line. Refusing to operate rather "
            "than degrade invisibly. Fix: either wrap complete() AND stream() in this "
            "hook, or route computer-use through an orchestrator that does not prefer "
            "stream()."
        )


class ComputerUseNativeToolPassthroughUnsupportedError(RuntimeError):
    """Raised when the mounted orchestrator/provider cannot carry `computer`'s
    native tool form to the wire without this hook doing it for them.

    hook-computer-use used to promote `computer`'s `native_tool_spec` to the wire
    itself and inject the matching `anthropic-beta` header (see CHANGELOG /
    module docstring - "job 1"). That is now redundant and has been removed:

    * `amplifier-module-loop-streaming` (commit f8004e0, PR #36 - "feat: preserve
      model-native tool form through ToolSpec construction") makes its
      `_build_tool_spec()` preserve a tool's `native_tool_spec` through `ToolSpec`
      construction - `ToolSpec` is `extra="allow"`, so the native keys ride along
      as extras and reach the provider intact.
    * `amplifier-module-provider-anthropic` (commit 94a4354, PR #79 - "fix:
      cache_control targets last function tool; derive betas from native tool
      types") makes the provider derive the required `anthropic-beta` header
      itself from the native tool types present on the request, and stop
      stamping `cache_control` onto them.

    If EITHER upstream module predates its fix, `computer`'s native definition
    silently degrades to a plain function tool: the request is still valid, the
    tool still appears, and the model just gets the weaker definition - measurably
    worse targeting, with no error and no log line. That is the exact
    silent-downgrade failure mode this bundle exists to prevent (see
    `ComputerUseHookIncompatibleProviderError` for the sibling guard against the
    same class of failure on the streaming path). So: refuse to mount rather than
    let it happen invisibly.

    Detection deliberately does not trust a version string - manifests can lie,
    and a shallow git clone may not even have one. Instead it drives the actual
    installed code with a throwaway probe tool/tool-list and checks the real
    output, exactly the way `_fail_if_stream_incompatible` checks for a real
    `stream` attribute rather than a claimed version.
    """


_ANTHROPIC_PROBE_TOOL_TYPE = "computer_20251124"
_OPENAI_PROBE_TOOL_TYPE = "computer"
_NATIVE_COMPUTER_DIALECT_CACHE_ATTR = "_amplifier_computer_use_native_computer_dialect"
_NATIVE_COMPUTER_DIALECT_UNRESOLVED = object()


class _NativeComputerToolProbe:
    """Attribute-only, schema-complete stand-in for a native ToolSpec."""

    name = "__computer_use_native_computer_probe__"
    description = "native computer-use compatibility probe"

    def __init__(self, tool_type: str) -> None:
        self.type = tool_type
        self.parameters: dict[str, Any] = {"type": "object", "properties": {}}
        self.input_schema = self.parameters


def _provider_derives_native_tool_betas(provider: Any) -> str | None:
    """Real capability probe for Anthropic's wire convention: does `provider`
    self-derive the `anthropic-beta` header required to opt `tool_type` into
    native tool_use (amplifier-module-provider-anthropic PR #79)?

    Drives the provider's own `_derive_native_tool_betas()` (if present) with
    a throwaway native tool dict and checks that the returned beta header
    actually mentions computer-use - never trusts the provider's
    class name or module path to answer this. A provider with no such method,
    or one that does not recognise that type, returns `None` here exactly
    like a provider that was never Anthropic-shaped at all: this probe has no
    way to tell those two apart, and does not claim to (see
    `_provider_supports_native_computer_tool`'s docstring for why that is an
    acceptable, honest trade-off).
    """
    derive = getattr(provider, "_derive_native_tool_betas", None)
    if not callable(derive):
        logger.debug(
            "computer-use: provider %s has no _derive_native_tool_betas integration",
            type(provider).__name__,
        )
        return None
    try:
        betas = derive([{"type": _ANTHROPIC_PROBE_TOOL_TYPE, "name": "computer"}])
    except Exception:
        logger.debug(
            "computer-use: _derive_native_tool_betas probe raised on %s",
            type(provider).__name__,
        )
        return None
    if isinstance(betas, list) and any("computer-use" in str(beta) for beta in betas):
        return _ANTHROPIC_PROBE_TOOL_TYPE
    logger.debug(
        "computer-use: provider %s did not derive a computer-use beta",
        type(provider).__name__,
    )
    return None


def _provider_recognizes_bare_computer_tool(provider: Any) -> str | None:
    """Real capability probe for OpenAI's wire convention: does `provider`
    place a native `computer` tool declaration on the wire completely bare -
    no `name`/`description`/`parameters` - rather than falling through to its
    ordinary function-tool branch (amplifier-module-provider-openai PR #58)?

    Live Responses API traffic proved OpenAI's `computer` tool accepts *zero*
    declaration fields beyond `type`: `{"type": "computer"}` -> 200;
    `display_width_px`/`display_height_px`/`display_width`/`environment`
    (any of them, alone) -> 400 "Unknown parameter". A degraded declaration
    here is not a weaker-but-working tool the way it can be with Anthropic -
    it is a hard, immediate request failure, which makes this probe's job
    slightly different in kind from `_provider_derives_native_tool_betas`:
    It first asks the optional, argument-free
    `get_native_computer_tool_spec()` serialization seam. Only an exact
    `{"type": "computer"}` answer is accepted. Legacy providers without a
    usable seam may be probed through `_convert_tools_from_request()`, but only
    while they explicitly report `tool_search_mode == "off"`: newer
    namespaced conversion records roster and pending-tool state, so probing it
    is not observationally safe.
    """
    try:
        get_spec = getattr(provider, "get_native_computer_tool_spec", None)
    except Exception:
        logger.debug(
            "computer-use: get_native_computer_tool_spec was unreadable on %s",
            type(provider).__name__,
        )
        get_spec = None
    if callable(get_spec):
        try:
            spec = get_spec()
            if type(spec) is dict and spec == {"type": _OPENAI_PROBE_TOOL_TYPE}:
                return _OPENAI_PROBE_TOOL_TYPE
        except Exception:
            logger.debug(
                "computer-use: get_native_computer_tool_spec probe raised on %s",
                type(provider).__name__,
            )
        else:
            logger.debug(
                "computer-use: provider %s did not return the exact bare computer spec",
                type(provider).__name__,
            )

    try:
        legacy_converter_is_safe = getattr(provider, "tool_search_mode", None) == "off"
    except Exception:
        logger.debug(
            "computer-use: provider %s did not expose a readable tool_search_mode",
            type(provider).__name__,
        )
        return None
    if not legacy_converter_is_safe:
        logger.debug(
            "computer-use: not probing legacy bare-computer conversion on %s "
            "without tool_search_mode='off'",
            type(provider).__name__,
        )
        return None

    convert = getattr(provider, "_convert_tools_from_request", None)
    if not callable(convert):
        logger.debug(
            "computer-use: provider %s has no _convert_tools_from_request integration",
            type(provider).__name__,
        )
        return None

    try:
        converted = convert([_NativeComputerToolProbe(_OPENAI_PROBE_TOOL_TYPE)])
    except Exception:
        logger.debug(
            "computer-use: bare-computer-tool probe raised on %s",
            type(provider).__name__,
        )
        return None
    if converted == [{"type": _OPENAI_PROBE_TOOL_TYPE}]:
        return _OPENAI_PROBE_TOOL_TYPE
    logger.debug(
        "computer-use: provider %s did not preserve a bare computer tool",
        type(provider).__name__,
    )
    return None


#: Every known way an Amplifier provider module can prove it will carry a
#: native `computer` tool type to the wire, newest-vendor-last. A table, not a
#: chain of `or`s, for one reason: when the answer is False the caller can name
#: every integration point it actually tried (see `_wrap_provider`'s log line).
#: A silent "unsupported" that does not say what it looked for is how a
#: downgrade hides.
#:
#: NOTE these are NOT vendor wire formats - those live in one place,
#: `tool-computer-use`'s `providers.py`. These are the *plumbing* names of
#: amplifier provider modules (`_derive_native_tool_betas`,
#: `_convert_tools_from_request`), which is a different axis that merely
#: correlates 1:1 with vendors while there are exactly two. This module cannot
#: import that table anyway: `hook-computer-use` declares `dependencies = []`
#: and is separately installable, by design.
_NATIVE_WIRE_PROBES: tuple[tuple[str, Any], ...] = (
    (
        "_derive_native_tool_betas (dated computer_YYYYMMDD types)",
        _provider_derives_native_tool_betas,
    ),
    (
        "get_native_computer_tool_spec (bare `computer`; legacy "
        "_convert_tools_from_request only when tool_search_mode='off')",
        _provider_recognizes_bare_computer_tool,
    ),
)


def _native_wire_probe_names() -> str:
    return "; ".join(label for label, _ in _NATIVE_WIRE_PROBES)


def _provider_supports_native_computer_tool(provider: Any) -> str | None:
    """Replaces the old `_is_anthropic()` module-name sniff as the gate for
    whether `_wrap_provider` even attempts to wrap `provider`.

    A module-name match answers "what is this object called"; it says
    nothing about what the object actually *does* - and the moment a second
    vendor (OpenAI) shipped its OWN, differently-shaped native `computer`
    tool support, "not named anthropic" stopped meaning "not compatible".
    This checks the only thing that actually matters: which native `computer`
    type will `provider` place on the wire? Two real, independent behavioural
    probes each supply their own canonical dialect seed - see their docstrings:

      * `_provider_derives_native_tool_betas` - Anthropic's dated
        `computer_YYYYMMDD` convention.
      * `_provider_recognizes_bare_computer_tool` - OpenAI's bare `computer`
        convention.

    Honest limitation, stated plainly rather than papered over: neither probe
    can distinguish "this provider was never meant to support computer-use at
    all" from "this IS a supported vendor, but the installed build predates
    the exact fix being probed for" - both look identical from the outside
    (the integration point this probe drives simply does not exist yet). A
    module-name check could have told those apart by trusting a claimed
    identity; a real behavioural check, by construction, cannot - it only
    reports what the code in front of it actually does. `None` here means
    "wrap nothing, log why, move on" (see `_wrap_provider`), not a raised
    error - the loud failure this bundle still guarantees is reserved for a
    provider that DOES demonstrate a working integration point but computes
    the wrong answer for it (a real, observable bug, not a guess about
    identity) and for the orchestrator-side check in
    `_fail_if_orchestrator_native_tool_spec_unsupported`, which does not have
    this ambiguity (see that function's docstring).
    """
    try:
        cached = getattr(
            provider,
            _NATIVE_COMPUTER_DIALECT_CACHE_ATTR,
            _NATIVE_COMPUTER_DIALECT_UNRESOLVED,
        )
    except Exception:
        cached = _NATIVE_COMPUTER_DIALECT_UNRESOLVED
    if cached is None or isinstance(cached, str):
        return cached

    native_tool_type = None
    for label, probe in _NATIVE_WIRE_PROBES:
        if native_tool_type := probe(provider):
            logger.debug(
                "computer-use: provider %s carries native tool type %r "
                "(confirmed by %s)",
                type(provider).__name__,
                native_tool_type,
                label,
            )
            break
    try:
        setattr(provider, _NATIVE_COMPUTER_DIALECT_CACHE_ATTR, native_tool_type)
    except Exception:
        logger.debug(
            "computer-use: could not cache native computer dialect on %s",
            type(provider).__name__,
        )
    return native_tool_type


def _is_loop_streaming(orchestrator: Any) -> bool:
    """Module-name heuristic applied to the ORCHESTRATOR only - out of scope
    for this pass (see module docstring): loop-streaming is the only
    orchestrator this bundle has ever run against, so there is no second
    implementation motivating a capability check here the way there now is
    for providers. Needed because the mounted orchestrator could be
    anything, including one this hook has no opinion about at all."""
    identity = f"{type(orchestrator).__module__}.{type(orchestrator).__name__}"
    return "loop_streaming" in identity.lower().replace("-", "_")


def _orchestrator_preserves_native_tool_spec(orchestrator: Any) -> bool | None:
    """Probe whether the mounted orchestrator's tool-spec construction preserves
    a tool's `native_tool_spec` (amplifier-module-loop-streaming PR #36).

    Exercises the orchestrator module's own `_build_tool_spec()` against a
    throwaway stub tool exposing `native_tool_spec`, and checks whether the
    native `type` actually survives into the emitted `ToolSpec`. This is a real
    behavioural probe of the installed code, not a version string.

    Returns:
        True  - identified as loop-streaming, and it preserves the native type.
        False - identified as loop-streaming, but it does not: either
                `_build_tool_spec()` is missing entirely, or it drops the
                native type (the pre-PR-#36 behaviour).
        None  - NOT identified as loop-streaming at all (see
                `_is_loop_streaming`) - some other orchestrator is mounted,
                which is simply not this check's concern. Callers must not
                treat this as "confirmed compatible".
    """
    if not _is_loop_streaming(orchestrator):
        return None
    module = sys.modules.get(type(orchestrator).__module__)
    build: Any = getattr(module, "_build_tool_spec", None) if module else None
    if not callable(build):
        return False

    class _NativeToolSpecProbe:
        name = "__computer_use_native_tool_spec_probe__"
        description = "probe"
        input_schema: dict[str, Any] = {}

        @property
        def native_tool_spec(self) -> dict[str, Any]:
            return {
                "type": "computer_20251124",
                "name": "__computer_use_native_tool_spec_probe__",
            }

    try:
        spec: Any = build(_NativeToolSpecProbe())
        dumped = spec.model_dump(exclude_none=True)
    except Exception:
        logger.exception(
            "computer-use: native_tool_spec passthrough probe raised on orchestrator %s",
            type(orchestrator).__name__,
        )
        return False
    return dumped.get("type") == "computer_20251124"


def _fail_if_orchestrator_native_tool_spec_unsupported(coordinator: Any) -> None:
    """Refuse to mount if the mounted orchestrator's `ToolSpec` construction
    would drop `computer`'s native tool form on the floor. See
    `ComputerUseNativeToolPassthroughUnsupportedError` for the full rationale.

    Provider-side compatibility is no longer checked here: by the time
    `_wrap_provider` calls this, `_provider_supports_native_computer_tool`
    has already gated on it (see that function's docstring for why an
    incompatible provider is a quiet skip there, not a raise here) - this
    function is purely about the orchestrator.

    The orchestrator probe returns `None` when it cannot be run at all (some
    orchestrator other than loop-streaming is mounted) - that is "not this
    check's concern", not "confirmed compatible", and is intentionally NOT
    treated as a failure: we only refuse to mount when the probe positively
    identified loop-streaming AND it came back negative. Unlike the provider
    side, `_is_loop_streaming` staying a module-name check is deliberate and
    unchanged (see its own docstring) - there is exactly one orchestrator
    this bundle has ever run against, so there is no second implementation
    motivating a capability check the way a second provider now does.
    """
    orchestrator = None
    try:
        orchestrator = coordinator.get("orchestrator")
    except Exception:
        logger.debug("computer-use: orchestrator lookup failed", exc_info=True)
    if orchestrator is not None and (
        _orchestrator_preserves_native_tool_spec(orchestrator) is False
    ):
        raise ComputerUseNativeToolPassthroughUnsupportedError(
            f"computer-use: orchestrator {type(orchestrator).__name__} "
            f"({type(orchestrator).__module__}) does not preserve a tool's "
            "native_tool_spec through its ToolSpec construction. "
            "hook-computer-use no longer promotes tool specs itself - upgrade "
            "amplifier-module-loop-streaming to at least commit f8004e0 (PR #36, "
            "'feat: preserve model-native tool form through ToolSpec "
            "construction'), or the `computer` tool's native definition will "
            "silently degrade to a plain function tool."
        )


#: Reused decoder for `_loads_leading` - `json.JSONDecoder` is stateless/reentrant
#: across `raw_decode` calls, so one module-level instance is safe to share.
_JSON_DECODER = json.JSONDecoder()


def _loads_leading(text: str) -> Any:
    """Parse the JSON value at the *start* of `text`, ignoring anything after it.

    Tool-result content is not necessarily JSON-and-only-JSON by the time this hook
    sees it: the kernel's `HookResult.append_to_last_tool_result` mechanism (see
    HOOKS_API.md - "Injection placement control") lets OTHER hooks glue their own
    text onto the tail of the *same* last-tool-result content string ours occupies.
    Session-start reminders (`hooks-status-context`, `hooks-todo-reminder`,
    `mode-status`, `hooks-skills-visibility`, ...) do exactly this on the very
    first tool call of a session - the common case a screenshot is taken in.

    That mechanism is legitimate, general kernel policy we neither own nor control
    the timing of (per KERNEL_PHILOSOPHY.md, "policy lives at the edges" - other
    hooks are free to append). What we own is not assuming we're the only thing
    that will ever write to that string. `json.loads` requires the *entire* input
    to be consumed and raises `JSONDecodeError: Extra data` the moment anything
    trails the closing brace - so a session-start reminder appended after our
    marker silently made every first-screenshot expansion fail, having nothing to
    do with local vs. remote. `raw_decode` parses one JSON value from the start of
    the string and simply reports where it stopped, so trailing text - ours or
    anyone else's - no longer breaks marker detection.
    """
    return _JSON_DECODER.raw_decode(text.lstrip())[0]


def _parse_marker(content: Any) -> dict[str, Any] | None:
    """Return the computer-use payload if this tool content carries one.

    The orchestrator does not hand our ``ToolResult`` straight to the provider - it
    serialises it into an envelope, so the real payload arrives as a JSON *string*
    nested under ``output``::

        {"error": null, "output": "{\\"__amplifier_computer_use__\\": 1, ...}"}

    Unwrap whatever shape shows up rather than assuming one: envelope, bare payload,
    or already-decoded dict. The content string may also carry trailing text
    appended by another hook (see `_loads_leading`) - tolerate that too.
    """
    if isinstance(content, dict):
        return content if MARKER in content else _parse_marker(content.get("output"))
    if not isinstance(content, str) or MARKER not in content:
        return None
    try:
        data = _loads_leading(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(data, dict):
        if MARKER in data:
            return data
        return _parse_marker(data.get("output"))
    return None


def _image_block(path: str) -> dict[str, Any] | None:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        logger.warning("computer-use: screenshot %s is gone; sending text only", path)
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(raw).decode(),
        },
    }


def _read_content(msg: Any) -> Any:
    """Messages reach the provider as pydantic ``Message`` objects, but plain dicts
    show up in tests and in other orchestrators. Support both."""
    if isinstance(msg, dict):
        return msg.get("content")
    return getattr(msg, "content", None)


def _with_content(msg: Any, content: Any) -> Any:
    """Return a copy of `msg` carrying new content.

    Content is deliberately written as *plain dicts*, not ``TextBlock``/``ImageBlock``
    instances: those serialise an extra ``visibility: null`` key that the Anthropic API
    rejects inside a ``tool_result``. Pydantic emits a cosmetic serializer warning for
    the raw dicts (suppressed at mount) but produces exactly the bytes the API wants.
    """
    if isinstance(msg, dict):
        return {**msg, "content": content}
    try:
        clone = msg.model_copy()
        clone.content = content
        return clone
    except Exception:  # noqa: BLE001 - not a pydantic model; fall back to mutating
        try:
            msg.content = content
        except Exception:  # noqa: BLE001 - immutable message; report and move on
            logger.warning(
                "computer-use: could not rewrite message content on %r",
                type(msg).__name__,
            )
        return msg


def _expand_tool_results(messages: list[Any], max_inline: int) -> list[Any]:
    """Turn screenshot markers into image blocks, newest `max_inline` kept inline."""
    rewritten: list[Any] = []
    budget = max_inline
    for msg in reversed(messages):
        payload = _parse_marker(_read_content(msg))
        if payload is None:
            rewritten.append(msg)
            continue

        text = str(payload.get("text") or "screenshot")
        images = [p for p in payload.get("images", []) if isinstance(p, str)]
        blocks: list[dict[str, Any]] = []
        # Distinguish "budget exhausted before we ever tried this file" (superseded)
        # from "we tried to read it and it was gone" (missing) - these used to share
        # one message ("superseded by a newer screenshot"), which was actively
        # misleading for the missing-file case: the model would be told its
        # screenshot was dropped for recency reasons when the real reason was the
        # file being pruned/unreadable.
        missing = False
        if budget > 0 and images:
            for path in images:
                block = _image_block(path)
                if block is not None:
                    blocks.append(block)
                else:
                    missing = True
            if blocks:
                budget -= 1
        if blocks:
            rewritten.append(
                _with_content(msg, [{"type": "text", "text": text}, *blocks])
            )
        else:
            if not images:
                note = ""
            elif missing:
                note = " [image dropped: screenshot file no longer available]"
            else:
                note = " [image dropped: superseded by a newer screenshot]"
            rewritten.append(_with_content(msg, f"{text}{note}"))
    rewritten.reverse()
    return rewritten


def _select_provider_native_tool_type_on_computer_tool(
    coordinator: Any, native_tool_type: str, model: str | None
) -> None:
    """Prime the mounted tool for a provider-selected native dialect.

    Older/fake tools keep their `note_model()` compatibility path. Lookup and
    selection failures are diagnostic only: a request must not fail here.
    """
    try:
        tool = coordinator.get("tools", "computer")
    except Exception:  # noqa: BLE001 - a lookup failure must never break a request
        logger.debug(
            "computer-use: 'computer' tool lookup failed for native type selection",
            exc_info=True,
        )
        return
    select = getattr(tool, "select_provider_native_tool_type", None)
    if callable(select):
        try:
            select(native_tool_type, model=model)
        except Exception:  # noqa: BLE001 - selection must never take down a request
            logger.debug(
                "computer-use: provider native tool type selection raised unexpectedly",
                exc_info=True,
            )
        return
    note_model = getattr(tool, "note_model", None)
    if not callable(note_model):
        return
    try:
        note_model(model)
    except Exception:  # noqa: BLE001 - note_model must never take down a request
        logger.debug("computer-use: note_model raised unexpectedly", exc_info=True)


def _request_declares_computer_tool(request: Any) -> bool:
    """Return whether this request can be affected by the computer-tool dialect.

    A provider instance can serve unrelated background work as well as a computer-use
    turn. Only a request that actually declares the ``computer`` tool may update the
    mounted tool's provider/model-specific native type.
    """
    tools = getattr(request, "tools", None)
    if not isinstance(tools, list):
        return False
    for tool in tools:
        name = (
            tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
        )
        if name == "computer":
            return True
    return False


def _wrap_provider(provider: Any, coordinator: Any, max_inline: int) -> bool:
    native_tool_type = _provider_supports_native_computer_tool(provider)
    if native_tool_type is None:
        # provider:request fires every turn. The cached negative probe must
        # stay visible once, not drown later tool errors in identical warnings.
        try:
            if getattr(provider, _UNSUPPORTED_WARNING_FLAG, False) is True:
                return False
            setattr(provider, _UNSUPPORTED_WARNING_FLAG, True)
        except Exception:
            # Immutable/proxied providers may reject markers. Stay fail-loud
            # rather than breaking the request or using a global identity cache.
            pass
        logger.warning(
            "computer-use: provider %s (%s) did not prove native computer "
            "passthrough; tried %s. Native computer use is disabled (the provider "
            "may be unsupported or lack these integration points).",
            type(provider).__name__,
            type(provider).__module__,
            _native_wire_probe_names(),
        )
        return False

    _select_provider_native_tool_type_on_computer_tool(
        coordinator, native_tool_type, getattr(provider, "default_model", None)
    )
    if getattr(provider, _WRAPPED_FLAG, False):
        # The dialect is re-primed above for every provider:request, even when
        # this provider's complete() wrapper already exists.
        return False
    # Fail loud (see ComputerUseHookIncompatibleProviderError) BEFORE wrapping, not
    # after: wrapping a stream()-capable provider would "succeed" and log
    # "wrapped provider ... for native computer use" while the wrap is never
    # actually exercised on the request hot path.
    _fail_if_stream_incompatible(provider)
    # Same reasoning, different failure mode: if the mounted orchestrator
    # cannot carry `computer`'s native tool form to the wire on its own, wrapping
    # would still "succeed" while the tool silently degrades to a plain function
    # tool. See ComputerUseNativeToolPassthroughUnsupportedError. (Provider-side
    # compatibility was already confirmed above.)
    _fail_if_orchestrator_native_tool_spec_unsupported(coordinator)
    if not hasattr(provider, "complete"):
        return False

    original = provider.complete

    async def complete(request: Any, **kwargs: Any):
        if _request_declares_computer_tool(request):
            _effective_model = getattr(request, "model", None) or getattr(
                provider, "default_model", None
            )
            _select_provider_native_tool_type_on_computer_tool(
                coordinator, native_tool_type, _effective_model
            )
        try:
            messages = getattr(request, "messages", None)
            if isinstance(messages, list):
                if _TRACE_PATH:
                    for m in messages[-6:]:
                        c = _read_content(m)
                        role = (
                            m.get("role")
                            if isinstance(m, dict)
                            else getattr(m, "role", "?")
                        )
                        _trace(
                            f"  msg role={role} content_type={type(c).__name__} preview={str(c)[:160]!r}"
                        )
                before = sum(1 for m in messages if _parse_marker(_read_content(m)))
                request.messages = _expand_tool_results(messages, max_inline)
                inlined = sum(
                    1 for m in request.messages if isinstance(_read_content(m), list)
                )
                if before:
                    _trace(f"complete: markers={before} messages_with_blocks={inlined}")
        except Exception:
            logger.exception(
                "computer-use: request rewrite failed; sending request unchanged"
            )
        return await original(request, **kwargs)

    provider.complete = complete
    setattr(provider, _WRAPPED_FLAG, True)
    _trace(
        f"WRAPPED provider={type(provider).__name__} module={type(provider).__module__}"
    )
    logger.info(
        "computer-use: wrapped provider %s for screenshot inlining",
        type(provider).__name__,
    )
    return True


#: `computer` action names that mutate the target, mirrored from
#: `tool-computer-use`'s own `MUTATING` set so this hook does not need to
#: import that module (kept small and duplicated deliberately - see
#: `_make_gate_handler`'s docstring for why a local copy is safer here than a
#: cross-module import).
_GATE_MUTATING_ACTIONS = {
    "mouse_move",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "left_mouse_down",
    "left_mouse_up",
    "left_click_drag",
    "scroll",
    "key",
    "hold_key",
    "type",
    "focus_window",
    "set_clipboard",
}


def _interactive_approval_possible() -> bool:
    """Legacy fallback for hosts without an explicit approval transport capability.

    The app-layer `ApprovalSystem` that answers `ask_user` (`amplifier_core.approval`,
    implemented outside this bundle - CLI/web/API) is not something this hook can
    inspect or wrap. What it CAN check, in the same process, is the one precondition
    historical terminal implementations shared: a real terminal to prompt on.
    `sys.stdin.isatty()` is False for exactly the case that used to crash silently -
    a backgrounded run, a piped/redirected stdin, a service with no controlling
    terminal - and True for a normal interactive session, unchanged.

    This is a deliberate, named heuristic, not a certainty: an app layer that answers
    `ask_user` some other way (e.g. a web UI polling a queue) would also read False
    here and be denied - see `_make_gate_handler`'s docstring for why "deny with a
    clear reason" is still the correct, honest default for that case, and how an
    operator gets an explicit way around it.
    """
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        # A closed/replaced stdin (some test harnesses, some service launchers)
        # answers `isatty()` with one of these - treat exactly like "no TTY",
        # never like "yes, interactive" (fail loud, never optimistic).
        return False


def _host_approval_possible(coordinator: Any) -> bool:
    """Prefer an explicit app transport capability; preserve legacy CLI probing.

    A boolean ``approval.interactive`` says that the app can deliver ``ask_user``
    to a person even when the runtime's stdin is a pipe. It grants no permission:
    the normal approval system still owns the answer and deny-by-default policy.
    Explicit False, malformed values and lookup failures never fall back to a TTY.
    Only an absent capability uses the legacy heuristic.
    """
    getter = getattr(coordinator, "get_capability", None)
    if callable(getter):
        try:
            capability = getter("approval.interactive")
        except Exception:  # noqa: BLE001 - uncertain transport must not grant availability
            return False
        if capability is not None:
            return capability is True
    return _interactive_approval_possible()


def _make_gate_handler(coordinator: Any, unattended_writes_ok: bool = False):
    """Build the `tool:pre` handler implementing the write-confirmation gate
    (`docs/designs/remote-transport.md` \u00a710.4): "Gate every WRITE, or gate
    none - anything finer is guesswork wearing a confidence costume."

    This hook is pure POLICY sitting on top of a MECHANISM `ComputerTool`
    already computes and exposes (`gate_writes`, `is_remote` - see
    tool-computer-use's `__init__.py`): whether to gate is the tool's own,
    already-resolved decision (defaults to on for a remote target with
    `read_only=False`, off otherwise - see that module for the exact rule).
    This function only turns "gate_writes is True and this action mutates"
    into the kernel's own `ask_user` mechanism (`HOOKS_API.md`) - it invents
    no new confirmation system of its own, per the design doc's explicit
    guidance not to.

    Real incident this closes: `ask_user`'s approval prompt is answered by an
    app-layer `ApprovalSystem` this bundle does not own (see
    `_interactive_approval_possible`'s docstring) - on a backgrounded run with
    no TTY, that implementation's own `input()` hits immediate EOF and raises
    `EOFError`, which propagates uncaught all the way to the operator as
    `Tool computer failed: EOF when reading a line` - a message that names
    NOTHING (not approval, not the missing terminal, not what to do about
    it). That single line was previously misread as "the remote write path
    was never wired up" - a full misdiagnosis cycle, and false: the write
    path works fine. The fix does not - cannot - patch that external
    approval system; it prevents ever reaching it when this process can
    already tell the prompt cannot be answered, and substitutes a real,
    actionable, named error instead of letting the EOF happen at all.

    `unattended_writes_ok`: the deliberate, explicit, ALWAYS-LOGGED opt-in for
    a run launched on purpose against a target the operator already named,
    with nobody at the keyboard to answer a prompt. Never a default (`False`
    unless a human sets `unattended_writes_ok: true` in this hook's own
    config) and never inferred from the environment - the gate itself is not
    weakened for the interactive case; this only changes what happens on the
    ONE path that used to crash instead of asking or denying.

    This is also the ONE mechanism that keeps `unattended_writes_ok` and
    `gate_writes` from being two write-gates that do not know about each
    other: they are two answers to the same policy question
    (`docs/designs/remote-transport.md` \u00a710.4), not independent
    mechanisms. `ComputerTool`'s own fail-safe (`DesktopTool.execute()`,
    tool-computer-use's `__init__.py`) needs to see the SAME decision this
    handler makes, or it re-denies an action this handler already approved -
    unreachable in exactly the case that shipped it (a remote `focus_window`
    with `unattended_writes_ok: true` set). So on every call, before making
    its own decision, this handler syncs the live value onto the
    `ComputerTool` instance (`computer._unattended_writes_ok`) - always,
    even when `gate_writes` is off or the action is a read, so the tool
    never reads a stale value from an earlier, differently-configured call.

    Same wall, different door: the interactive `ask_user` path this handler
    dispatches to below had the identical problem - a human approves, but
    nothing told `execute()`'s fail-safe that THIS exact call was the one
    approved. `computer._interactive_write_approved`
    (`ComputerTool.__init__`'s docstring has the full rationale) is the
    honest, per-call answer - reset False at the very top of every call
    alongside `_unattended_writes_ok`, set True only immediately before this
    handler hands its decision to `ask_user`. Deliberately a second flag,
    not a second meaning stuffed into `_unattended_writes_ok`: one says
    "nobody's there and that's an explicit opt-in", the other says "someone
    was there and said yes" - and this codebase has already paid once for a
    name that answered a different question than the one it looked like it
    answered.
    """

    async def handler(event: str, data: dict[str, Any]) -> HookResult:
        tool_name = (data or {}).get("tool_name")
        if tool_name not in ("computer", "desktop"):
            return HookResult(action="continue")
        try:
            tool = coordinator.get("tools", tool_name)
        except Exception:  # noqa: BLE001 - a lookup failure degrades to "no gate", never a crash
            return HookResult(action="continue")
        computer = tool if tool_name == "computer" else getattr(tool, "_computer", None)
        if computer is None:
            return HookResult(action="continue")
        # Sync BEFORE the early-return below, unconditionally: this handler is
        # the single source of truth for `unattended_writes_ok`, and
        # `DesktopTool.execute()`'s fail-safe check must always see this
        # call's live value, not a value left behind by a previous one.
        computer._unattended_writes_ok = unattended_writes_ok
        # Same unconditional-reset discipline for the interactive counterpart
        # (`ComputerTool.__init__`'s docstring for `_interactive_write_approved`
        # has the full rationale for why this is a second, honestly-named
        # flag rather than overloading the one above). Reset to False on
        # EVERY call, before any early return - only the `ask_user` branch
        # below ever sets it True, and only for the one call it is deciding
        # right now. This is what makes a previous call's approval unable to
        # authorize this one: by the time this line runs for call N, call
        # N-1's `execute()` has already run and already read whatever this
        # handler set for it.
        computer._interactive_write_approved = False
        if not getattr(computer, "_gate_writes", False):
            return HookResult(action="continue")
        action = (data.get("tool_input") or {}).get("action")
        if action not in _GATE_MUTATING_ACTIONS:
            return HookResult(action="continue")
        backend_name = getattr(getattr(computer, "_backend", None), "name", "?")

        if not _host_approval_possible(coordinator):
            # The EOF fix: never hand this to `ask_user` - the app-layer approval
            # system's own `input()` would hit immediate EOF with no diagnostic at
            # all (see this function's docstring). Decide here instead, with a
            # real reason either way.
            if unattended_writes_ok:
                logger.warning(
                    "computer-use: unattended_writes_ok=True - auto-ALLOWING "
                    "%s.%s on backend %r with NO interactive approval available "
                    "(stdin is not a TTY) and NO human confirmation. This is an "
                    "explicit, logged config opt-in (hook-computer-use config "
                    "'unattended_writes_ok') - not a default and not inferred.",
                    tool_name,
                    action,
                    backend_name,
                )
                return HookResult(action="continue")
            return HookResult(
                action="deny",
                reason=(
                    f"action {tool_name}.{action!r} requires human approval "
                    f"(gate_writes is enabled for backend {backend_name!r}), but "
                    "no interactive approval transport is available to ask "
                    "(the app must advertise approval.interactive, or legacy "
                    "stdin must be a TTY). The write was NOT sent. To proceed: "
                    "(1) run this session interactively so the approval prompt "
                    "can be answered, or (2) set hook-computer-use config "
                    "'unattended_writes_ok: true' to explicitly allow writes on "
                    "this target with no human confirmation - a deliberate, "
                    "logged opt-in, never a default."
                ),
            )

        # Sync BEFORE returning, same as `_unattended_writes_ok` above: `ask_user`
        # is a blocking gate (`HOOKS_API.md`, priority 2, same tier as `deny`) -
        # the kernel calls `DesktopTool.execute()` for THIS call only if the
        # human allows it; a decline or timeout never reaches `execute()` at
        # all. So setting this now is not presuming the answer - it is only
        # ever read on the one path where the answer was already yes.
        computer._interactive_write_approved = True
        return HookResult(
            action="ask_user",
            approval_prompt=(
                f"Remote computer-use ({backend_name}): allow "
                f"{tool_name}.{action!r} on the target desktop?"
            ),
            approval_options=["Allow", "Deny"],
            approval_default="deny",
            reason="gate_writes is enabled for this remote target (\u00a710.4)",
        )

    return handler


def _make_halt_notice_handler(coordinator: Any):
    """Build the `tool:post` handler that closes defect 1
    (`docs/designs/coexistence.md` \u00a76.0): a halted session must not be able
    to reach a final response without the interruption in front of the
    model.

    The `HaltedError` message already reaches the model as this tool call's
    own error result - and a real evaluation run proved that is not enough:
    the model saw five halts spread across a session and still reported
    "Task completed successfully" with zero mention of any interruption
    (`.amplifier/evaluation/computer-use/20260802T113341Z/s2-interrupt-halt/`).
    An isolated tool-error deep in a long transcript is easy for a model to
    fail to surface in a summary written many turns later - especially once
    other, successful actions follow it.

    The fix used here is the kernel's own mechanism (`inject_context`,
    `HOOKS_API.md`), not a second one built on top of it - exactly what
    `coexistence.md` \u00a78.3 already prescribes for pause and asks not be
    reinvented for halt: "Use the kernel's existing mechanism; do not build
    a second one." `ComputerTool.execute()` records every `HaltedError` it
    sees into `computer.halt_notices` (`tool-computer-use/__init__.py`);
    this handler fires on every `tool:post` for `computer`/`desktop` and, as
    long as that list is non-empty, injects a fresh system-role reminder
    that the model cannot avoid seeing on its very next turn - repeated on
    every subsequent tool call for the rest of the session, not just once,
    so it is still there no matter how many more actions happen before the
    model writes its final response. `ephemeral=True` mirrors the same
    reminder pattern the kernel already uses for the task-list nudge every
    turn: a fact that must be fresh on every turn, not one more permanent
    message bloating history.
    """

    async def handler(event: str, data: dict[str, Any]) -> HookResult:
        tool_name = (data or {}).get("tool_name")
        if tool_name not in ("computer", "desktop"):
            return HookResult(action="continue")
        try:
            tool = coordinator.get("tools", tool_name)
        except Exception:  # noqa: BLE001 - a lookup failure must not break the turn
            return HookResult(action="continue")
        computer = tool if tool_name == "computer" else getattr(tool, "_computer", None)
        notices = getattr(computer, "halt_notices", None)
        if not notices:
            return HookResult(action="continue")
        latest = notices[-1]
        safety_notice = (
            "SAFETY NOTICE (computer-use human/agent coexistence guard): "
            f"{len(notices)} human-detected interruption(s) occurred during "
            "this driving session - a person at the machine produced input "
            "the agent did not generate, and writes were halted before the "
            f"next one (docs/designs/coexistence.md \u00a76.0). Most recent: "
            f"{latest['message']} You MUST explicitly acknowledge this "
            "interruption in any summary, report, or completion claim you "
            "give the user - never report unqualified success or that the "
            "task completed cleanly without mentioning it."
        )
        return HookResult(
            action="inject_context",
            context_injection=(
                f'<system-reminder source="hook-computer-use">\n{safety_notice}\n</system-reminder>'
            ),
            context_injection_role="system",
            ephemeral=True,
        )

    return handler


#: `warnings.filterwarnings` compiles `message` and matches it with `re.match`,
#: which anchors at the *start* of the string only - and `.` does not cross a
#: newline unless `re.DOTALL` is in effect. Pydantic's own aggregate warning
#: ("Pydantic serializer warnings:\n  PydanticSerializationUnexpectedValue(...)")
#: puts the token this filter keys on on its *second* line, so the previous
#: pattern (`.*PydanticSerializationUnexpectedValue.*`, no DOTALL) never
#: matched anything, ever - this filter has been dead since the day it was
#: written, and every screenshot has been dumping a stack-trace-shaped
#: `UserWarning` wall into ordinary interactive sessions. `(?s)` turns DOTALL
#: on for the rest of the pattern so `.` crosses the newline.
_PYDANTIC_SERIALIZER_WARNING_MESSAGE_PATTERN = (
    r"(?s).*PydanticSerializationUnexpectedValue.*"
)

#: `warnings.filterwarnings` also accepts `category=`/`module=` to narrow a
#: filter beyond its message text - worth doing here because `UserWarning`
#: (pydantic's aggregate warning's actual category, confirmed by direct
#: capture, not assumed) is the single most commonly reused built-in warning
#: category in the ecosystem. A message-only ignore would also silently eat
#: any *unrelated* warning - ours or a third party's - that happens to raise
#: `UserWarning` with text that happens to contain this substring, for the
#: rest of the process, since `warnings.filterwarnings` mutates a
#: process-global filter list. `module` is matched (again via `re.match`)
#: against the `__name__` of the module whose frame called `warnings.warn()`
#: - confirmed by direct capture to be `pydantic.main` (the Python wrapper
#: that calls `warn()`, not `pydantic_core`'s Rust frame) - so this filter's
#: blast radius is exactly "pydantic's own serializer", never our own code
#: or anyone else's.
_PYDANTIC_SERIALIZER_WARNING_MODULE_PATTERN = r"^pydantic(\..*)?$"


def _install_pydantic_serializer_warning_filter() -> None:
    """Silence pydantic's own "serializer warnings" aggregate `UserWarning`.

    Image blocks are written as plain dicts (see `_with_content`'s docstring)
    so no `visibility: null` reaches the API - a proper `ImageBlock` model
    would serialize that field and the Anthropic API rejects it inside a
    `tool_result`. Pydantic notices the resulting field-type mismatch on every
    `model_dump()` and warns; the serialised bytes are correct either way, so
    this is a real - if cosmetic - noise problem, not a bug in the dicts.

    Scoped narrowly (see the two pattern constants' docstrings) rather than a
    bare message-only ignore, so the blast radius stays "pydantic's own
    serializer warning", not "any UserWarning containing this substring".
    """
    warnings.filterwarnings(
        "ignore",
        message=_PYDANTIC_SERIALIZER_WARNING_MESSAGE_PATTERN,
        category=UserWarning,
        module=_PYDANTIC_SERIALIZER_WARNING_MODULE_PATTERN,
    )


def _pydantic_serializer_warning_is_suppressed() -> bool:
    """Drive a real probe through the filter this module just installed and
    report whether it was actually swallowed.

    This exists because the filter this module has always installed
    previously compiled and "succeeded" cleanly while silently doing nothing,
    for its entire lifetime (see `_PYDANTIC_SERIALIZER_WARNING_MESSAGE_PATTERN`'s
    docstring) - and nothing surfaced that. A filter this easy to silently
    break must prove itself at mount, every mount, rather than being trusted
    forever on the strength of having compiled. The probe reproduces the
    exact raw-dict content shape (`_with_content`) that triggers pydantic's
    real warning, run through `model_dump()`, so this checks the actual
    installed filter against the actual warning shape - not a hand-picked
    string that could pass for the wrong reason.

    Cost: one small `Message` construction and one `model_dump()` call, once
    per mount (not per request) - negligible next to the rest of mount-time
    work in this module.
    """
    from amplifier_core.message_models import Message  # local: probe-only cost

    probe = Message(role="user", content="x")
    # Typed `Any`, matching `_with_content`'s own `content: Any` parameter -
    # the raw-dict shape is the point of the probe (see this function's
    # docstring); a precisely-typed list literal here would make pyright
    # reject exactly the assignment this filter exists to tolerate.
    probe_content: Any = [
        {"type": "text", "text": "hook-computer-use mount-time probe"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "AAA",
            },
        },
    ]
    probe.content = probe_content
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _install_pydantic_serializer_warning_filter()
        probe.model_dump()
    return len(caught) == 0


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Register the provider-request hook that enables native computer use."""
    cfg = config or {}
    max_inline = int(cfg.get("max_inline_screenshots", _DEFAULT_MAX_INLINE_IMAGES))
    _install_pydantic_serializer_warning_filter()
    if not _pydantic_serializer_warning_is_suppressed():
        # Logged, not raised: an unsuppressed warning is cosmetic noise, not a
        # functional break - the serialised image/tool-result bytes are
        # correct either way (see `_install_pydantic_serializer_warning_filter`'s
        # docstring), so mounting must not be blocked on it. But a silent
        # filter that silently stops working is exactly the trap this
        # replaces - this filter was dead for the module's entire lifetime
        # with nothing in the logs to show it. ERROR, not DEBUG/INFO, so a
        # future pydantic message-format change is impossible to miss.
        logger.error(
            "computer-use: pydantic serializer-warning suppression filter "
            "did NOT suppress its own probe warning at mount - pydantic's "
            "warning message format may have changed. Cosmetic only "
            "(serialised image/tool-result bytes are unaffected), but every "
            "screenshot will now log a 'Pydantic serializer warnings' "
            "UserWarning. Update _PYDANTIC_SERIALIZER_WARNING_MESSAGE_PATTERN "
            "/ _PYDANTIC_SERIALIZER_WARNING_MODULE_PATTERN in "
            "amplifier_module_hook_computer_use to match the new shape."
        )
    priority = int(cfg.get("priority", 50))
    # Explicit, always-logged unattended-write opt-in (see `_make_gate_handler`'s
    # docstring) - `False` unless a human sets this, never inferred from the
    # environment. Read here, at mount, from THIS hook's own config - never
    # from `tool-computer-use`'s config, which has no opinion on this.
    unattended_writes_ok = bool(cfg.get("unattended_writes_ok", False))

    async def handler(event: str, data: dict[str, Any]) -> HookResult:
        # The behavior can be loaded without a usable desktop (only the
        # computer_use_unavailable stub), or in a child without computer access.
        # Recheck every turn: runtime activation can mount computer later.
        try:
            if coordinator.get("tools", "computer") is None:
                return HookResult(action="continue")
        except Exception:
            # A failed lookup is not proof of absence. Keep the existing provider
            # compatibility checks active rather than silently bypassing them.
            logger.warning(
                "computer-use: computer tool lookup failed; checking provider "
                "compatibility without confirming tool presence",
                exc_info=True,
            )

        # Providers are guaranteed mounted by the time the loop asks one to run.
        name = (data or {}).get("provider")
        provider = None
        try:
            provider = coordinator.get("providers", name) if name else None
            if provider is None:
                mounted = coordinator.get("providers")
                if isinstance(mounted, dict) and mounted:
                    provider = next(iter(mounted.values()))
        except Exception:
            # This determines whether native computer-use ever engages for the
            # entire session. Previously logged at DEBUG only - invisible at any
            # normal log level, so a persistently failing lookup meant computer-use
            # silently never wrapped anything, with nothing in the logs to show it.
            logger.warning("computer-use: provider lookup failed", exc_info=True)
        if provider is None:
            _trace(f"handler: NO PROVIDER FOUND (name={name!r})")
            # Same reasoning: without this, "no provider found" was visible ONLY
            # via AMPLIFIER_COMPUTER_USE_TRACE, which is off by default. A session
            # that never finds a provider to wrap looks identical - in the normal
            # logs - to one that is working correctly.
            logger.warning(
                "computer-use: no provider found to wrap (requested=%r); "
                "screenshot inlining will not happen this turn",
                name,
            )
        else:
            _wrap_provider(provider, coordinator, max_inline)
        return HookResult(action="continue")

    coordinator.hooks.register(
        PROVIDER_REQUEST, handler, priority=priority, name="hook-computer-use"
    )
    coordinator.hooks.register(
        TOOL_PRE,
        _make_gate_handler(coordinator, unattended_writes_ok=unattended_writes_ok),
        priority=priority,
        name="hook-computer-use-gate",
    )
    coordinator.hooks.register(
        TOOL_POST,
        _make_halt_notice_handler(coordinator),
        priority=priority,
        name="hook-computer-use-halt-notice",
    )
    _trace(f"MOUNTED max_inline={max_inline}")
    logger.info("hook-computer-use mounted (max_inline_screenshots=%d)", max_inline)
    return {
        "name": "hook-computer-use",
        "version": __version__,
        "provides": ["native-computer-use-wire-format"],
        "description": (
            "Verifies native tool-spec passthrough is supported upstream and "
            "returns screenshots as image blocks"
        ),
    }
