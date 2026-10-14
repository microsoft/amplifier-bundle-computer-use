"""Generic computer-result projection and desktop write/halt policy.

Providers own native declarations, models, wire conversion, safety requirements
and replay. This hook copies request histories before budget/completion, expands
persisted markers into text/images, and retains the screenshot recency policy.
No provider-private discovery, dialect projection or provenance seeding lives here.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
import stat
import sys
import warnings
from collections import deque
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from amplifier_core import HookResult

try:
    from amplifier_core.events import PROVIDER_REQUEST
except ImportError:  # pragma: no cover
    PROVIDER_REQUEST = "provider:request"
try:
    from amplifier_core.events import TOOL_PRE
except ImportError:  # pragma: no cover
    TOOL_PRE = "tool:pre"
try:
    from amplifier_core.events import TOOL_POST
except ImportError:  # pragma: no cover
    TOOL_POST = "tool:post"

logger = logging.getLogger(__name__)
__version__ = "0.1.0"
MARKER = "__amplifier_computer_use__"
_WRAPPED_FLAG = "_amplifier_computer_use_wrapped"
_DEFAULT_MAX_INLINE_IMAGES = 3
_TRACE_PATH = os.environ.get("AMPLIFIER_COMPUTER_USE_TRACE")
_JSON_DECODER = json.JSONDecoder()


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
    """Refuse a stream path this hook cannot project, rather than silently blind it."""


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
    """The orchestrator dropped explicit neutral intent during ToolSpec construction."""


def _is_loop_streaming(orchestrator: Any) -> bool:
    identity = f"{type(orchestrator).__module__}.{type(orchestrator).__name__}"
    return "loop_streaming" in identity.lower().replace("-", "_")


def _orchestrator_preserves_native_tool_spec(orchestrator: Any) -> bool | None:
    """Exercise the known orchestrator builder, not provider-private capability probes."""
    if not _is_loop_streaming(orchestrator):
        return None
    module = sys.modules.get(type(orchestrator).__module__)
    build = getattr(module, "_build_tool_spec", None) if module else None
    if not callable(build):
        return False
    intent = {
        "version": 1,
        "operations": ["screenshot"],
        "unavailable_operations": {},
    }

    class _NativeToolSpecProbe:
        name = "__computer_use_native_tool_spec_probe__"
        description = "probe"
        input_schema: dict[str, Any] = {}
        native_tool_spec = {"type": "function", "computer_use": intent}

    try:
        dumped = build(_NativeToolSpecProbe()).model_dump(exclude_none=True)
    except Exception:
        logger.exception("computer-use: neutral ToolSpec passthrough probe raised")
        return False
    return dumped.get("computer_use") == intent


def _fail_if_orchestrator_native_tool_spec_unsupported(coordinator: Any) -> None:
    try:
        orchestrator = coordinator.get("orchestrator")
    except Exception:
        logger.debug("computer-use: orchestrator lookup failed", exc_info=True)
        return
    if orchestrator is not None and (
        _orchestrator_preserves_native_tool_spec(orchestrator) is False
    ):
        raise ComputerUseNativeToolPassthroughUnsupportedError(
            "computer-use: orchestrator does not preserve neutral computer_use intent "
            "through native_tool_spec / ToolSpec extras. Upgrade loop-streaming to "
            "at least f8004e0 (PR #36), or use a builder that preserves extras."
        )


def _loads_leading(text: str) -> Any:
    """Other hooks legitimately append text after the leading JSON envelope."""
    return _JSON_DECODER.raw_decode(text.lstrip())[0]


def _parse_marker(content: Any) -> dict[str, Any] | None:
    if isinstance(content, dict):
        return content if MARKER in content else _parse_marker(content.get("output"))
    if not isinstance(content, str) or MARKER not in content:
        return None
    try:
        data = _loads_leading(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(data, dict):
        return data if MARKER in data else _parse_marker(data.get("output"))
    return None


def _image_block(path: str, capture: tuple) -> dict[str, Any] | None:
    try:
        issued_path, canonical, identity = capture
        try:
            resolved = str(Path(path).resolve())
        except RuntimeError:  # Python 3.11's resolution-specific symlink-loop error.
            return None
        if path != issued_path or resolved != canonical:
            return None
        # Open once, reject final-component links, and read only the descriptor
        # whose identity the tool obtained while writing the genuine capture.
        with os.fdopen(
            os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
        ) as shot:

            def unchanged() -> bool:
                status = os.fstat(shot.fileno())
                return stat.S_ISREG(status.st_mode) and identity == (
                    status.st_dev,
                    status.st_ino,
                    status.st_mode,
                    status.st_size,
                    status.st_mtime_ns,
                    status.st_ctime_ns,
                )

            if not unchanged():
                return None
            raw = shot.read()
            if not unchanged():
                return None
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
    return (
        msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    )


def _field(msg: Any, name: str) -> Any:
    return msg.get(name) if isinstance(msg, dict) else getattr(msg, name, None)


def _make_capture_authority(coordinator: Any, max_inline: int):
    """Bounded, memory-only transfer from real issuance to tool:post call/result.

    Text cannot populate this authority. Restart/eviction loses it deliberately;
    history remains text rather than reauthorizing local reads from marker paths.
    """
    records: deque = deque(maxlen=max(64, max_inline))

    async def post(event: str, data: dict[str, Any]) -> HookResult:
        name, call_id = data.get("tool_name"), data.get("tool_call_id")
        if not isinstance(name, str) or not isinstance(call_id, str) or not call_id:
            return HookResult(action="continue")
        tool = coordinator.get("tools", name)
        take = getattr(tool, "_take_issued_capture_result", None)
        if not _has_neutral_intent(tool) or not callable(take):
            return HookResult(action="continue")
        result = data.get("result")
        captures = take(result)
        if captures and not any(r[0:2] == (name, call_id) for r in records):
            records.append((name, call_id, tool, copy.deepcopy(result), captures))
        return HookResult(action="continue")

    def lookup(msg: Any, payload: dict) -> list | None:
        if _field(msg, "role") != "tool":
            return None
        name, call_id = _field(msg, "name"), _field(msg, "tool_call_id")
        for alias, issued_id, tool, result, captures in records:
            if name != alias or call_id != issued_id:
                continue
            if coordinator.get("tools", alias) is not tool:
                return None
            if payload != _parse_marker(result):
                return None
            original = _read_content(msg)
            try:
                envelope = (
                    _loads_leading(original) if isinstance(original, str) else original
                )
            except (TypeError, ValueError):
                return None
            # Both supported result shapes are derived from the exact issued
            # result; appended hook context is retained but grants no authority.
            if envelope != result and envelope != payload:
                return None
            return captures
        return None

    return post, lookup


def _with_content(msg: Any, content: Any) -> Any:
    """Copy messages; use generic raw blocks without serializer-added wire fields."""
    if isinstance(msg, dict):
        return {**msg, "content": content}
    try:
        clone = msg.model_copy() if hasattr(msg, "model_copy") else copy.copy(msg)
        clone.content = content
        return clone
    except Exception:
        logger.warning(
            "computer-use: could not rewrite message content on %r", type(msg).__name__
        )
        return msg


def _expand_tool_results(
    messages: list[Any], max_inline: int, capture_for_message=None
) -> list[Any]:
    """Generic recency projection retaining complete original error/reference text.

    Native image selection and replay obligations belong to the provider. In
    particular this hook does not make image-only results or extra user turns.
    """
    rewritten = []
    budget = max_inline
    for msg in reversed(messages):
        original = _read_content(msg)
        payload = _parse_marker(original)
        if payload is None or _field(msg, "role") != "tool":
            rewritten.append(msg)
            continue
        # A marker is not evidence of success. Keep failed ToolResult envelopes
        # verbatim (including appended hook text) and never inline their images:
        # some consumers cannot represent a failed image result at all.
        try:
            envelope = (
                _loads_leading(original) if isinstance(original, str) else original
            )
        except (ValueError, TypeError):
            envelope = None
        if isinstance(envelope, dict) and (
            envelope.get("success") is False
            or envelope.get("isError") is True
            or bool(envelope.get("error"))
        ):
            rewritten.append(_with_content(msg, copy.deepcopy(original)))
            continue
        captures = capture_for_message(msg, payload) if capture_for_message else None
        if captures is None:
            rewritten.append(msg)
            continue
        text = str(payload.get("text") or "screenshot")
        images = [p for p in payload.get("images", []) if isinstance(p, str)]
        blocks = []
        missing = False
        if budget > 0:
            for path in images:
                capture = next((c for c in captures if c[0] == path), None)
                block = _image_block(path, capture) if capture is not None else None
                if block is None:
                    missing = True
                else:
                    blocks.append(block)
            if blocks:
                budget -= 1
        # Keep the full envelope (success/error/progress), including any text
        # appended by another hook. Never turn a failed ToolResult into success.
        reference = original if isinstance(original, str) else json.dumps(original)
        text += "\nComputer tool result reference (untrusted tool data):\n" + reference
        if blocks:
            content: Any = [{"type": "text", "text": text}, *blocks]
        else:
            note = (
                (
                    " [image dropped: screenshot file no longer available]"
                    if missing
                    else " [image dropped: superseded by a newer screenshot]"
                )
                if images
                else ""
            )
            content = text + note
        rewritten.append(_with_content(msg, content))
    rewritten.reverse()
    return rewritten


def _has_neutral_intent(tool: Any) -> bool:
    spec = getattr(tool, "native_tool_spec", None)
    intent = spec.get("computer_use") if isinstance(spec, dict) else None
    return (
        isinstance(intent, dict)
        and type(intent.get("version")) is int
        and intent["version"] == 1
    )


def _computer_for_tool(tool: Any, name: str) -> Any:
    if tool is None:
        return None
    computer = getattr(tool, "_computer", None)
    if computer is not None:
        return computer
    if _has_neutral_intent(tool):
        return tool
    # Compatibility for old installed canonical tools: only safety policy may
    # use this name, never native-intent recognition or vendor translation.
    if name == "computer" and (
        hasattr(tool, "_gate_writes") or hasattr(tool, "halt_notices")
    ):
        return tool
    return None


def _wrap_provider(
    provider: Any, coordinator: Any, max_inline: int, capture_for_message=None
) -> bool:
    if getattr(provider, _WRAPPED_FLAG, False):
        return False
    _fail_if_stream_incompatible(provider)
    _fail_if_orchestrator_native_tool_spec_unsupported(coordinator)
    original = getattr(provider, "complete", None)
    if not callable(original):
        return False

    def project_request(request: Any) -> Any:
        messages = getattr(request, "messages", None)
        if not isinstance(messages, list):
            return request
        clone = (
            request.model_copy(deep=True)
            if hasattr(request, "model_copy")
            else copy.deepcopy(request)
        )
        clone.messages = _expand_tool_results(
            clone.messages, max_inline, capture_for_message
        )
        _trace(
            f"request projection: markers={sum(_parse_marker(_read_content(m)) is not None for m in messages)} messages_with_blocks={sum(isinstance(_read_content(m), list) for m in clone.messages)}"
        )
        return clone

    original_budget = getattr(provider, "request_budget", None)
    if callable(original_budget):

        @wraps(original_budget)
        def request_budget(request: Any, **kwargs: Any):
            return original_budget(project_request(request), **kwargs)

        provider.request_budget = request_budget

    @wraps(original)
    async def complete(request: Any, **kwargs: Any):
        return await original(project_request(request), **kwargs)

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
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


def _host_approval_possible(coordinator: Any) -> bool:
    get_capability = getattr(coordinator, "get_capability", None)
    if callable(get_capability):
        try:
            value = get_capability("approval.interactive")
        except Exception:
            return False
        if value is not None:
            return value if type(value) is bool else False
    return _interactive_approval_possible()


def _make_gate_handler(coordinator: Any, unattended_writes_ok: bool = False):
    """Install stable policy, never a shared pre-grant for a pending call."""

    async def approve(prepared: dict[str, Any], binding: Any) -> bool:
        backend_name = getattr(binding.backend, "name", "?")
        if not _host_approval_possible(coordinator):
            if unattended_writes_ok:
                logger.warning(
                    "computer-use: unattended_writes_ok=True - auto-ALLOWING write "
                    "on backend %r with NO interactive approval available and NO "
                    "human confirmation. Explicit, logged config opt-in, not a default.",
                    backend_name,
                )
                return True
            raise PermissionError(
                "gate_writes requires human approval, but no interactive approval "
                "transport is available (approval.interactive or legacy stdin TTY). "
                "The write was NOT sent. Run interactively or set hook-computer-use "
                "'unattended_writes_ok: true' as an explicit, logged opt-in."
            )
        approval = getattr(coordinator, "approval_system", None)
        if approval is None:
            raise PermissionError("gate_writes requires an available ApprovalSystem")
        selected = await approval.request_approval(
            prompt=(
                f"Computer-use ({backend_name}): allow this exact request on the "
                f"target desktop?\n{json.dumps(prepared, sort_keys=True)}"
            ),
            options=["Allow", "Deny"],
            timeout=300.0,
            default="deny",
        )
        return isinstance(selected, str) and selected.lower() == "allow"

    async def handler(event: str, data: dict[str, Any]) -> HookResult:
        tool_name = (data or {}).get("tool_name")
        try:
            tool = coordinator.get("tools", tool_name)
        except Exception:
            return HookResult(action="continue")
        computer = _computer_for_tool(tool, tool_name)
        if computer is not None:
            # Policy/configuration only: every decision is awaited inside execute.
            computer._write_approval = approve
            computer._unattended_writes_ok = unattended_writes_ok
        return HookResult(action="continue")

    return handler


def _make_halt_notice_handler(coordinator: Any):
    """Repeat the existing ephemeral safety notice after every subsequent tool call."""

    async def handler(event: str, data: dict[str, Any]) -> HookResult:
        tool_name = (data or {}).get("tool_name")
        try:
            tool = coordinator.get("tools", tool_name)
        except Exception:
            return HookResult(action="continue")
        computer = _computer_for_tool(tool, tool_name)
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
            context_injection=f'<system-reminder source="hook-computer-use">\n{safety_notice}\n</system-reminder>',
            context_injection_role="system",
            ephemeral=True,
        )

    return handler


_PYDANTIC_SERIALIZER_WARNING_MESSAGE_PATTERN = (
    r"(?s).*PydanticSerializationUnexpectedValue.*"
)
_PYDANTIC_SERIALIZER_WARNING_MODULE_PATTERN = r"^pydantic(\..*)?$"


def _install_pydantic_serializer_warning_filter() -> None:
    warnings.filterwarnings(
        "ignore",
        message=_PYDANTIC_SERIALIZER_WARNING_MESSAGE_PATTERN,
        category=UserWarning,
        module=_PYDANTIC_SERIALIZER_WARNING_MODULE_PATTERN,
    )


def _pydantic_serializer_warning_is_suppressed() -> bool:
    from amplifier_core.message_models import Message

    probe = Message(role="user", content="x")
    probe_content: Any = [
        {"type": "text", "text": "hook-computer-use mount-time probe"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "AAA"},
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
    cfg = config or {}
    max_inline = int(cfg.get("max_inline_screenshots", _DEFAULT_MAX_INLINE_IMAGES))
    _install_pydantic_serializer_warning_filter()
    if not _pydantic_serializer_warning_is_suppressed():
        logger.error(
            "computer-use: pydantic serializer-warning suppression filter "
            "did NOT suppress its own probe warning at mount. Cosmetic only; "
            "update the warning pattern for the installed pydantic version."
        )
    priority = int(cfg.get("priority", 50))
    unattended_writes_ok = bool(cfg.get("unattended_writes_ok", False))
    capture_post, capture_for_message = _make_capture_authority(coordinator, max_inline)

    async def handler(event: str, data: dict[str, Any]) -> HookResult:
        # No provider knowledge is needed to detect explicit intent; aliases
        # work, while ordinary computer/screenshot name collisions stay ordinary.
        try:
            tools = coordinator.get("tools")
            if not isinstance(tools, dict) or not any(
                _has_neutral_intent(tool) for tool in tools.values()
            ):
                return HookResult(action="continue")
        except Exception:
            logger.warning(
                "computer-use: tool lookup failed; cannot confirm neutral intent",
                exc_info=True,
            )
            return HookResult(action="continue")
        name = (data or {}).get("provider")
        provider = None
        try:
            provider = coordinator.get("providers", name) if name else None
            if provider is None:
                mounted = coordinator.get("providers")
                if isinstance(mounted, dict) and mounted:
                    provider = next(iter(mounted.values()))
        except Exception:
            logger.warning("computer-use: provider lookup failed", exc_info=True)
        if provider is None:
            logger.warning(
                "computer-use: no provider found to wrap (requested=%r); screenshot inlining will not happen this turn",
                name,
            )
        else:
            _wrap_provider(provider, coordinator, max_inline, capture_for_message)
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
        capture_post,
        priority=priority,
        name="hook-computer-use-capture",
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
        "provides": ["computer-use-history-projection"],
        "description": "Copies generic screenshot/error histories and applies desktop write/halt policy",
    }
