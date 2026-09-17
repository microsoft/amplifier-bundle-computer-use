"""Standalone remote computer-use agent - Phase 1.

This is the process that runs ON the target machine, reached over SSH by
`SshTransport`, and speaks the NDJSON wire protocol defined in `wire.py`. It
has ONE job: read one JSON request per line from stdin, execute it against
whichever platform `Backend` `registry.select_backend()` picks for THIS
machine (the exact same `MacOSBackend`/`LinuxX11Backend`/`WindowsBackend` that
already back the local path and its 83 tests - see
`docs/designs/remote-transport.md` \u00a72, "one implementation, two deployment
shapes"), and write one JSON response per line to stdout.

Deployment note: this module is designed to be `runpy.run_module()`'d after a
bootstrap stub extracts this package's files into a scratch directory and adds
it to `sys.path` - see `ssh_transport.py`. It has NO dependency on
`amplifier_core` (this whole package doesn't - see `pyproject.toml`), so it
runs standalone on a target that has never heard of Amplifier. Pillow IS a
real dependency (needed for `capture_scaled`) - `ssh_transport.py` provisions
it via `uv run --with pillow`, per the verified environment facts in the
design doc (\u00a73.1): both real targets have `uv` but neither has `pip3`
guaranteed nor Pillow pre-installed.

Stdout hygiene (\u00a76.4) happens FIRST, before any other import, because
PyObjC (macOS) and some X11 bindings can print to stdout on import, which
would corrupt the very first bytes of the protocol stream.
"""

from __future__ import annotations

import os
import sys

# -- \u00a76.4: stdout hygiene, before any other import ------------------------
# Duplicate the real stdout fd into a private "protocol channel" fd, then
# redirect fd 1 (stdout) to fd 2 (stderr). Everything after this line that
# writes to `print()`/`sys.stdout` lands on stderr (agent logs, diagnostics);
# only writes through `_PROTO` are the wire protocol. `ssh -T` (no pty) means
# no line-ending translation to worry about either side.
_PROTO = os.fdopen(os.dup(1), "w", encoding="utf-8")
os.dup2(2, 1)

import argparse
import base64
import io
import json
import logging
import signal
import sys as _sys
import threading
import traceback
from collections.abc import Callable
from typing import Any, ClassVar

# Package-relative imports: this module always runs as part of the
# `amplifier_module_tool_computer_use` (locally) or a same-shaped extracted
# package (remotely) - see `ssh_transport.PAYLOAD_MODULES`.
from . import announce_macos
from .agent_scratch_lease import (
    STALE_AGENT_DIR_MIN_AGE_SECONDS,
    sweep_stale_agent_dirs,
)
from .backend import Backend, BackendError
from .exclusion import ExclusionZone
from .ledger import DEFAULT_DEADMAN_SECONDS, HeldInputLedger
from .overlay_linux import LinuxOverlay
from .overlay_windows import WindowsOverlay
from .registry import NoBackendAvailable, select_backend
from .wire import PROTOCOL_VERSION, Request, Response, classify_op

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s agent %(levelname)s %(message)s",
)
logger = logging.getLogger("amplifier-cu-agent")


def _content_hash() -> str:
    """The sha256 the controller computed over the exact bytes it sent, stashed
    by the bootstrap stub - see `ssh_transport.py`'s `_BOOTSTRAP_STUB`. Reported
    back in the handshake so the controller can fail loud on any mismatch
    (\u00a76.1/\u00a77) - this is NOT recomputed here, it is the receiver's own
    independent hash of what it actually read off the wire, compared against
    the sender's hash of what it actually sent.
    """
    return os.environ.get("AMPLIFIER_CU_AGENT_SHA256", "")


