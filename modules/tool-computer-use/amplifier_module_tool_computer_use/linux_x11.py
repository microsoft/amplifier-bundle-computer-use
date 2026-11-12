"""Local Linux desktop backend, via X11 (Xlib + XTEST).

In-process: every action below talks straight to the X server over a persistent
Xlib connection - no subprocess anywhere on the hot path. This is the opposite
constraint from `WindowsBackend`, which must cross the WSL2/Win32 boundary through
`powershell.exe` for every single action. That asymmetry is exactly what forced the
`Backend` protocol (see `backend.py`) to be shaped around *capabilities*, not around
either implementation's plumbing.

Screen capture uses `Display.get_image()` (an in-process X `GetImage` request)
rather than shelling out to ImageMagick's `import` - see `capture()`.

Clipboard is the one place this backend shells out (to `xclip`): implementing the
ICCCM/ CLIPBOARD selection-owner protocol correctly (answering `SelectionRequest`
from other clients, persisting after this process exits) is a well-solved problem
`xclip` already gets right; reimplementing it in-process would trade a battle-tested
dependency for a large amount of fragile protocol code with no capability upside.
Its absence is a normal, loud `BackendError` on the specific clipboard action, not a
capability the whole backend probe depends on (the `computer` tool works fine
without it; only `desktop.get_clipboard`/`set_clipboard` need it).
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# python-xlib is a Linux-only dependency (see pyproject.toml's
# `sys_platform == 'linux'` marker) - not installed on macOS/Windows, and now
# also not installed on a remote-transport target that only provisioned
# Pillow via `uv run --with pillow` (see ssh_transport.py). Importing it
# unconditionally at module level would crash `registry.py`'s
# `from .linux_x11 import LinuxX11Backend` on every non-Linux machine -
# exactly the trap this bundle's `macos.py` already guards against for
# pyobjc-framework-Quartz (see that module's own `_IMPORT_ERROR` handling and
# `backend.py`'s D1 docstring: a backend that cannot possibly work must never
# take down `mount()`, starting at *import*, not just at `probe()`).
_IMPORT_ERROR: str | None = None
try:
    from Xlib import XK as _XK
    from Xlib import X as _X
    from Xlib import display as _xlib_display
    from Xlib.ext import xtest as _xtest
    from Xlib.protocol import event as _xevent
except Exception as exc:  # noqa: BLE001 - any import failure -> unavailable, not fatal
    _XK = _X = _xlib_display = _xtest = _xevent = None  # type: ignore[assignment]
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
#: Typed `Any` (not the real Xlib module types) so every reference below
#: type-checks cleanly - the real, load-bearing `None` guard is `probe()`,
#: which refuses to make this backend available at all when the import failed.
XK: Any = _XK
X: Any = _X
xlib_display: Any = _xlib_display
xtest: Any = _xtest
xevent: Any = _xevent

from .backend import (
    BackendError,
    MonitorEnumerationUnavailable,
    MonitorInfo,
    ProbeResult,
    ScreenGeometry,
    WindowInfo,
    WindowList,
)

logger = logging.getLogger(__name__)

_BUTTON_NUMBERS = {"left": 1, "middle": 2, "right": 3}
_SCROLL_BUTTONS = {"up": 4, "down": 5, "left": 6, "right": 7}

#: xdotool-style aliases for the modifier and named keys people actually type in
#: combos (e.g. "ctrl+shift+s"). Anything not listed here is passed through as-is
#: to `Xlib.XK.string_to_keysym`, which already understands real X11 keysym names
#: ("Return", "F5", "Page_Down", single characters, ...).
_MODIFIER_ALIASES = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "alt": "Alt_L",
    "option": "Alt_L",
    "shift": "Shift_L",
    "super": "Super_L",
    "cmd": "Super_L",
    "win": "Super_L",
    "windows": "Super_L",
    "meta": "Super_L",
}
_KEY_ALIASES = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "home": "Home",
    "end": "End",
    "pageup": "Page_Up",
    "page_up": "Page_Up",
    "pagedown": "Page_Down",
    "page_down": "Page_Down",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "insert": "Insert",
}


#: Timeout for the `systemctl --user show-environment` subprocess `probe()`
#: shells out to below - generous for a purely local D-Bus round trip, tight
#: enough that a hung/misbehaving systemd user manager cannot stall mount().
_SESSION_DISPLAY_CHECK_TIMEOUT_S = 3.0


def _reference_session_display(
    timeout_s: float = _SESSION_DISPLAY_CHECK_TIMEOUT_S,
) -> str | None:
    """Best-effort lookup of the `DISPLAY` value THIS USER'S OWN systemd
    `--user` manager has registered (`systemctl --user show-environment`) -
    the value the display/session manager itself exported at login,
    independent of whatever `DISPLAY` this specific process happens to have
    inherited right now.

    This is the ground truth `probe()` checks a blind
    `os.environ.get(\"DISPLAY\")` pickup against (see `__init__`'s
    `_display_explicit`). A real, healthy, XTEST-capable X server can still
    be the WRONG one: a stray `Xvfb` left running from an earlier
    `CONTRIBUTING.md` ship-gate session, with `DISPLAY` still exported in a
    shell nobody closed, satisfies every other check in this file
    identically to the user's real interactive desktop - and was observed
    doing exactly that (alive 6+ days, `ppid=1`) on a real machine. Capture,
    click, and type all "succeed" against the wrong display with zero
    errors, which is exactly the reported symptom: "it says it did it, no
    errors, but nothing happens on my desktop."

    An **active-monitor-count (RandR) check was tried and rejected**: a
    real, logged-in-but-headless desktop can legitimately report zero active
    monitors while a stray `Xvfb` reports one active (fake) monitor - RandR
    gets exactly backwards which one is "the user's session" on such a box.
    `systemctl --user show-environment` does not have that failure mode: it
    reports what the LOGIN session itself registered, not what any given X
    server's virtual output happens to claim.

    Returns `None` - "no reference available", not "mismatch" - whenever
    there is nothing to compare against: no systemd user manager reachable
    at all (`systemctl` missing, no per-user D-Bus session - a container or
    a bare CI box with no systemd `--user` instance), or a reachable one
    that has never exported a `DISPLAY` (a genuinely headless account with
    no GUI login ever). Both are the honest, legitimate no-user-session case
    this bundle explicitly still supports (CI, containers,
    `scripts/verify_coexistence.py`'s own deliberate `Xvfb` target) - not an
    error to paper over. Only a *known, differing* reference counts as a
    mismatch; the caller decides what to do with `None`.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # No systemd, no `systemctl` on PATH, no reachable user D-Bus, or it
        # hung - all the same "no reference available" case to this caller.
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("DISPLAY="):
            value = line[len("DISPLAY=") :].strip()
            return value or None
    return None


def _resolve_xauthority() -> str | None:
    """Find the Xauthority cookie file without assuming `~/.Xauthority`.

    python-xlib's own `Xauthority()` already reads the `XAUTHORITY` env var first
    (see `Xlib/xauth.py`), so if it is already set correctly - as it is under a
    gdm-managed session, `/run/user/<uid>/gdm/Xauthority`, which has no
    `~/.Xauthority` at all - this function changes nothing. It only fills the gap
    when `XAUTHORITY` is unset or points at a missing file, and even then it tries
    more than one plausible location rather than hardcoding a single path.
    """
    existing = os.environ.get("XAUTHORITY")
    if existing and Path(existing).exists():
        return existing
    uid = os.getuid()
    for candidate in (
        Path.home() / ".Xauthority",
        Path(f"/run/user/{uid}/gdm/Xauthority"),
        Path(f"/run/user/{uid}/.mutter-Xwaylandauth"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _keysym_for_name(name: str) -> int:
    keysym = XK.string_to_keysym(name)
    if keysym:
        return keysym
    if len(name) == 1:
        # Unicode private range XKB uses for keysyms with no named X11 constant -
        # the same trick `xdotool type` uses for characters outside Latin-1.
        return 0x01000000 + ord(name)
    raise BackendError(f"unknown key name {name!r}")


class LinuxX11Backend:
    """Executes computer-use actions against the local X11 desktop, in-process."""

    name = "linux-x11"
    type_text_guarded = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        explicit_display = cfg.get("display")
        #: True when the caller named a display ON PURPOSE (an explicit
        #: `config["display"]` - e.g. `scripts/verify_coexistence.py`'s own
        #: `LinuxX11Backend({"display": ...})`, which always sets this).
        #: False means `_display_name` fell back to a blind
        #: `os.environ.get("DISPLAY")` pickup - the path `registry.py`'s
        #: `select_backend()` takes for every real mount, and the one that
        #: let a stray `Xvfb`'s leaked `DISPLAY` env var silently become
        #: "the" desktop. `probe()` only runs the user-session match check
        #: below when this is False - a deliberate, explicit target is
        #: trusted as-is; that is the caller's call, not this backend's to
        #: second-guess.
        self._display_explicit: bool = bool(explicit_display)
        self._display_name: str | None = explicit_display or os.environ.get("DISPLAY")
        self._display: Any = None  # Xlib.display.Display, set once probe() succeeds
        self._root: Any = None
        # Cached result of `_check_discrete_input_available()` - see that method for
        # why this is a lazy, per-connection check rather than folded into `probe()`.
        self._discrete_input_blocked_reason: str | None = None
        self._discrete_input_checked = False

    # -- capability probe (D1) ---------------------------------------------------
    def probe(self) -> ProbeResult:
        """Connect and confirm XTEST is present. Cheap: one local socket connection,
        no subprocess, no network. Never raises - failure is reported, not thrown."""
        # Report a missing dependency AS a missing dependency. Without this the
        # guarded import above leaves the Xlib names bound to None, and the first
        # use downstream surfaces as "'NoneType' object has no attribute
        # 'Display'" - which reads like an X server connection fault and sends
        # whoever hits it looking at DISPLAY, XAUTHORITY, and xhost instead of at
        # `pip install python-xlib`. Observed for real in a live session mount.
        if _IMPORT_ERROR is not None:
            return ProbeResult(
                False,
                "python-xlib is not installed in the running interpreter "
                f"({_IMPORT_ERROR}); this backend cannot drive X11 without it",
            )
        if not self._display_name:
            return ProbeResult(False, "no DISPLAY set; no local X11 session to talk to")
        # User-session match check - only for a BLIND env pickup (see
        # `__init__`'s `_display_explicit`). A caller that named `display`
        # explicitly (e.g. `scripts/verify_coexistence.py`'s deliberate
        # `Xvfb` target) is trusted as-is and skips this entirely; this only
        # guards the path `registry.select_backend()` actually takes at a
        # real mount, where nothing but `os.environ.get("DISPLAY")` ever
        # named this display. See `_reference_session_display`'s docstring
        # for the defect this closes and the RandR approach it replaces.
        if not self._display_explicit:
            reference = _reference_session_display()
            if reference is not None and reference != self._display_name:
                return ProbeResult(
                    False,
                    f"DISPLAY={self._display_name!r} (picked up from this "
                    "process's environment) does not match the display "
                    f"this user's own login session has registered "
                    f"({reference!r}, from `systemctl --user "
                    "show-environment`); refusing to drive a desktop that "
                    "may not be the one actually being used interactively. "
                    "A leaked DISPLAY env var from an earlier Xvfb/test "
                    "session is the common cause - see CONTRIBUTING.md's "
                    "ship-gate instructions. Set tool config 'display' "
                    "explicitly if you intend to target this X server on "
                    "purpose.",
                )
        xauth = _resolve_xauthority()
        if xauth and not os.environ.get("XAUTHORITY"):
            os.environ["XAUTHORITY"] = xauth
        try:
            display = xlib_display.Display(self._display_name)
        except Exception as exc:  # noqa: BLE001 - any connection failure -> unavailable
            return ProbeResult(
                False, f"cannot connect to X server {self._display_name!r}: {exc}"
            )
        try:
            ext = display.query_extension("XTEST")
            if not ext or not ext.present:
                display.close()
                return ProbeResult(
                    False, "X server does not support the XTEST extension"
                )
        except Exception as exc:  # noqa: BLE001
            display.close()
            return ProbeResult(False, f"XTEST query failed: {exc}")
        # Keep the connection open for real use - this instance (not a fresh one) is
        # what the registry hands to ComputerTool/DesktopTool.
        self._display = display
        self._root = display.screen().root
        return ProbeResult(True)

    def _ensure_connected(self) -> None:
        if self._display is None:
            result = self.probe()
            if not result.available:
                raise BackendError(f"linux-x11 backend not available: {result.reason}")

    @property
    def display(self) -> Any:
        """The live Xlib `Display` connection this backend already holds open.

        The coexistence overlay (`overlay_linux.LinuxOverlay`,
        `docs/designs/coexistence.md` \u00a77.3) must create its window on the SAME
        connection this backend drives input through - not a second one -
        so its lifetime is tied to this backend's own connection (module
        docstring's "ghost-free by construction" property). `_ensure_connected()`
        guarantees this is non-`None` by the time a caller reaches here.
        """
        self._ensure_connected()
        return self._display

    def _check_discrete_input_available(self) -> None:
        """XTEST discrete input (buttons/keys) requires that no other client on
        this X session holds an exclusive core-protocol grab on the pointer or
        keyboard. When one does, XTEST still *generates* real, indistinguishable-
        from-hardware button/key events - `fake_input` never raises and the X
        server never returns a protocol error - but the exclusive grab consumes
        those events before normal window-hierarchy delivery happens: clicks land
        on no window, keystrokes are never seen by the focused app, and nothing
        in the observable API surface signals that anything went wrong. Confirmed
        on this backend's reference environment (a GNOME headless remote-desktop
        session): `XGrabPointer`/`XGrabKeyboard` on the root window both return
        `AlreadyGrabbed` continuously - independent of whether an RDP client is
        actually connected - while `move()`/`capture()` remain fully functional
        (pointer position is DIX-global state, not gated by this kind of grab).

        This is checked lazily (only when a discrete-input method is first
        called) rather than folded into `probe()`, because `probe()` gates
        *mounting the whole backend* and pointer motion / screen capture
        genuinely work regardless of this condition - failing the entire
        backend at mount time would discard real, working capability. Cached
        per-connection: this is an environmental condition, not per-action
        state, so re-probing on every click would double round trips for a
        result that will not change mid-session.
        """
        if self._discrete_input_checked:
            if self._discrete_input_blocked_reason:
                raise BackendError(self._discrete_input_blocked_reason)
            return
        self._discrete_input_checked = True
        root = self._root
        pointer_grab = root.grab_pointer(
            True,
            X.ButtonPressMask,
            X.GrabModeAsync,
            X.GrabModeAsync,
            X.NONE,
            X.NONE,
            X.CurrentTime,
        )
        if pointer_grab == 0:  # 0 == GrabSuccess: nothing was holding it - release ours
            self._display.ungrab_pointer(X.CurrentTime)
        keyboard_grab = root.grab_keyboard(
            True, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime
        )
        if keyboard_grab == 0:
            self._display.ungrab_keyboard(X.CurrentTime)
        self._display.sync()
        if pointer_grab != 0 or keyboard_grab != 0:
            self._discrete_input_blocked_reason = (
                "discrete input (click/key/type_text/scroll/drag) cannot reach "
                "application windows on this X11 session: the root window's "
                f"pointer and/or keyboard is already exclusively grabbed by "
                f"another client (XGrabPointer={pointer_grab}, "
                f"XGrabKeyboard={keyboard_grab}; 0 means available, nonzero means "
                "already held elsewhere). This is not a defect in this backend's "
                "use of XTEST - fake_input still generates real events, but the "
                "existing exclusive grab consumes them before window-hierarchy "
                "delivery. Commonly caused by a GNOME headless remote-desktop "
                "session (gnome-remote-desktop / mutter) holding an exclusive "
                "input grab for its virtual seat, independent of whether an RDP "
                "client is currently connected. move()/cursor_position()/capture() "
                "are unaffected by this condition and remain usable."
            )
            raise BackendError(self._discrete_input_blocked_reason)

    # -- Backend protocol: geometry + capture -------------------------------------
    def screen_geometry(self) -> ScreenGeometry:
        self._ensure_connected()
        geom = self._root.get_geometry()
        if geom.width <= 0 or geom.height <= 0:
            # The connection's screen size can be positive while the actual
            # capture/input root is 0x0 (reported on XWayland). It is not a safe
            # fallback: reject the root before scaling or issuing GetImage.
            raise BackendError(
                f"X11 root window has invalid geometry {geom.width}x{geom.height}; "
                "width and height must be positive. This can occur with XWayland "
                "on a Wayland desktop; native Wayland desktop control is not "
                "supported. Use a supported X11 desktop session or a supported "
                "remote target. Connection screen dimensions are not a fallback."
            )
        return ScreenGeometry(geom.width, geom.height, 0, 0)

    # -- coexistence: presence + target binding (docs/designs/coexistence.md) ----
    def presence_idle_ms(self) -> float:
        """Milliseconds since the last input event (real OR synthetic)
        reached this X server, via the `MIT-SCREEN-SAVER` extension's
        `idle` field - the `idle_source` the presence detector
        (`presence.PresenceMonitor`) reconciles against its own injection
        timestamps (\u00a75 of the coexistence design). Cheap: one in-process X
        round trip, no subprocess - safe to call once per elementary event.

        Not part of the `Backend` protocol (`backend.py`) - it is coexistence
        infrastructure specific to platforms with a proven `GUARD` band (\u00a75.5),
        looked up via `getattr` by whatever constructs the
        `coexistence_guard.CoexistenceGuard` for this backend.
        """
        self._ensure_connected()
        info = self._root.screensaver_query_info()
        return float(info.idle)

    def current_target(self) -> str | None:
        """The foreground window handle, for target binding (\u00a78.6) - `None`
        if this desktop's window manager does not expose `_NET_ACTIVE_WINDOW`
        (binding then reports `\"unverified\"` rather than silently pretending
        to enforce something it cannot check, per \u00a78.6)."""
        self._ensure_connected()
        try:
            active = self._root.get_full_property(self._atom("_NET_ACTIVE_WINDOW"), 0)
        except Exception:  # noqa: BLE001 - a read failure here must never crash
            return None
        if not active or not active.value:
            return None
        return str(int(active.value[0]))

    def list_monitors(self) -> list[MonitorInfo]:
        """Enumerate real monitors via RandR 1.5's `GetMonitors` request.

        RandR, not Xinerama: RandR 1.5 (2015) is the maintained multi-monitor
        extension on every X server this backend otherwise depends on (RandR
        itself is already required indirectly - `xrandr`/modesetting drivers are
        how modern Xorg does multi-monitor at all); Xinerama is a legacy
        compatibility shim RandR superseded and reports strictly less (no stable
        per-output name, no primary flag). python-xlib only attaches
        `xrandr_get_monitors` to Window objects when the server negotiates RandR
        >= 1.5 (see `Xlib.ext.randr.init()`) - on anything older the attribute
        genuinely does not exist. That is exactly the "cannot enumerate" case the
        `Backend.list_monitors` contract requires failing loudly for: this method
        never invents a single synthetic monitor to paper over it.
        """
        self._ensure_connected()
        get_monitors = getattr(self._root, "xrandr_get_monitors", None)
        if get_monitors is None:
            raise BackendError(
                "X server does not negotiate RandR >= 1.5 (no monitor "
                "enumeration available); cannot select a per-monitor target on "
                "this X11 session. Set tool config 'target_monitor: "
                "virtual-desktop' to opt into whole-desktop bounding-box mode "
                "instead."
            )
        try:
            reply = get_monitors(is_active=True)
        except Exception as exc:  # any protocol failure -> unavailable
            raise BackendError(f"RandR monitor enumeration failed: {exc}") from exc
        if not reply.monitors:
            connected = self._connected_output_count()
            if connected == 0:
                raise MonitorEnumerationUnavailable(
                    "RandR reported zero active monitors; cannot select a "
                    "per-monitor target on this X11 session (RandR also "
                    "reports zero CONNECTED outputs on this server - no "
                    "monitor is physically attached, so this is the "
                    "expected headless/virtual-display state, not a "
                    "failure)",
                    expected=True,
                )
            if connected is None:
                raise MonitorEnumerationUnavailable(
                    "RandR reported zero active monitors; cannot select a "
                    "per-monitor target on this X11 session (could not "
                    "determine this server's connected-output count, so "
                    "this is treated conservatively as NOT the expected "
                    "headless case)"
                )
            raise MonitorEnumerationUnavailable(
                f"RandR reported zero active monitors despite {connected} "
                "CONNECTED output(s) on this server - a monitor appears to "
                "be physically attached yet RandR's monitor-object "
                "enumeration is not reporting it; this is a genuine "
                "anomaly, not the expected headless case, and the "
                "whole-desktop fallback may not be trustworthy here"
            )
        monitors: list[MonitorInfo] = []
        for i, m in enumerate(reply.monitors):
            name = ""
            if m.name:
                try:
                    name = self._display.get_atom_name(m.name)
                except Exception:  # noqa: BLE001 - best-effort; id still unique below
                    name = ""
            monitor_id = name or f"monitor-{i}"
            monitors.append(
                MonitorInfo(
                    id=monitor_id,
                    x=int(m.x),
                    y=int(m.y),
                    width=int(m.width_in_pixels),
                    height=int(m.height_in_pixels),
                    primary=bool(m.primary),
                    name=name,
                )
            )
        return monitors

    def _connected_output_count(self) -> int | None:
        """How many RandR outputs this X server reports as `Connected` -
        NOT the same as `len(GetScreenResources().outputs)` (every output the
        driver knows about, connected or not: a real GPU commonly reports an
        output - e.g. `HDMI-0`/`USB-C-0`..`USB-C-3` - for every physical
        connector on the card, whether or not anything is plugged into it).

        This is the discriminator `list_monitors()` uses, on the "RandR
        reports zero active monitors" branch, to tell "genuinely no monitor
        attached" apart from "a monitor IS attached and RandR's monitor-
        object enumeration is simply broken". Verified live on the machine
        that motivated this: a real GDM session reported 5 RandR outputs -
        ALL `Disconnected` - alongside `GetMonitors` returning zero. That
        is genuinely the headless case (the GPU has physical connectors;
        nothing is plugged into any of them), not an anomaly, even though
        raw output count is nonzero.

        Returns `None` (never raises) if this probe itself cannot complete
        - e.g. `GetScreenResources`/`GetOutputInfo` fail, or this RandR
        version predates them. `None` is treated by the caller the same as
        "a monitor IS connected" (stay loud) - the honest, conservative
        default for when discrimination itself is unavailable; this probe
        must never let its OWN failure be silently read as "benign".
        """
        try:
            resources = self._root.xrandr_get_screen_resources()
            connected = 0
            for output in resources.outputs:
                info = self._display.xrandr_get_output_info(
                    output, resources.config_timestamp
                )
                # RandR `Connection` enum: 0=Connected, 1=Disconnected,
                # 2=UnknownConnection. Only 0 counts - "Unknown" is not a
                # proof of "no monitor attached".
                if info.connection == 0:
                    connected += 1
            return connected
        except Exception:  # noqa: BLE001 - a failed probe must never mask
            # the real "zero active monitors" error, and must never be
            # misread as proof of the benign case.
            return None

    def capture(self, region: tuple[int, int, int, int] | None = None) -> bytes:
        """In-process `GetImage` - no `import`/ImageMagick subprocess."""
        # Recheck even a regional capture: the root may have become unusable
        # since mount-time resolution (e.g. desktop/session reconfiguration).
        geom = self.screen_geometry()
        from PIL import Image

        if region:
            x1, y1, x2, y2 = region
            x, y, w, h = x1, y1, max(1, x2 - x1), max(1, y2 - y1)
        else:
            x, y, w, h = 0, 0, geom.width, geom.height
        raw = self._root.get_image(x, y, w, h, X.ZPixmap, 0xFFFFFFFF)
        # Depth-24 TrueColor X servers pack pixels as B, G, R, padding - "BGRX" in
        # PIL's raw decoder table. Verified against this box: a solid-color region
        # round-trips through this path with the correct channel order.
        img = Image.frombuffer("RGB", (w, h), raw.data, "raw", "BGRX", 0, 1)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # -- pointer -------------------------------------------------------------------
    def cursor_position(self) -> tuple[int, int]:
        self._ensure_connected()
        pointer = self._root.query_pointer()
        return int(pointer.root_x), int(pointer.root_y)

    def move(self, x: int, y: int) -> None:
        self._ensure_connected()
        xtest.fake_input(self._display, X.MotionNotify, x=x, y=y)
        self._display.sync()

    def _button_event(self, event_type: int, button: int) -> None:
        xtest.fake_input(self._display, event_type, button)

    def click(
        self, x: int | None, y: int | None, button: str = "left", count: int = 1
    ) -> None:
        self._ensure_connected()
        self._check_discrete_input_available()
        num = _BUTTON_NUMBERS.get(button)
        if num is None:
            raise BackendError(f"unsupported click button {button!r}")
        if x is not None and y is not None:
            self.move(x, y)
        for i in range(max(1, count)):
            self._button_event(X.ButtonPress, num)
            self._button_event(X.ButtonRelease, num)
            self._display.sync()
            if i < count - 1:
                time.sleep(0.05)  # let the app register discrete clicks, not a drag

    def mouse_down(self, x: int | None, y: int | None, button: str = "left") -> None:
        self._ensure_connected()
        self._check_discrete_input_available()
        num = _BUTTON_NUMBERS.get(button)
        if num is None:
            raise BackendError(f"unsupported button {button!r}")
        if x is not None and y is not None:
            self.move(x, y)
        self._button_event(X.ButtonPress, num)
        self._display.sync()

    def mouse_up(self, x: int | None, y: int | None, button: str = "left") -> None:
        self._ensure_connected()
        self._check_discrete_input_available()
        num = _BUTTON_NUMBERS.get(button)
        if num is None:
            raise BackendError(f"unsupported button {button!r}")
        if x is not None and y is not None:
            self.move(x, y)
        self._button_event(X.ButtonRelease, num)
        self._display.sync()

    def drag(self, start: tuple[int, int] | None, end: tuple[int, int]) -> None:
        self._ensure_connected()
        self._check_discrete_input_available()
        if start is not None:
            self.move(*start)
        self.mouse_down(None, None, "left")
        time.sleep(0.05)
        self.move(*end)
        time.sleep(0.05)
        self.mouse_up(None, None, "left")

    def scroll(self, x: int | None, y: int | None, direction: str, amount: int) -> None:
        self._ensure_connected()
        self._check_discrete_input_available()
        num = _SCROLL_BUTTONS.get(direction)
        if num is None:
            raise BackendError(f"unsupported scroll direction {direction!r}")
        if x is not None and y is not None:
            self.move(x, y)
        for _ in range(max(1, amount)):
            self._button_event(X.ButtonPress, num)
            self._button_event(X.ButtonRelease, num)
        self._display.sync()

    # -- keyboard --------------------------------------------------------------
    def _map_scratch(self, keysym: int) -> int:
        """Dynamically bind `keysym` onto the highest keycode as a scratch slot -
        the same technique `xdotool type`/`key` use for characters or symbols with
        no keycode in the current keyboard layout."""
        scratch = self._display.display.info.max_keycode
        self._display.change_keyboard_mapping(
            scratch, [(keysym, keysym, keysym, keysym)]
        )
        self._display.sync()
        self._display.get_keyboard_mapping(scratch, 1)  # refresh cached keymap
        return scratch

    def _keycode_for_keysym(self, keysym: int) -> int:
        """Keycode for a keysym at level 0. For combos (`key`/`hold_key`): the caller
        already names its own modifiers (e.g. "ctrl+s"), so the base key is sent
        unshifted and does not need level detection."""
        keycode = self._display.keysym_to_keycode(keysym)
        return keycode or self._map_scratch(keysym)

    def _keycode_and_level(self, keysym: int) -> tuple[int, int]:
        """Keycode plus the shift level it lives at, for typing raw text where the
        caller names no modifiers of its own (e.g. an uppercase letter or `!`
        implicitly needs Shift synthesized alongside it)."""
        try:
            pairs = list(self._display.keysym_to_keycodes(keysym))
        except Exception:  # noqa: BLE001 - unknown keysym, fall through to scratch-map
            pairs = []
        if pairs:
            return min(pairs, key=lambda p: p[1])
        return self._map_scratch(keysym), 0

    def _parse_combo(self, combo: str) -> list[int]:
        parts = [p for p in combo.split("+") if p]
        if not parts:
            raise BackendError("empty key combo")
        keysyms = []
        for part in parts:
            lowered = part.lower()
            name = _MODIFIER_ALIASES.get(lowered) or _KEY_ALIASES.get(lowered) or part
            keysyms.append(_keysym_for_name(name))
        return keysyms

    def _press_combo(self, keysyms: list[int], hold_seconds: float = 0.0) -> None:
        keycodes = [self._keycode_for_keysym(ks) for ks in keysyms]
        for kc in keycodes:
            xtest.fake_input(self._display, X.KeyPress, kc)
        self._display.sync()
        if hold_seconds:
            time.sleep(hold_seconds)
        for kc in reversed(keycodes):
            xtest.fake_input(self._display, X.KeyRelease, kc)
        self._display.sync()

    def key(self, combo: str) -> None:
        self._ensure_connected()
        self._check_discrete_input_available()
        self._press_combo(self._parse_combo(combo))

    def hold_key(self, combo: str, duration: float) -> None:
        self._ensure_connected()
        self._check_discrete_input_available()
        self._press_combo(self._parse_combo(combo), hold_seconds=max(0.0, duration))

    def type_text(self, text: str, guard: Any = None) -> None:
        """Type literal text, one character at a time.

        `guard` is an optional `coexistence_guard.CoexistenceGuard`
        (\u00a75.2/\u00a78.6 of `docs/designs/coexistence.md`). When supplied, every
        keystroke is individually checked (halt/pause/target-binding, via
        `guard.before_event()`) and individually timestamped for presence
        detection (`guard.after_event()`) - this is the mechanism that lets a
        human keystroke landing MID-`type_text` be detected, rather than
        only between whole operations (\u00a75.2, proven by O5). Omitting `guard`
        (the default) reproduces the exact prior behavior byte-for-byte -
        every existing caller of this method is unaffected.
        """
        self._ensure_connected()
        self._check_discrete_input_available()
        shift_kc: int | None = None
        for ch in text:
            if guard is not None:
                guard.before_event()
            name = {"\n": "Return", "\t": "Tab"}.get(ch, ch)
            keysym = _keysym_for_name(name)
            keycode, level = self._keycode_and_level(keysym)
            need_shift = level == 1
            if need_shift and shift_kc is None:
                shift_kc = self._display.keysym_to_keycode(_keysym_for_name("Shift_L"))
            if need_shift and shift_kc:
                xtest.fake_input(self._display, X.KeyPress, shift_kc)
            xtest.fake_input(self._display, X.KeyPress, keycode)
            xtest.fake_input(self._display, X.KeyRelease, keycode)
            if need_shift and shift_kc:
                xtest.fake_input(self._display, X.KeyRelease, shift_kc)
            self._display.sync()
            if guard is not None:
                guard.after_event()
        self._display.sync()

    # -- windows (EWMH) ----------------------------------------------------------
    def _atom(self, name: str) -> int:
        return self._display.intern_atom(name)

    def list_windows(self) -> WindowList:
        self._ensure_connected()
        client_list = self._root.get_full_property(self._atom("_NET_CLIENT_LIST"), 0)
        if client_list is None:
            raise BackendError(
                "window manager does not expose _NET_CLIENT_LIST (EWMH); "
                "list_windows is unsupported on this desktop"
            )
        active = self._root.get_full_property(self._atom("_NET_ACTIVE_WINDOW"), 0)
        foreground = str(int(active.value[0])) if active and active.value else None

        name_atom = self._atom("_NET_WM_NAME")
        utf8_atom = self._atom("UTF8_STRING")
        state_atom = self._atom("_NET_WM_STATE")
        hidden_atom = self._atom("_NET_WM_STATE_HIDDEN")

        windows: list[WindowInfo] = []
        for wid in client_list.value:
            win = self._display.create_resource_object("window", wid)
            title = ""
            name_prop = win.get_full_property(name_atom, utf8_atom)
            if name_prop and name_prop.value:
                raw = name_prop.value
                title = (
                    raw.decode("utf-8", "replace")
                    if isinstance(raw, bytes)
                    else str(raw)
                )
            if not title:
                try:
                    title = win.get_wm_name() or ""
                except Exception:  # noqa: BLE001 - best-effort title fallback
                    title = ""
            minimized = False
            state_prop = win.get_full_property(state_atom, 0)
            if state_prop and state_prop.value:
                minimized = hidden_atom in list(state_prop.value)
            windows.append(
                WindowInfo(str(int(wid)), title, minimized, rect=self._window_rect(win))
            )
        return WindowList(windows, foreground)

    def _window_rect(self, win: Any) -> tuple[int, int, int, int] | None:
        """SCREEN-space `(left, top, right, bottom)` for `win`, or `None` if
        this X server could not answer either request.

        `get_geometry()` alone is not enough: its `x`/`y` are relative to
        `win`'s immediate parent (typically a window-manager reparenting
        frame, not the root window), so `translate_coords` against the root
        window is what actually resolves the window's origin into the same
        root/SCREEN space every `MonitorInfo` and every other coordinate in
        this backend already uses (matches `monitors.attribute_monitor`'s
        expected convention). Best-effort: a window can disappear between
        `_NET_CLIENT_LIST` being read and this call - any protocol error
        here is reported as "no geometry" (`None`), never a crash of the
        whole `list_windows()` call over one stale window.
        """
        try:
            geom = win.get_geometry()
            origin = win.translate_coords(self._root, 0, 0)
        except Exception:  # noqa: BLE001 - best-effort per-window geometry
            return None
        left, top = int(origin.x), int(origin.y)
        return left, top, left + int(geom.width), top + int(geom.height)

    def focus_window(self, handle: str) -> None:
        self._ensure_connected()
        try:
            wid = int(handle)
        except ValueError as exc:
            raise BackendError(f"invalid window handle {handle!r}") from exc
        win = self._display.create_resource_object("window", wid)
        ev = xevent.ClientMessage(
            window=win,
            client_type=self._atom("_NET_ACTIVE_WINDOW"),
            data=(32, [1, X.CurrentTime, 0, 0, 0]),
        )
        mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
        self._root.send_event(ev, event_mask=mask)
        self._display.flush()

    # -- clipboard (shells out to xclip - see module docstring) -------------------
    def get_clipboard(self) -> str:
        exe = shutil.which("xclip")
        if not exe:
            raise BackendError("xclip not found on PATH; required for clipboard access")
        proc = subprocess.run(
            [exe, "-selection", "clipboard", "-o"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            raise BackendError(
                f"xclip -o failed: {proc.stderr.strip() or proc.returncode}"
            )
        return proc.stdout

    def set_clipboard(self, text: str) -> None:
        exe = shutil.which("xclip")
        if not exe:
            raise BackendError("xclip not found on PATH; required for clipboard access")
        proc = subprocess.run(
            [exe, "-selection", "clipboard"],
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            raise BackendError(
                f"xclip set failed: {proc.stderr.strip() or proc.returncode}"
            )

    def close(self) -> None:
        if self._display is not None:
            try:
                self._display.close()
            except Exception:
                logger.debug(
                    "linux-x11: error closing display connection", exc_info=True
                )
            self._display = None
