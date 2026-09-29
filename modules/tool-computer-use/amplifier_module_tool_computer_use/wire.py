"""The remote-transport wire protocol: newline-delimited JSON, request/response
correlated by monotonic integer `id`, one object per line, UTF-8, no embedded
newlines.

Pure logic only - no sockets, no subprocess, no SSH. `RemoteBackend` and
`remote_agent.py` both import this module so the controller and the agent
share exactly one definition of "what a valid line looks like" and "which ops
may be retried" - see `docs/designs/remote-transport.md` \u00a76.

This module has zero dependencies beyond the stdlib and is safe to import on
the remote target (which may lack `amplifier_core`, PIL, or any of this
bundle's other dependencies) as well as on the controller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

#: Wire protocol version. The controller disconnects (fails loud, never
#: negotiates down) if the agent's handshake reports anything else - see
#: `docs/designs/remote-transport.md` \u00a76.1.
PROTOCOL_VERSION = 1

OpClass = Literal["read", "write", "control"]

#: This table IS the retry policy (\u00a76.3). WRITE ops get zero automatic
#: retries, ever - a lost response does not mean the action didn't land, and
#: replaying it is how a lost `click` response becomes two clicks on
#: "Confirm". READ ops are idempotent by construction. CONTROL ops
#: (`hello`/`release_all`/`bye`) are idempotent by construction too.
READ_OPS = frozenset(
    {
        "probe",
        "screen_geometry",
        "list_monitors",
        "capture",
        "capture_scaled",
        "cursor_position",
        "list_windows",
        "get_clipboard",
        "ping",
        # Coexistence presence read (docs/designs/coexistence.md \u00a75) - forwards
        # to the remote agent's own backend.presence_idle_ms(). Idempotent by
        # construction (a query, no side effect), so READ is the correct class.
        "presence_idle",
        # Coexistence announcement-status read (docs/designs/coexistence.md
        # \u00a78.1/\u00a79.1/\u00a710.3) - has a human clicked Pause/Cancel on the
        # target's own persistent overlay since the last read? A query, no
        # side effect - the same idempotency argument as `presence_idle`.
        "announcement_status",
    }
)
WRITE_OPS = frozenset(
    {
        "move",
        "click",
        "mouse_down",
        "mouse_up",
        "drag",
        "scroll",
        "key",
        "hold_key",
        "hold",
        "type_text",
        "focus_window",
        "set_clipboard",
    }
)
CONTROL_OPS = frozenset(
    {
        "hello",
        "release_all",
        "bye",
        # Raise this target's own session-start disclosure channel
        # (docs/designs/coexistence.md \u00a77, \u00a710.3) - the macOS dialog or the
        # Linux/Windows persistent overlay, whichever `presence_platform`
        # implies. Session-lifecycle, not injected input - CONTROL, matching
        # the design doc's own classification, not WRITE.
        "announce_raise",
    }
)

ALL_OPS = READ_OPS | WRITE_OPS | CONTROL_OPS


class UnknownOpError(ValueError):
    """`classify_op` was asked about an op this protocol version does not know."""


def classify_op(op: str) -> OpClass:
    """Return which retry-policy class `op` belongs to. Raises for an unknown op
    rather than guessing - an op this protocol doesn't know the class of must
    never be silently treated as safe-to-retry."""
    if op in READ_OPS:
        return "read"
    if op in WRITE_OPS:
        return "write"
    if op in CONTROL_OPS:
        return "control"
    raise UnknownOpError(f"unknown op {op!r}; cannot classify for retry policy")


def is_retryable(op: str) -> bool:
    """WRITE ops are never retryable - `docs/designs/remote-transport.md` \u00a76.3
    is explicit that this is policy, not advisory."""
    return classify_op(op) != "write"


@dataclass(frozen=True)
class Request:
    """One outbound wire request. `id` is assigned by the caller (monotonic,
    per-connection) so responses can be correlated even if the agent ever
    replies out of order (Phase 1 does not pipeline, but the shape allows it)."""

    id: int
    op: str
    args: dict[str, Any] = field(default_factory=dict)

    def encode(self) -> bytes:
        """One line of UTF-8 JSON, newline-terminated. No embedded newlines -
        `json.dumps` never emits a literal `\\n` inside a JSON string."""
        return (
            json.dumps({"id": self.id, "op": self.op, "args": self.args}) + "\n"
        ).encode("utf-8")


@dataclass(frozen=True)
class Response:
    """One inbound wire response - either a result or an error, never both."""

    id: int
    ok: bool
    result: Any = None
    error_type: str | None = None
    error_message: str | None = None

    @classmethod
    def decode(cls, line: bytes | str) -> Response:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        data = json.loads(line)
        if "id" not in data or "ok" not in data:
            raise ValueError(f"malformed wire response (missing id/ok): {data!r}")
        if data["ok"]:
            return cls(id=int(data["id"]), ok=True, result=data.get("result"))
        err = data.get("error") or {}
        return cls(
            id=int(data["id"]),
            ok=False,
            error_type=err.get("type", "BackendError"),
            error_message=err.get("message", "remote op failed with no message"),
        )

    def encode(self) -> bytes:
        """Used by the agent side to emit a response line."""
        if self.ok:
            payload: dict[str, Any] = {"id": self.id, "ok": True, "result": self.result}
        else:
            payload = {
                "id": self.id,
                "ok": False,
                "error": {
                    "type": self.error_type or "BackendError",
                    "message": self.error_message or "",
                },
            }
        return (json.dumps(payload) + "\n").encode("utf-8")


class ProtocolMismatchError(RuntimeError):
    """The agent's handshake did not satisfy the controller's requirements.

    Fail-loud, no negotiate-down: `docs/designs/remote-transport.md` \u00a76.1 -
    "The controller fails loud and disconnects if: protocol is not exactly
    what it speaks; agent_sha256 does not match what it deployed;
    probe.available is false; or any required entry in permissions is false."
    """


class AgentBackendUnavailableError(ProtocolMismatchError):
    """A verified payload reports no usable desktop on this target.

    The connection is still rejected; callers may expose an unavailable tool
    instead of treating this operational state as a broken module contract.
    Protocol, payload integrity and required permissions must pass first.
    """


def validate_handshake(
    handshake: dict[str, Any],
    *,
    expected_sha256: str,
    required_permissions: tuple[str, ...] = (),
) -> None:
    """Raise `ProtocolMismatchError` if `handshake` fails any hard requirement.

    Pure validation - no I/O. Called by `RemoteBackend`/`SshTransport` right
    after reading the agent's unsolicited first line.
    """
    if handshake.get("protocol") != PROTOCOL_VERSION:
        raise ProtocolMismatchError(
            f"agent speaks protocol {handshake.get('protocol')!r}, "
            f"controller requires exactly {PROTOCOL_VERSION!r}"
        )
    got_sha = handshake.get("agent_sha256")
    if got_sha != expected_sha256:
        raise ProtocolMismatchError(
            f"deployed payload sha256 {expected_sha256!r} does not match "
            f"agent-reported {got_sha!r} - refusing a possibly-corrupted or "
            "tampered deploy"
        )
    permissions = handshake.get("permissions") or {}
    missing = [p for p in required_permissions if not permissions.get(p, False)]
    if missing:
        raise ProtocolMismatchError(
            f"required permission(s) not granted on target: {missing} "
            f"(reported permissions: {permissions})"
        )
    probe = handshake.get("probe")
    if not isinstance(probe, dict) or not isinstance(probe.get("available"), bool):
        raise ProtocolMismatchError("agent probe must report boolean available")
    if probe["available"] is False:
        raise AgentBackendUnavailableError(
            f"agent backend unavailable: {probe.get('reason', 'no reason given')}"
        )