def _probe_permissions(backend: Backend) -> dict[str, bool]:
    """Best-effort, backend-specific permission probe for the handshake.

    \u00a76.1: "Discovering a TCC denial at connect time is a clear error
    message; discovering it at first click is a mystery." macOS TCC grants
    attach to the *responsible* process, which can differ depending on the
    launch chain (`python3` directly vs. `uv run`) - so this probes for real,
    every connection, rather than trusting a value cached from a previous run.

    Both checks below are deliberately PROMPT-FREE and NON-CAPTURING: this
    runs unconditionally on the handshake's critical path (a fixed 30s
    budget - see `SshTransport.connect`'s `connect_timeout`), even when
    `read_only=True` and nothing has asked to view the screen yet. A real
    `backend.capture()` used to stand in here as a proxy for "is Screen
    Recording granted?" - but `capture()` (`macos.py`) is a straight-line,
    UNBOUNDED Core Graphics call (`CGDisplayCreateImage`/
    `CGWindowListCreateImage`) with no timeout and no cancellation path,
    plumbed through a lock-state check that shells out to `ioreg` first.
    Measured live (12+ trials against an idle macOS target): that whole
    path is normally cheap (tens to a few hundred ms), but nothing bounds
    its worst case - a WindowServer that is mid-transition (display
    sleep/wake, lock/unlock, external-monitor negotiation) can legitimately
    block a Core Graphics capture call for an unbounded time, which
    previously exhausted the entire handshake budget with no error at all
    (`RemoteTargetUnavailable: no handshake ... within 30.0s`). Screen
    Recording status has a purpose-built, instant, non-invasive query -
    `CGPreflightScreenCaptureAccess` (already used by `macos.py`'s own
    `_capture_none_error` for exactly this reason) - so this asks that
    question directly instead of inferring it from a real capture's side
    effects. `None` (older macOS/pyobjc without the symbol) is a genuine
    "could not determine" - the key is left out of `permissions` rather
    than guessed; `wire.validate_handshake` already treats a missing
    permission key as not-granted (fail closed), which is the only safe
    default here.
    """
    permissions: dict[str, bool] = {}
    if backend.name != "macos":
        return permissions
    try:
        from . import macos as _macos  # local import: only exists/imports on Darwin

        permissions["accessibility"] = bool(_macos._ax_is_process_trusted())
    except Exception:
        logger.exception("permission probe: accessibility check failed")
        permissions["accessibility"] = False
    try:
        from . import macos as _macos  # local import: only exists/imports on Darwin

        granted = _macos._cg_preflight_screen_capture_access()
        if granted is not None:
            permissions["screen_recording"] = granted
        # else: could not determine (no CGPreflightScreenCaptureAccess symbol on
        # this OS/pyobjc) - omit rather than guess; see docstring above.
    except Exception:
        logger.exception("permission probe: screen_recording check failed")
        permissions["screen_recording"] = False
    return permissions


# Compatibility import for callers that used the old name. The age is now a
# minimum retention floor; it is never interpreted as liveness or idleness.
STALE_AGENT_DIR_MAX_AGE_SECONDS = STALE_AGENT_DIR_MIN_AGE_SECONDS


class UnsupportedOpError(BackendError):
    """This backend/platform combination does not implement `op` in Phase 1."""


