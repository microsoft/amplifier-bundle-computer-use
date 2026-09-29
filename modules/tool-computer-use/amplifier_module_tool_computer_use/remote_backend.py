"""`RemoteBackend`: implements the `Backend` protocol by marshalling every call
across `SshTransport` to the SAME platform backend code running on the target -
`docs/designs/remote-transport.md` \u00a75.

Owns request ids, op classification, and the one retry rule that matters:
WRITE ops are never retried (\u00a76.3) - a lost response does not mean the
action didn't land.
"""

from __future__ import annotations

import itertools
import logging
import time
from typing import Any

from .backend import (
    BackendError,
    MonitorInfo,
    ProbeResult,
    ScreenGeometry,
    WindowInfo,
    WindowList,
)
from .ssh_transport import AgentStderrError, SshConnectError, SshTransport
from .wire import AgentBackendUnavailableError, Request, Response, classify_op

logger = logging.getLogger(__name__)


def _decode_rect(raw: Any) -> tuple[int, int, int, int] | None:
    """The inverse of `remote_agent._op_list_windows`'s `list(w.rect)`
    encoding: a 4-element JSON array back into `WindowInfo.rect`'s tuple
    shape, or `None` if the agent reported no geometry (or sent something
    malformed) for this window - never a guess."""
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        left, top, right, bottom = (int(v) for v in raw[:4])
    except (TypeError, ValueError):
        return None
    return left, top, right, bottom


class RemoteTargetUnavailable(AgentStderrError):
    """An explicit `target:` could not be reached or has no usable desktop.

    Deliberately NOT a subclass of `registry.NoBackendAvailable` - that type is
    caught by `mount()` and degrades to a silent skip, which is exactly the
    wrong behavior for a remote target (\u00a79 / acceptance item 7): "an
    unreachable target fails loud at mount and does not fall back to the
    controller's local desktop". Falling back silently to a local backend
    when a specific remote machine was asked for would mean the agent starts
    driving the WRONG desktop with zero indication - the worst possible
    outcome this design can produce (\u00a79).
    """


