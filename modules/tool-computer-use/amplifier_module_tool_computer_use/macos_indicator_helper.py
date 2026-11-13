"""Private, payload-free Mac activity panel process.

Import and --self-test are inert: AppKit is imported only after a valid start.
The controller launches this file with its *own* prepared Python interpreter,
not the login shell's Python. No compiler, listener, daemon or permission prompt.
Window metadata acknowledges our surface, never visible pixels or input release.
"""

from __future__ import annotations

import json
import math
import os
import re
import select
import signal
import sys
import threading
import time

VERSION = 1
MAX_FRAME = 512
HEARTBEAT_SECONDS = 0.5
WATCHDOG_SECONDS = 4.0
TERMINAL_DWELL_SECONDS = 3.0
STATES = frozenset(
    {"viewing", "controlling", "paused", "stopping", "released", "interrupted"}
)
TERMINAL = frozenset({"released", "interrupted"})
LABELS = {
    "viewing": "Amplifier viewing this Mac",
    "controlling": "Amplifier controlling this Mac",
    "paused": "Paused — viewing may continue",
    "stopping": "Stopping — please wait",
    "released": "Control released",
    "interrupted": "Connection lost — state unconfirmed",
}
REASONS = frozenset(
    {
        "ok",
        "unsupported_platform",
        "console_user_mismatch",
        "console_unavailable",
        "cocoa_unavailable",
        "gui_unavailable",
        "surface_unconfirmed",
        "protocol_error",
        "connection_lost",
        "helper_failed",
        "reply_timeout",
        "not_started",
        "closed",
    }
)