class RemoteAgent:
    """Owns the ledger, the backend, and op dispatch. `run()` is the NDJSON
    read/execute/respond loop; everything else is a release trigger or an op
    handler."""

    def __init__(
        self,
        backend: Backend,
        *,
        deadman_seconds: float = DEFAULT_DEADMAN_SECONDS,
        read_only: bool = True,
    ) -> None:
        self.backend = backend
        self.read_only = read_only
        self._ledger = HeldInputLedger(
            deadman_seconds=deadman_seconds, on_release=self._on_release
        )
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # Mutable coordinate box per held mouse button token (\u00a710.2) - see
        # `_op_mouse_down`/`_op_mouse_up`. Lets an explicit `mouse_up` hand its
        # real (x, y) to the SAME release_fn the ledger would otherwise call
        # with synthetic (None, None) coordinates on a link-death release,
        # without ever invoking `backend.mouse_up` twice for one press.
        self._mouse_pending: dict[str, dict[str, int | None]] = {}
        # Coexistence announcement (docs/designs/coexistence.md \u00a77, \u00a710.3):
        # the persistent overlay's own object (Linux/macOS has none - its
        # dialog is one-shot and blocking, see `_op_announce_raise`), and the
        # latched pause/cancel flags a click on that overlay sets. Latched,
        # never cleared here - the controller decides how many times it has
        # already acted on a given transition (see `announcement_status`'s
        # docstring and `__init__.py`'s `_sync_remote_announcement_state`).
        self._overlay: LinuxOverlay | WindowsOverlay | None = None
        self._exclusion = ExclusionZone()
        self._announce_paused = False
        self._announce_cancelled = False

    def _on_release(self, kind: str, token: str) -> None:
        # \u00a710.2/\u00a73.3: this exact line, `RELEASED:<token>`, is the
        # already-verified proof that release actually fired - printed to
        # stderr (never the protocol channel) so it survives even if stdout
        # is already gone by the time release happens.
        print(f"RELEASED:{token}", file=sys.stderr, flush=True)

    # -- coexistence announcement channel (docs/designs/coexistence.md \u00a77) ---
    #
    # Every existing announcement module (`announce_macos.py`,
    # `overlay_linux.py`, `overlay_windows.py`) was built assuming it runs in
    # the SAME process that owns the injection call site (each module's own
    # docstring says so) - which, for a remote session, is HERE, never the
    # controller (`__init__.py` imports none of them for a `RemoteBackend`).
    # These three methods are that missing target-side half.

    def _mark_paused(self) -> None:
        # Same stderr-proof-line discipline as `_on_release` above - a
        # human's own click is exactly the kind of fact that must survive
        # even if this process is torn down moments later.
        self._announce_paused = True
        print("ANNOUNCE:paused", file=sys.stderr, flush=True)

    def _mark_cancelled(self) -> None:
        self._announce_cancelled = True
        print("ANNOUNCE:cancelled", file=sys.stderr, flush=True)

    def _overlay_status_payload(self) -> dict[str, Any]:
        assert self._overlay is not None
        return {
            "shown": True,
            "buttons": {
                b.name: [b.rect.x1, b.rect.y1, b.rect.x2, b.rect.y2]
                for b in self._overlay.buttons
            },
        }

    def _op_announce_raise(self, args: dict[str, Any]) -> dict[str, Any]:
        """Raise THIS target's own session-start disclosure channel - the
        macOS announce-and-acknowledge dialog, or the persistent Linux/
        Windows overlay (docs/designs/coexistence.md \u00a77.3). The controller
        (`__init__.py`'s `_build_remote_announcement`) applies the exact
        same \u00a77.3/\u00a77.6 policy to this method's result that it already
        applies to a LOCAL `announce_macos.announce()`/overlay call - only
        WHERE the channel physically runs differs.

        Idempotent for the persistent-overlay case: a lost response must
        never cause a second overlay to be raised on top of the first, so
        an already-shown overlay is reported as-is rather than re-shown.
        The macOS dialog has no such state to remember - each call is a
        fresh, one-shot, blocking prompt, matching `announce_macos.announce`
        itself.
        """
        if self.backend.name == "macos":
            message = str(args["message"])
            timeout_seconds = int(
                args.get("timeout_seconds", announce_macos.DEFAULT_TIMEOUT_SECONDS)
            )
            try:
                result = announce_macos.announce(
                    message, timeout_seconds=timeout_seconds
                )
            except announce_macos.AnnounceError as exc:
                raise BackendError(f"macOS announce dialog failed: {exc}") from exc
            return {"button": result.button, "gave_up": result.gave_up}

        if self._overlay is not None and self._overlay.shown:
            return self._overlay_status_payload()

        screen_width = int(args["screen_width"])
        screen_x = int(args.get("screen_x", 0))
        screen_y = int(args.get("screen_y", 0))
        if self.backend.name.startswith("linux"):
            display = getattr(self.backend, "_display", None)
            if display is None:
                raise BackendError(
                    "linux-x11 backend has no live display connection to "
                    "attach an announcement overlay to"
                )
            overlay: LinuxOverlay | WindowsOverlay = LinuxOverlay(
                display,
                screen_width=screen_width,
                screen_x=screen_x,
                screen_y=screen_y,
                exclusion=self._exclusion,
                on_pause=self._mark_paused,
                on_cancel=self._mark_cancelled,
            )
        elif self.backend.name == "windows-wsl2":
            overlay = WindowsOverlay(
                screen_width=screen_width,
                screen_x=screen_x,
                screen_y=screen_y,
                exclusion=self._exclusion,
                on_pause=self._mark_paused,
                on_cancel=self._mark_cancelled,
            )
        else:
            raise UnsupportedOpError(
                f"'announce_raise' has no persistent-overlay implementation "
                f"for backend {self.backend.name!r}"
            )
        try:
            overlay.show()
        except Exception as exc:  # noqa: BLE001 - surfaced to the controller as a channel failure
            raise BackendError(
                f"failed to raise remote announcement overlay: {exc}"
            ) from exc
        self._overlay = overlay
        return self._overlay_status_payload()

    def _op_announcement_status(self, _args: dict[str, Any]) -> dict[str, Any]:
        """Has a human clicked Pause/Cancel on `self._overlay` since this
        agent started? Latched, level-triggered state - it is the
        CONTROLLER's job (`__init__.py`'s `_sync_remote_announcement_state`)
        to track which transitions it has already acted on, not this
        agent's, so a slow or missed poll never loses the fact.
        """
        return {"paused": self._announce_paused, "cancelled": self._announce_cancelled}

    def _teardown_overlay(self) -> None:
        """Best-effort teardown, called from every exit path this agent
        already has (`run()`'s `finally`, and every installed signal
        handler) - so an SSH link drop, a `bye`, or stdin EOF never strands
        a window on the human's own desktop (\u00a79.1/\u00a79.2). The SAME already-
        proven mechanism the held-input ledger uses for its own cleanup
        (`\u00a710.2`/`\u00a73.3`: stdin EOF -> this process's `finally` block runs) -
        not a new one. A no-op when no overlay was ever raised (macOS's
        dialog is one-shot and never sets `self._overlay` at all).
        """
        if self._overlay is None:
            return
        try:
            self._overlay.hide()
        except Exception:  # noqa: BLE001 - best-effort on every exit path
            logger.debug("teardown: overlay hide failed", exc_info=True)
        self._overlay = None

    # -- lifecycle ------------------------------------------------------------

    def install_signal_handlers(self) -> None:
        def _handler(signum: int, _frame: Any) -> None:
            logger.warning("agent: signal %s received - releasing and exiting", signum)
            self._ledger.stop()
            self._teardown_overlay()
            _sys.exit(0)

        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # pragma: no cover - platform-dependent
                pass

    def handshake(self) -> dict[str, Any]:
        probe = self.backend.probe()
        monitors = []
        try:
            monitors = [
                {
                    "id": m.id,
                    "x": m.x,
                    "y": m.y,
                    "width": m.width,
                    "height": m.height,
                    "primary": m.primary,
                }
                for m in self.backend.list_monitors()
            ]
        except BackendError as exc:
            logger.warning("handshake: list_monitors unavailable: %s", exc)
        capabilities = ["capture_scaled", "held_ledger", "audit", "read_only"]
        return {
            "protocol": PROTOCOL_VERSION,
            "agent_sha256": _content_hash(),
            "python": _sys.version.split()[0],
            "platform": _sys.platform,
            "backend": self.backend.name,
            "probe": {"available": probe.available, "reason": probe.reason},
            "capabilities": capabilities,
            "permissions": _probe_permissions(self.backend),
            "monitors": monitors,
            # M2 (docs/designs/capability-awareness.md \u00a72.1/\u00a75.3): this
            # agent's OWN dispatch table, generated - not prose. \u00a72.1 found a
            # prose capability table ("nine ops are not implemented over the
            # wire") that was wrong for 27 commits, in the direction that made
            # an agent UNDERSTATE what it could do to a real user. A
            # generated fact cannot go stale the way that table did - the
            # controller reads this to answer "can I do X on this binding?"
            # from the agent's own reality instead of from prose that has to
            # be kept in sync by hand.
            "ops": sorted(self._HANDLERS),
        }

    def run(self, stdin: Any, stdout: Any) -> None:
        """The NDJSON read loop. `stdin`/`stdout` are injected (not `sys.stdin`/
        `_PROTO`) so this is directly unit-testable against plain pipes with a
        fake backend - see `tests/test_remote_agent_ledger.py`."""
        handshake = self.handshake()
        stdout.write(json.dumps({"id": 0, "ok": True, "result": handshake}) + "\n")
        stdout.flush()
        try:
            for line in stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    req = Request(
                        id=int(data["id"]),
                        op=str(data["op"]),
                        args=data.get("args") or {},
                    )
                except Exception as exc:  # noqa: BLE001 - malformed line, not our bug
                    logger.warning("malformed request line: %s (%s)", line[:200], exc)
                    continue
                resp = self._dispatch(req)
                stdout.write(resp.encode().decode("utf-8"))
                stdout.flush()
                if req.op == "bye":
                    break
        finally:
            # \u00a710.2 primary path: stdin EOF (the loop above ends when the
            # iterator is exhausted, i.e. the pipe closed) triggers release
            # here, unconditionally, whether the loop ended via EOF, `bye`, or
            # an exception. `_teardown_overlay()` rides the SAME already-
            # proven guarantee, for the same reason (\u00a79.1/\u00a79.2): a link
            # drop that never runs another line of Python here would leave
            # both a held key AND a stranded overlay - one guarantee, one
            # call site, for both.
            self._ledger.stop()
            self._teardown_overlay()

    # -- dispatch ---------------------------------------------------------------

    def _dispatch(self, req: Request) -> Response:
        try:
            op_class = classify_op(req.op)
        except ValueError as exc:
            return Response(
                id=req.id, ok=False, error_type="UnknownOpError", error_message=str(exc)
            )
        if self.read_only and op_class == "write" and req.op != "release_all":
            return Response(
                id=req.id,
                ok=False,
                error_type="BackendError",
                error_message=f"op {req.op!r} blocked: agent enforces read_only "
                "(defence in depth - see docs/designs/remote-transport.md \u00a710.4)",
            )
        handler = self._HANDLERS.get(req.op)
        if handler is None:
            return Response(
                id=req.id,
                ok=False,
                error_type="UnsupportedOpError",
                error_message=f"op {req.op!r} not implemented in Phase 1",
            )
        try:
            result = handler(self, req.args)
            return Response(id=req.id, ok=True, result=result)
        except BackendError as exc:
            return Response(
                id=req.id,
                ok=False,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        except Exception as exc:
            logger.exception("op %s raised", req.op)
            return Response(
                id=req.id,
                ok=False,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

    # -- op handlers --------------------------------------------------------

    def _op_probe(self, _args: dict[str, Any]) -> dict[str, Any]:
        p = self.backend.probe()
        return {"available": p.available, "reason": p.reason}

    def _op_screen_geometry(self, _args: dict[str, Any]) -> dict[str, Any]:
        g = self.backend.screen_geometry()
        return {
            "width": g.width,
            "height": g.height,
            "origin_x": g.origin_x,
            "origin_y": g.origin_y,
        }

    def _op_list_monitors(self, _args: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "id": m.id,
                "x": m.x,
                "y": m.y,
                "width": m.width,
                "height": m.height,
                "primary": m.primary,
                "name": m.name,
            }
            for m in self.backend.list_monitors()
        ]

    def _op_cursor_position(self, _args: dict[str, Any]) -> dict[str, Any]:
        x, y = self.backend.cursor_position()
        return {"x": x, "y": y}

    def _op_presence_idle(self, _args: dict[str, Any]) -> dict[str, Any]:
        """Forward a presence-idle read to whichever real platform backend
        this agent is running (`docs/designs/coexistence.md` \u00a75) - the
        controller-side `RemoteBackend.presence_idle_ms()` calls this over
        the already-open NDJSON channel rather than duplicating any presence
        logic on the controller. `UnsupportedOpError` (not a guessed value)
        if this target's own backend has no `presence_idle_ms()` - never
        silently report a fabricated idle reading (\u00a79.6).
        """
        idle_source = getattr(self.backend, "presence_idle_ms", None)
        if idle_source is None:
            raise UnsupportedOpError(
                f"backend {self.backend.name!r} has no presence_idle_ms() - "
                "coexistence presence detection is not available on this target"
            )
        return {"idle_ms": float(idle_source())}

    def _op_capture_scaled(self, args: dict[str, Any]) -> dict[str, Any]:
        # C1: downscale HERE, on the target, before any bytes cross the wire -
        # never send the native capture and let the controller resize it.
        from PIL import Image  # lazy: only this op needs Pillow

        from .geometry import compute_display

        region = args.get("region")
        region_t = tuple(region) if region else None
        model_w = int(args.get("model_w", 1280))
        model_h = int(args.get("model_h", 800))
        max_edge = int(args.get("max_edge", 1280))
        max_pixels = int(args.get("max_pixels", 1_150_000))
        png = self.backend.capture(region=region_t)
        with Image.open(io.BytesIO(png)) as img:
            native_w, native_h = img.size
            img = img.convert("RGB")
            if region_t:
                tw, th = compute_display(img.width, img.height, max_edge, max_pixels)
                if (tw, th) != (img.width, img.height):
                    img = img.resize((tw, th), Image.Resampling.LANCZOS)
            else:
                img = img.resize((model_w, model_h), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
        return {
            "enc": "b64",
            "png": base64.standard_b64encode(buf.getvalue()).decode(),
            "w": img.width,
            "h": img.height,
            "native_w": native_w,
            "native_h": native_h,
            "scaled_on": "agent",
        }

    def _op_move(self, args: dict[str, Any]) -> None:
        self.backend.move(int(args["x"]), int(args["y"]))

    def _op_click(self, args: dict[str, Any]) -> None:
        self.backend.click(
            args.get("x"),
            args.get("y"),
            button=str(args.get("button", "left")),
            count=int(args.get("count", 1)),
        )

    def _op_type_text(self, args: dict[str, Any]) -> None:
        self.backend.type_text(str(args.get("text", "")))

    def _op_key(self, args: dict[str, Any]) -> None:
        self.backend.key(str(args["combo"]))

    def _op_mouse_down(self, args: dict[str, Any]) -> None:
        """Phase 2. Registered in the held-input ledger (\u00a710.2): Anthropic's
        action set exposes `left_mouse_down`/`left_mouse_up` independently, so
        the model can legitimately leave this half-open across two separate
        tool calls, and a link death between them must still guarantee
        release - the same guarantee `_op_hold`'s diagnostic modifier-hold
        already proves end to end, applied here to the real action surface.

        The release_fn reads its coordinates out of `self._mouse_pending[token]`
        at the moment it actually fires, rather than closing over `(x, y)`
        from THIS call - see `_op_mouse_up`, which mutates that box so an
        explicit matching `mouse_up` releases at the coordinates it was
        actually given, while a ledger-triggered release (deadman/EOF/signal,
        no explicit `mouse_up` ever arrived) still safely defaults to
        releasing at the current pointer position. This is what prevents
        `backend.mouse_up` from being invoked TWICE for one press - once
        directly here on the explicit path, once more via the release_fn.
        """
        x = args.get("x")
        y = args.get("y")
        button = str(args.get("button", "left"))
        self.backend.mouse_down(x, y, button)
        token = f"mouse:{button}"
        pending: dict[str, int | None] = {"x": None, "y": None}
        self._mouse_pending[token] = pending

        def _release() -> None:
            self.backend.mouse_up(pending["x"], pending["y"], button)
            self._mouse_pending.pop(token, None)

        self._ledger.hold("mouse", token, _release)

    def _op_mouse_up(self, args: dict[str, Any]) -> None:
        """If a matching `mouse_down` is still tracked in the ledger, release
        THROUGH the ledger (supplying this call's real coordinates via
        `_mouse_pending`) so `backend.mouse_up` is invoked exactly once and
        the ledger entry is correctly retired in the same step - a second,
        already-released `mouse_up`, or one with no matching `mouse_down` at
        all (e.g. `read_only` toggled mid-session), still performs the real
        action directly rather than silently dropping it."""
        x = args.get("x")
        y = args.get("y")
        button = str(args.get("button", "left"))
        token = f"mouse:{button}"
        pending = self._mouse_pending.get(token)
        if pending is not None:
            pending["x"], pending["y"] = x, y
            self._ledger.release(token)
        else:
            self.backend.mouse_up(x, y, button)

    def _op_drag(self, args: dict[str, Any]) -> None:
        """\u00a710.2: stays ONE call into `Backend.drag()` - never decomposed
        into mouse_down/move/mouse_up here, so a link failure mid-drag cannot
        strand a held button (the backend's own drag() is already atomic on
        all three platforms)."""
        start = args.get("start")
        end = args["end"]
        start_t = (int(start[0]), int(start[1])) if start else None
        self.backend.drag(start_t, (int(end[0]), int(end[1])))

    def _op_scroll(self, args: dict[str, Any]) -> None:
        self.backend.scroll(
            args.get("x"),
            args.get("y"),
            str(args["direction"]),
            int(args.get("amount", 1)),
        )

    def _op_hold_key(self, args: dict[str, Any]) -> None:
        """`Backend.hold_key` presses, sleeps for `duration`, and releases
        within one synchronous call on all three platforms - the same
        atomicity `drag` relies on, and why this is not additionally
        threaded through the ledger: there is no wire round trip during
        which a half-held key could be stranded by a link death."""
        self.backend.hold_key(str(args["combo"]), float(args.get("duration", 1.0)))

    def _op_list_windows(self, _args: dict[str, Any]) -> dict[str, Any]:
        result = self.backend.list_windows()
        return {
            "windows": [
                {
                    "handle": w.handle,
                    "title": w.title,
                    "minimized": w.minimized,
                    # list(...) - a tuple isn't valid JSON; `RemoteBackend.list_windows`
                    # converts it back to a tuple on receipt. `None` (no geometry for
                    # this window) survives the round trip unchanged.
                    "rect": list(w.rect) if w.rect is not None else None,
                }
                for w in result.windows
            ],
            "foreground": result.foreground,
        }

    def _op_focus_window(self, args: dict[str, Any]) -> None:
        self.backend.focus_window(str(args["handle"]))

    def _op_get_clipboard(self, _args: dict[str, Any]) -> dict[str, Any]:
        return {"text": self.backend.get_clipboard()}

    def _op_set_clipboard(self, args: dict[str, Any]) -> None:
        self.backend.set_clipboard(str(args.get("text", "")))

    def _op_hold(self, args: dict[str, Any]) -> dict[str, Any]:
        """Day-one ledger proof-of-mechanism (\u00a710.2): hold a single modifier
        key down, with NO matching up, tracked in the ledger until an explicit
        `release_all`, stdin EOF, a signal, or the deadman timer releases it.

        Not one of the ops Phase 2 adds to the wire surface (`hold_key`,
        `mouse_down`/`up`, `drag`) - this is the minimal, safest-possible
        primitive that actually exercises the guarantee end to end against a
        live desktop: a bare modifier key-down cannot click, type, or move
        anything, so it is safe to demonstrate on a machine a human is
        actively using. Matches the mechanic already proven in
        docs/designs/remote-transport.md \u00a73.3 (`{"op":"hold","held":["ctrl"]}`
        ... `RELEASED:ctrl`).
        """
        name = str(args["key"]).lower()
        release_fn = self._modifier_down(name)
        self._ledger.hold("key", name, release_fn)
        return {"held": self._ledger.held_tokens}

    def _op_release_all(self, _args: dict[str, Any]) -> dict[str, Any]:
        released = self._ledger.release_all(reason="release_all op")
        return {"released": released}

    def _op_bye(self, _args: dict[str, Any]) -> dict[str, Any]:
        return {"bye": True}

    _HANDLERS: ClassVar[dict[str, Callable[[RemoteAgent, dict[str, Any]], Any]]] = {
        "probe": _op_probe,
        "screen_geometry": _op_screen_geometry,
        "list_monitors": _op_list_monitors,
        "cursor_position": _op_cursor_position,
        "capture_scaled": _op_capture_scaled,
        "move": _op_move,
        "click": _op_click,
        "mouse_down": _op_mouse_down,
        "mouse_up": _op_mouse_up,
        "drag": _op_drag,
        "scroll": _op_scroll,
        "type_text": _op_type_text,
        "key": _op_key,
        "hold_key": _op_hold_key,
        "list_windows": _op_list_windows,
        "focus_window": _op_focus_window,
        "get_clipboard": _op_get_clipboard,
        "set_clipboard": _op_set_clipboard,
        "hold": _op_hold,
        # Coexistence presence read (docs/designs/coexistence.md §5). Missing
        # this entry is why a real SSH session got
        # `UnsupportedOpError: op 'presence_idle' not implemented in Phase 1`
        # on every remote presence read: `_op_presence_idle` was fully
        # implemented above and `wire.py` classified it READ, but `_dispatch`
        # looks ops up HERE, so the handler was unreachable. Every unit test
        # passed because they call `_op_presence_idle` directly or mock
        # `_call` - none exercised this table. The consequence on real
        # hardware was not a silent gap but a hard crash: the guard WAS
        # constructed (platform=windows-wsl2, guard_ms=20.0) and then raised
        # IdleUnreadableError on its first sample.
        "presence_idle": _op_presence_idle,
        # Coexistence announcement channel (docs/designs/coexistence.md
        # \u00a77, \u00a710.3) - see `_op_announce_raise`/`_op_announcement_status`'s
        # own docstrings for why this target-side half was missing.
        "announce_raise": _op_announce_raise,
        "announcement_status": _op_announcement_status,
        "release_all": _op_release_all,
        "bye": _op_bye,
    }

    # -- modifier hold/release primitives (macOS + Linux X11 only) ----------
    #
    # Deliberately NOT routed through `Backend` - see `_op_hold`'s docstring.
    # This is the one place remote_agent.py steps outside "one implementation,
    # two deployment shapes", and only for a diagnostic/safety-proof op that
    # is not part of the shipped action surface Claude ever calls (that
    # surface goes through `Backend.key`/`hold_key`, unchanged).

    def _modifier_down(self, name: str) -> Callable[[], None]:
        if self.backend.name == "macos":
            return self._macos_modifier_down(name)
        if self.backend.name.startswith("linux"):
            return self._linux_modifier_down(name)
        raise UnsupportedOpError(
            f"'hold' is not implemented for backend {self.backend.name!r} in "
            "Phase 1 (macOS and Linux X11 only)"
        )

    def _macos_modifier_down(self, name: str) -> Callable[[], None]:
        from . import macos as _macos

        keycode = _MACOS_MODIFIER_KEYCODE.get(name)
        if keycode is None:
            raise BackendError(
                f"unknown modifier {name!r}; expected one of "
                f"{sorted(_MACOS_MODIFIER_KEYCODE)}"
            )
        Quartz = _macos.Quartz
        down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)

        def _release() -> None:
            up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

        return _release

    def _linux_modifier_down(self, name: str) -> Callable[[], None]:
        from Xlib import X
        from Xlib.ext import xtest

        from . import linux_x11 as _linux

        keysym_name = _LINUX_MODIFIER_KEYSYM.get(name)
        if keysym_name is None:
            raise BackendError(
                f"unknown modifier {name!r}; expected one of "
                f"{sorted(_LINUX_MODIFIER_KEYSYM)}"
            )
        display = getattr(self.backend, "_display")  # noqa: B009 - deliberate, same-package reach
        keysym = _linux._keysym_for_name(keysym_name)
        keycode = display.keysym_to_keycode(keysym)
        xtest.fake_input(display, X.KeyPress, keycode)
        display.sync()

        def _release() -> None:
            xtest.fake_input(display, X.KeyRelease, keycode)
            display.sync()

        return _release


#: Apple HIToolbox virtual keycodes for the four modifier keys, left-hand
#: variants (`kVK_*` constants) - public, stable, Apple-documented values, the
#: same class of constant `macos.py`'s own `_KEYCODE` table already hardcodes.
_MACOS_MODIFIER_KEYCODE = {
    "shift": 0x38,  # kVK_Shift
    "ctrl": 0x3B,  # kVK_Control
    "control": 0x3B,
    "alt": 0x3A,  # kVK_Option
    "option": 0x3A,
    "cmd": 0x37,  # kVK_Command
    "command": 0x37,
}

_LINUX_MODIFIER_KEYSYM = {
    "shift": "Shift_L",
    "ctrl": "Control_L",
    "control": "Control_L",
    "alt": "Alt_L",
    "cmd": "Super_L",
    "super": "Super_L",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Amplifier remote computer-use agent")
    parser.add_argument(
        "--deadman-seconds", type=float, default=DEFAULT_DEADMAN_SECONDS
    )
    parser.add_argument(
        "--read-only",
        type=str,
        default="true",
        help="'true' (default, safe) or 'false' - agent-side enforcement, "
        "defence in depth alongside the controller's own gate (\u00a710.4)",
    )
    args = parser.parse_args(argv)

    # Best-effort hygiene for whatever a hard-killed predecessor left behind -
    # see `sweep_stale_agent_dirs`'s docstring. Runs before backend selection
    # so it happens on every connection attempt, including ones where this
    # machine turns out to have no usable backend at all.
    sweep_stale_agent_dirs()

    try:
        backend = select_backend({})
    except NoBackendAvailable as exc:
        _PROTO.write(
            json.dumps(
                {
                    "id": 0,
                    "ok": True,
                    "result": {
                        "protocol": PROTOCOL_VERSION,
                        "agent_sha256": _content_hash(),
                        "python": _sys.version.split()[0],
                        "platform": _sys.platform,
                        "backend": "none",
                        "probe": {"available": False, "reason": str(exc)},
                        "capabilities": [],
                        "permissions": {},
                        "monitors": [],
                    },
                }
            )
            + "\n"
        )
        _PROTO.flush()
        logger.error("no backend available: %s", exc)
        return 1

    agent = RemoteAgent(
        backend,
        deadman_seconds=args.deadman_seconds,
        read_only=str(args.read_only).lower() != "false",
    )
    agent.install_signal_handlers()
    try:
        agent.run(sys.stdin, _PROTO)
    except Exception:  # noqa: BLE001 - top-level agent loop guard; must never
        # let an unexpected exception escape without releasing held input.
        logger.error("agent loop crashed:\n%s", traceback.format_exc())
        agent._ledger.stop()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