class RemoteBackend:
    """`Backend` implementation that drives a target machine over SSH.

    `is_remote = True` is a plain class attribute other modules (`ComputerTool`,
    the gate hook) check via `getattr(backend, "is_remote", False)` - no
    isinstance check, no import coupling, so any future backend can opt into
    the same remote-safety defaults without this module needing to know about
    it (matches the Backend `Protocol`'s own duck-typing shape).
    """

    is_remote = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.name = f"remote-ssh:{cfg.get('_host', '?')}"
        self._transport: SshTransport | None = cfg.get("_transport")
        self._ids = itertools.count(1)
        self._connected = False
        # Coexistence (docs/designs/coexistence.md \u00a75): the bare REMOTE
        # backend name (e.g. "windows-wsl2", "linux-x11", "macos") from the
        # handshake, as reported by the agent's own `self.backend.name` on
        # the target - NOT the composite `self.name` above (which is
        # "remote-ssh:<that value>" and is deliberately kept as the durable
        # halt-state / log key, unique per remote target). `_build_coexistence_guard`
        # (`__init__.py`) resolves the GUARD_MS band from THIS, never from
        # `self.name` - "remote-ssh:windows-wsl2" is not a `GUARD_MS` key and
        # never will be; the underlying platform's measured band is. `None`
        # until `connect()` completes a handshake.
        self.presence_platform: str | None = None
        # M1 (docs/designs/capability-awareness.md \u00a74/\u00a75.3): the whole
        # handshake dict `connect()` receives - every permission/capability/
        # ops fact the agent could determine about ITSELF, computed on the
        # target. `None` until `connect()` completes. This is a CONNECT-TIME
        # SNAPSHOT, never refreshed - `doctor` reports it alongside
        # `handshake_age_seconds` so a stale fact is never presented as
        # current (permissions can be revoked, a screen can be locked, mid-
        # session, long after this dict was built).
        self.handshake: dict[str, Any] | None = None
        self._connected_at: float | None = None

    @property
    def handshake_age_seconds(self) -> float | None:
        """Seconds since `handshake` was captured, or `None` before
        `connect()` has run. See `handshake`'s own docstring for why this
        age must travel with every fact read from it."""
        if self._connected_at is None:
            return None
        return time.monotonic() - self._connected_at

    @property
    def user_host(self) -> str:
        """The actual `user@host` (or bare `host`) string this backend talks
        to - unlike `self.name` (`"remote-ssh:<platform>"`, identical for
        any two different hosts running the same platform), this is unique
        per TARGET. Used by `__init__.py`'s `_channel_identity` AND
        `_halt_key` to key the one-disclosure-decision-per-machine cache
        and the durable-halt-state record respectively; every consumer that
        shares the same target already shares one `SshTransport`/
        `SharedTransportHandle` via `registry._build_ssh_transport`
        (`shared_transport.py`), so `self._transport.user_host` is the same
        string for all of them. `"?"` only if no transport was ever
        configured (should not happen via `registry.select_backend`).

        Bug-hunt defect B: folds in the transport's configured `port` when
        one is set (`ssh://host:2222`), so two targets that differ ONLY by
        port are not misidentified as the same physical machine either -
        the exact same class of bug defect A closed for hostname. `None`
        port (the common case, standard port 22) leaves this byte-identical
        to the pre-port-support string - `self._transport.user_host` itself
        never carries a port (see `SshTransport.port`/`_ssh_opts`, which
        thread it to `ssh -p` separately instead).
        """
        if self._transport is None:
            return "?"
        host = self._transport.user_host
        port = getattr(self._transport, "port", None)
        return f"{host}:{port}" if port else host

    # -- connection lifecycle (used by registry.select_backend) -------------

    def connect(
        self,
        *,
        required_permissions: tuple[str, ...] = (),
        connect_timeout: float = 30.0,
    ) -> dict[str, Any]:
        if self._transport is None:
            raise RemoteTargetUnavailable("no transport configured for RemoteBackend")
        try:
            handshake = self._transport.connect(
                required_permissions=required_permissions,
                connect_timeout=connect_timeout,
            )
        except SshConnectError as exc:
            raise RemoteTargetUnavailable(
                exc.message, agent_stderr=exc.agent_stderr
            ) from exc
        except AgentBackendUnavailableError as exc:
            raise RemoteTargetUnavailable(
                f"{exc}. Check the configured target's active desktop and "
                "permissions, then retry activation. No other computer was selected."
            ) from exc
        self._connected = True
        self.name = f"remote-ssh:{handshake.get('backend', '?')}"
        self.presence_platform = handshake.get("backend")
        # M1: bind the handshake instead of letting the caller discard it -
        # see `handshake`'s own docstring.
        self.handshake = handshake
        self._connected_at = time.monotonic()
        return handshake

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
        self._connected = False

    # -- request/response plumbing -------------------------------------------

    def _call(self, op: str, **args: Any) -> Any:
        if self._transport is None or not self._connected:
            raise BackendError(f"remote backend not connected (op={op!r})")
        req = Request(id=next(self._ids), op=op, args=args)
        op_class = classify_op(op)
        try:
            line = self._transport.send(req.encode())
        except SshConnectError as exc:
            # \u00a76.3: WRITE ops are NEVER retried, here or anywhere else in
            # this class - a lost response does not tell us whether the
            # action landed, and replaying it is how you get two clicks on
            # "Confirm". READ/CONTROL failures still just surface as an
            # error; nothing in this method silently retries any op class.
            raise BackendError(f"remote op {op!r} ({op_class}) failed: {exc}") from exc
        resp = Response.decode(line)
        if resp.id != req.id:
            raise BackendError(
                f"wire desync: sent id={req.id}, got id={resp.id} for op {op!r}"
            )
        if not resp.ok:
            raise BackendError(f"{resp.error_type}: {resp.error_message}")
        return resp.result

    # -- coexistence presence source (docs/designs/coexistence.md §5) ---------

    def presence_idle_ms(self) -> float:
        """Milliseconds since the REMOTE machine last saw any input.

        Without this method `_build_coexistence_guard` (`__init__.py`) builds
        no guard at all, because it gates on `getattr(backend,
        "presence_idle_ms", None)`. That is exactly what happened before this
        existed: every `target: ssh://...` session - on every platform - ran
        with zero coexistence protection, which is the primary deployment
        shape (a headless controller driving a desktop over SSH). A live
        session against remote Windows confirmed it: `guard built: False`, and
        no halt record was ever written.

        The read is forwarded over the same per-target singleton transport
        every other op uses - no second connection, no new process. It is
        classified READ in `wire.py`, so the `_call` docstring's rule that
        WRITE ops are never retried does not loosen here.

        Deliberately NOT piggybacked onto an existing response: `PresenceMonitor`
        computes `last_input_at = now - idle_ms`, so a value captured during an
        earlier round trip yields an arithmetically wrong timestamp, and wrong
        in the permissive direction (it makes a human look older than they are).
        A stale presence read is a false negative in a safety gate, which is the
        one failure mode this whole mechanism exists to prevent. One extra
        round trip per guarded write, bounded by tailnet RTT, is the honest
        price.
        """
        result = self._call("presence_idle")
        value = result.get("idle_ms") if isinstance(result, dict) else result
        if value is None:
            raise BackendError(
                "remote agent returned no idle_ms for presence_idle - the "
                "target's backend has no presence source, so coexistence "
                "cannot be enforced for it"
            )
        return float(value)

    # -- coexistence announcement channel (docs/designs/coexistence.md §7) ----

    def announce_raise(self, **kwargs: Any) -> dict[str, Any]:
        """Ask the target to raise its OWN session-start disclosure channel -
        the macOS announce-and-acknowledge dialog, or the persistent Linux/
        Windows overlay - via the `announce_raise` CONTROL op
        (§10.3). The channel always runs on the target (only that process
        can draw on that desktop, or has a console session to prompt) -
        this method only ever asks for it over the already-open transport
        and reports back what happened; `__init__.py`'s
        `_build_remote_announcement` applies the same §7.3/§7.6 policy to
        the result that the local branches already apply to a LOCAL
        `announce_macos.announce()`/overlay call.

        `**kwargs` are forwarded verbatim as the op's `args` - `message`/
        `timeout_seconds` for the macOS dialog, `screen_width`/`screen_x`/
        `screen_y` for a persistent overlay (see `remote_agent.RemoteAgent
        ._op_announce_raise` for what each backend actually reads).
        """
        result = self._call("announce_raise", **kwargs)
        return result if isinstance(result, dict) else {}

    def announcement_status(self) -> dict[str, Any]:
        """Has a human clicked Pause/Cancel on the target's own persistent
        overlay since the caller last asked (§8.1/§9.1)? The overlay lives
        entirely on the target - a click there is otherwise invisible to
        this controller, since nothing here observes the target's real
        input events. Forwards the `announcement_status` READ op; `{}`
        (never fabricated) if the target reports something unusable.
        """
        result = self._call("announcement_status")
        return result if isinstance(result, dict) else {}

    # -- Backend protocol -----------------------------------------------------

    def probe(self) -> ProbeResult:
        if not self._connected:
            return ProbeResult(False, "not connected")
        try:
            result = self._call("probe")
            return ProbeResult(bool(result.get("available")), result.get("reason", ""))
        except BackendError as exc:
            return ProbeResult(False, str(exc))

    def screen_geometry(self) -> ScreenGeometry:
        r = self._call("screen_geometry")
        return ScreenGeometry(
            width=r["width"],
            height=r["height"],
            origin_x=r.get("origin_x", 0),
            origin_y=r.get("origin_y", 0),
        )

    def list_monitors(self) -> list[MonitorInfo]:
        return [
            MonitorInfo(
                id=m["id"],
                x=m["x"],
                y=m["y"],
                width=m["width"],
                height=m["height"],
                primary=m.get("primary", False),
                name=m.get("name", ""),
            )
            for m in self._call("list_monitors")
        ]

    def capture(self, region: tuple[int, int, int, int] | None = None) -> bytes:
        # Phase 1 does not wire the raw `capture` op for routine use - C1's
        # whole point is that the SCALED path is what should be exercised on
        # the hot path. `capture_scaled` (below) is the capability
        # `imaging.capture_scaled_b64` prefers when it is present.
        raise BackendError(
            "RemoteBackend.capture() (native resolution over the wire) is not "
            "supported in Phase 1 - use capture_scaled (see C1 in "
            "docs/designs/remote-transport.md)"
        )

    def capture_scaled(
        self,
        region: tuple[int, int, int, int] | None,
        model_size: tuple[int, int],
        max_edge: int,
        max_pixels: int,
    ) -> str:
        """C1: the capability `imaging.capture_scaled_b64` detects via the
        class-descriptor idiom and prefers over `capture()` + local PIL resize
        - the agent downscales BEFORE the bytes cross the wire."""
        model_w, model_h = model_size
        result = self._call(
            "capture_scaled",
            region=list(region) if region else None,
            model_w=model_w,
            model_h=model_h,
            max_edge=max_edge,
            max_pixels=max_pixels,
        )
        return result["png"]

    def cursor_position(self) -> tuple[int, int]:
        r = self._call("cursor_position")
        return r["x"], r["y"]

    def move(self, x: int, y: int) -> None:
        self._call("move", x=x, y=y)

    def click(
        self, x: int | None, y: int | None, button: str = "left", count: int = 1
    ) -> None:
        self._call("click", x=x, y=y, button=button, count=count)

    def mouse_down(self, x: int | None, y: int | None, button: str = "left") -> None:
        """The agent registers this in its held-input ledger (\u00a710.2) so a
        link death between this call and the matching `mouse_up` still
        guarantees release - see `remote_agent.RemoteAgent._op_mouse_down`.
        Anthropic's action set exposes `left_mouse_down`/`left_mouse_up`
        independently, so the model can legitimately leave this half-open
        across two separate tool calls."""
        self._call("mouse_down", x=x, y=y, button=button)

    def mouse_up(self, x: int | None, y: int | None, button: str = "left") -> None:
        self._call("mouse_up", x=x, y=y, button=button)

    def drag(self, start: tuple[int, int] | None, end: tuple[int, int]) -> None:
        """Stays ONE wire round trip, matching \u00a710.2's explicit instruction:
        never decompose a drag into `mouse_down` + `move` + `mouse_up` across
        the wire - a link failure between frames would strand a held button.
        The agent's own `Backend.drag()` (already proven on all three
        platforms) does the press/move/release atomically on the target."""
        self._call(
            "drag",
            start=list(start) if start is not None else None,
            end=list(end),
        )

    def scroll(self, x: int | None, y: int | None, direction: str, amount: int) -> None:
        self._call("scroll", x=x, y=y, direction=direction, amount=amount)

    def key(self, combo: str) -> None:
        self._call("key", combo=combo)

    def hold_key(self, combo: str, duration: float) -> None:
        self._call("hold_key", combo=combo, duration=duration)

    def type_text(self, text: str, guard: Any = None) -> None:
        """See `Backend.type_text` for the `guard` parameter's contract.
        Accepted for protocol conformance but not used: the remote agent
        runs its own copy of a platform backend on the target (\u00a75 of
        `docs/designs/remote-transport.md`), so any intra-op presence
        detection belongs on that side, not here."""
        del guard
        self._call("type_text", text=text)

    def list_windows(self) -> WindowList:
        result = self._call("list_windows")
        windows = [
            WindowInfo(
                str(w["handle"]),
                str(w["title"]),
                bool(w.get("minimized", False)),
                rect=_decode_rect(w.get("rect")),
            )
            for w in result.get("windows", [])
        ]
        return WindowList(windows, result.get("foreground"))

    def focus_window(self, handle: str) -> None:
        self._call("focus_window", handle=handle)

    def get_clipboard(self) -> str:
        result = self._call("get_clipboard")
        return result.get("text", "") if isinstance(result, dict) else str(result)

    def set_clipboard(self, text: str) -> None:
        self._call("set_clipboard", text=text)

    # -- Phase-1-only diagnostic op: the ledger proof (see remote_agent.py) ---

    def hold(self, key: str) -> list[str]:
        result = self._call("hold", key=key)
        return list(result.get("held", []))

    def release_all(self) -> list[str]:
        result = self._call("release_all")
        return list(result.get("released", []))