class ProtocolError(ValueError):
    """Closed schema violation; never include received bytes in an error."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("protocol_error")
        result[key] = value
    return result


def decode_frame(data: bytes) -> dict:
    if (
        not 1 < len(data) <= MAX_FRAME
        or not data.endswith(b"\n")
        or data.count(b"\n") != 1
    ):
        raise ProtocolError("protocol_error")
    try:
        value = json.loads(data.decode("ascii"), object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("protocol_error") from exc
    if not isinstance(value, dict):
        raise ProtocolError("protocol_error")
    return value


def encode_frame(value: dict) -> bytes:
    data = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_FRAME:
        raise ProtocolError("protocol_error")
    return data


def _correlation(row: dict) -> None:
    if (
        type(row.get("v")) is not int
        or row["v"] != VERSION
        or not isinstance(row.get("g"), str)
        or re.fullmatch("[0-9a-f]{32}", row["g"]) is None
        or type(row.get("s")) is not int
        or not 1 <= row["s"] <= 2**53
    ):
        raise ProtocolError("protocol_error")


def validate_command(row: dict) -> None:
    if set(row) != {"v", "g", "s", "op", "state"}:
        raise ProtocolError("protocol_error")
    _correlation(row)
    if not isinstance(row["op"], str):
        raise ProtocolError("protocol_error")
    if row["op"] in ("start", "update"):
        if not isinstance(row["state"], str) or row["state"] not in STATES:
            raise ProtocolError("protocol_error")
    elif row["op"] not in ("status", "close", "abort") or row["state"] is not None:
        raise ProtocolError("protocol_error")


def validate_reply(row: dict) -> None:
    if set(row) != {"v", "g", "s", "event", "state", "reason", "window", "loop"}:
        raise ProtocolError("protocol_error")
    _correlation(row)
    if (
        row["event"] not in ("ready", "ack", "terminal", "unavailable")
        or not isinstance(row["state"], str)
        or row["state"] not in STATES
        or not isinstance(row["reason"], str)
        or row["reason"] not in REASONS
        or type(row["window"]) is not bool
        or type(row["loop"]) is not int
        or not 0 <= row["loop"] <= 2**53
    ):
        raise ProtocolError("protocol_error")


class Protocol:
    """One generation, contiguous sequence, irreversible terminal state."""

    def __init__(self):
        self.generation = None
        self.sequence = 0
        self.state = "interrupted"
        self.closing = False

    def accept(self, row: dict) -> str:
        validate_command(row)
        op, state = row["op"], row["state"]
        if self.generation is None:
            if (
                row["s"] != 1
                or op != "start"
                or state not in ("viewing", "controlling")
            ):
                raise ProtocolError("protocol_error")
            self.generation = row["g"]
        elif (
            row["g"] != self.generation
            or row["s"] != self.sequence + 1
            or op == "start"
            or self.closing
        ):
            raise ProtocolError("protocol_error")
        if op == "update":
            if self.state in TERMINAL and state != self.state:
                raise ProtocolError("protocol_error")
            if self.state == "stopping" and state not in (
                "stopping",
                "released",
                "interrupted",
            ):
                raise ProtocolError("protocol_error")
        self.sequence = row["s"]
        if op in ("start", "update"):
            self.state = state
        elif op == "abort" or (op == "close" and self.state not in TERMINAL):
            self.state = "interrupted"
        if op in ("close", "abort"):
            self.closing = True
        return op

    def reply(self, event, reason="ok", window=False, loop=0):
        return {
            "v": VERSION,
            "g": self.generation,
            "s": self.sequence,
            "event": event,
            "state": self.state,
            "reason": reason,
            "window": window,
            "loop": loop,
        }


def console_reason() -> str:
    if sys.platform != "darwin":
        return "unsupported_platform"
    try:
        # No username, host identifiers or other users' metadata cross IPC.
        uid = os.stat("/dev/console").st_uid
        if uid == 0:
            return "console_unavailable"
        return "ok" if uid == os.geteuid() else "console_user_mismatch"
    except OSError:
        return "console_unavailable"


class PanelRuntime:
    """Main-loop coordinator, also exercised with an inert surface in CI."""

    def __init__(self, protocol, surface, emit, *, clock=time.monotonic):
        self.protocol, self.surface, self.emit = protocol, surface, emit
        self.clock = clock
        self.last_command = self.last_tick = self.applied_at = clock()
        self.parent_pid = os.getppid()
        self.loops = 0
        self.pending = "ready"
        self.reason = "ok"
        self.finished = False
        self.ever_confirmed = False
        self.removing_at = None
        self.interrupt_requested = False
        self.surface.apply(protocol.state)

    def receive(self, data):
        if self.pending is not None:
            raise ProtocolError("protocol_error")
        before = self.protocol.state
        op = self.protocol.accept(decode_frame(data))
        self.last_command = self.clock()
        if self.interrupt_requested or self.reason != "ok":
            self.interrupt()
        elif before != self.protocol.state:
            self.applied_at = self.clock()
            self.surface.apply(self.protocol.state)
        self.pending = "terminal" if op in ("close", "abort") else "ack"

    def request_interrupt(self):
        # Signal-safe Python bookkeeping only. Never reenter AppKit or mutate
        # Protocol.accept's in-progress transition from a Python signal handler.
        self.interrupt_requested = True

    def interrupt(self, reason="connection_lost"):
        # Lost connection overrides even a previously requested released label.
        if self.reason == "ok":
            self.reason = reason
            self.protocol.state = "interrupted"
            self.protocol.closing = True
            self.applied_at = self.clock()
            self.surface.apply("interrupted")
        elif self.protocol.state != "interrupted":
            self.protocol.state = "interrupted"
            self.protocol.closing = True
            self.surface.apply("interrupted")

    def tick(self):
        self.loops += 1
        self.last_tick = now = self.clock()
        if (
            self.interrupt_requested
            or now - self.last_command > WATCHDOG_SECONDS
            or os.getppid() != self.parent_pid
        ):
            self.interrupt()
        confirmed = False if self.removing_at is not None else self.surface.confirmed()
        if confirmed:
            self.ever_confirmed = True
        elif (
            not self.ever_confirmed
            and self.reason == "ok"
            and now - self.applied_at < 2.0
        ):
            return  # Allow bounded WindowServer registration before initial ready.
        if not confirmed and self.removing_at is None:
            self.interrupt("surface_unconfirmed")
        closing = self.protocol.closing
        if closing and now - self.applied_at >= TERMINAL_DWELL_SECONDS:
            if self.removing_at is None:
                self.removing_at = now
            absent = self.surface.remove()
            if not absent and now - self.removing_at < 0.5:
                return
            reason = self.reason if absent else "surface_unconfirmed"
            if reason != "ok":
                self.protocol.state = "interrupted"
                self.reason = reason
            if self.pending is not None:
                self.emit(self.protocol.reply("terminal", reason, False, self.loops))
                self.pending = None
            self.finished = True
            return
        if self.pending is not None and not closing:
            event = self.pending if self.reason == "ok" else "unavailable"
            self.emit(self.protocol.reply(event, self.reason, confirmed, self.loops))
            self.pending = None
        elif self.pending is not None and self.reason != "ok":
            # Report loss promptly; the native loop retains the interrupted badge.
            self.emit(
                self.protocol.reply("unavailable", self.reason, False, self.loops)
            )
            self.pending = None


def _create_surface(appkit, quartz):
    """Create only our fixed-size, click-through, never-key/main panel."""

    class ActivityPanel(appkit.NSPanel):
        def canBecomeKeyWindow(self):
            return False

        def canBecomeMainWindow(self):
            return False

    screen = appkit.NSScreen.mainScreen()
    if screen is None:
        raise RuntimeError("gui_unavailable")
    visible = screen.visibleFrame()
    values = (
        visible.origin.x,
        visible.origin.y,
        visible.size.width,
        visible.size.height,
    )
    if not all(math.isfinite(v) for v in values) or values[2] < 496 or values[3] < 80:
        raise RuntimeError("gui_unavailable")
    panel = ActivityPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        ((values[0] + 16, values[1] + values[3] - 80), (480, 64)),
        appkit.NSWindowStyleMaskBorderless | appkit.NSWindowStyleMaskNonactivatingPanel,
        appkit.NSBackingStoreBuffered,
        False,
    )
    panel.setReleasedWhenClosed_(False)
    panel.setLevel_(appkit.NSStatusWindowLevel)
    panel.setCollectionBehavior_(
        appkit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | appkit.NSWindowCollectionBehaviorStationary
        | appkit.NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    panel.setHidesOnDeactivate_(False)
    panel.setBecomesKeyOnlyIfNeeded_(True)
    panel.setIgnoresMouseEvents_(True)
    panel.setMovable_(False)
    panel.setHasShadow_(False)
    panel.setOpaque_(True)
    panel.setBackgroundColor_(
        appkit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1, 0.82, 0.25, 1)
    )
    label = appkit.NSTextField.labelWithString_("")
    label.setFrame_(((8, 8), (464, 48)))
    label.setFont_(appkit.NSFont.boldSystemFontOfSize_(17))
    label.setTextColor_(appkit.NSColor.blackColor())
    label.setAlignment_(appkit.NSTextAlignmentCenter)
    label.setMaximumNumberOfLines_(2)
    label.setSelectable_(False)
    panel.contentView().addSubview_(label)
    panel.orderFront_(None)  # No activate, makeKey, orderFrontRegardless or focus call.

    class Surface:
        window_id = None
        removed = False

        def apply(self, state):
            label.setStringValue_(LABELS[state])
            panel.displayIfNeeded()

        def _onscreen(self):
            if self.window_id is None:
                number = panel.windowNumber()
                if number <= 0:
                    return None
                self.window_id = number
            entries = quartz.CGWindowListCopyWindowInfo(
                quartz.kCGWindowListOptionOnScreenOnly
                | quartz.kCGWindowListExcludeDesktopElements,
                quartz.kCGNullWindowID,
            )
            if entries is None:
                return None
            # Inspect ONLY owner and window number, never titles or other payload.
            return any(
                entry.get(quartz.kCGWindowOwnerPID) == os.getpid()
                and entry.get(quartz.kCGWindowNumber) == self.window_id
                for entry in entries
            )

        def confirmed(self):
            app = appkit.NSApplication.sharedApplication()
            return bool(
                app.activationPolicy() == appkit.NSApplicationActivationPolicyAccessory
                and not app.isActive()
                and panel.isVisible()
                and not panel.canBecomeKeyWindow()
                and not panel.canBecomeMainWindow()
                and not panel.isKeyWindow()
                and not panel.isMainWindow()
                and panel.ignoresMouseEvents()
                and self._onscreen() is True
                and console_reason() == "ok"
            )

        def remove(self):
            if not self.removed:
                # Save the actual observed ID before close can invalidate it.
                self._onscreen()
                panel.orderOut_(None)
                panel.close()
                self.removed = True
            return self.window_id is not None and self._onscreen() is False

    return Surface()


def _stop_application(app, appkit):
    app.stop_(None)
    # Wake only this helper's AppKit event queue. This application-defined event
    # is not keyboard/mouse input and is never posted to the desktop.
    event = appkit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
        appkit.NSEventTypeApplicationDefined, (0, 0), 0, 0, 0, None, 0, 0, 0
    )
    app.postEvent_atStart_(event, False)


def _emit(row):
    # A <=512 byte frame fits PIPE_BUF; a full/closed pipe never blocks AppKit.
    data = encode_frame(row)
    try:
        if os.write(1, data) != len(data):
            os._exit(71)
    except OSError:
        os._exit(71)


def _read_start():
    buffer = b""
    deadline = time.monotonic() + WATCHDOG_SECONDS
    while time.monotonic() < deadline:
        ready, _, _ = select.select([0], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = os.read(0, MAX_FRAME + 1 - len(buffer))
        if not chunk:
            break
        buffer += chunk
        if len(buffer) > MAX_FRAME:
            break
        if buffer.endswith(b"\n"):
            row = decode_frame(buffer)
            protocol = Protocol()
            protocol.accept(row)
            return protocol
    raise ProtocolError("protocol_error")


def _native_main(protocol):
    reason = console_reason()
    if reason != "ok":
        _emit(protocol.reply("unavailable", reason))
        return 2
    try:
        import AppKit
        import Quartz
        from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer
    except Exception:
        _emit(protocol.reply("unavailable", "cocoa_unavailable"))
        return 2
    try:
        app = AppKit.NSApplication.sharedApplication()
        if not app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory):
            raise RuntimeError("gui_unavailable")
        surface = _create_surface(AppKit, Quartz)
        runtime = PanelRuntime(protocol, surface, _emit)
    except Exception:
        _emit(protocol.reply("unavailable", "gui_unavailable"))
        return 2
    buffer = b""
    stop = threading.Event()

    def watchdog():
        # Independent of main-loop/IPC locks. A stalled loop cannot stay healthy.
        while not stop.wait(0.25):
            if time.monotonic() - runtime.last_tick > WATCHDOG_SECONDS:
                os._exit(70)

    threading.Thread(
        target=watchdog, name="mac-indicator-watchdog", daemon=True
    ).start()

    def interrupted(_signum, _frame):
        runtime.request_interrupt()

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, interrupted)

    def tick(_timer):
        nonlocal buffer
        try:
            try:
                chunk = os.read(0, MAX_FRAME + 1 - len(buffer))
            except BlockingIOError:
                chunk = None
            if chunk == b"":
                runtime.interrupt()
            elif chunk:
                buffer += chunk
                if len(buffer) > MAX_FRAME:
                    raise ProtocolError("protocol_error")
                if b"\n" in buffer:
                    frame, buffer = buffer.split(b"\n", 1)
                    runtime.receive(frame + b"\n")
        except ProtocolError:
            runtime.interrupt("protocol_error")
            buffer = b""
        except Exception:
            runtime.interrupt("helper_failed")
        try:
            runtime.tick()
            if runtime.finished:
                _stop_application(app, AppKit)
        except Exception:
            # A broken native surface cannot keep the helper resident indefinitely.
            os._exit(72)

    timer = NSTimer.timerWithTimeInterval_repeats_block_(0.1, True, tick)
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
    try:
        app.run()
    finally:
        timer.invalidate()
        stop.set()
        surface.remove()
    return 0 if protocol.state == "released" and runtime.reason == "ok" else 2


def self_test():
    """Exercise the real framing/state machine, without AppKit or a display."""
    protocol = Protocol()
    for seq, op, state in (
        (1, "start", "viewing"),
        (2, "status", None),
        (3, "update", "controlling"),
        (4, "update", "stopping"),
        (5, "update", "released"),
        (6, "close", None),
    ):
        protocol.accept(
            decode_frame(
                encode_frame(
                    {"v": 1, "g": "a" * 32, "s": seq, "op": op, "state": state}
                )
            )
        )
        validate_reply(decode_frame(encode_frame(protocol.reply("ack"))))
    try:
        protocol.accept(
            {"v": 1, "g": "a" * 32, "s": 6, "op": "update", "state": "controlling"}
        )
    except ProtocolError:
        return
    raise AssertionError("stale terminal command accepted")


def main():
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        print('{"protocol_self_test":"pass","native_ui_exercised":false}')
        return 0
    if sys.argv[1:]:
        return 2
    os.set_blocking(0, False)
    os.set_blocking(1, False)
    try:
        return _native_main(_read_start())
    except Exception:
        # No arbitrary error text, paths or caller data in stdout/stderr.
        return 2


if __name__ == "__main__":
    sys.exit(main())
