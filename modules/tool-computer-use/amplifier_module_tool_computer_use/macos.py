"""Local macOS desktop backend, via Core Graphics (Quartz) + Accessibility (AX).

In-process, like `LinuxX11Backend`: every action below talks straight to WindowServer
through pyobjc's `Quartz` bindings around CoreGraphics's Display Services and Event
Services APIs. Capture normally uses Core Graphics in process; a deliberately narrow
single-active-display fallback may invoke Apple's `screencapture` utility after a native
capture returns `None` and fresh lock, topology, and permission checks succeed. The
remaining core action set (geometry, monitors, cursor, move/click/drag/scroll/key/type)
does not spawn a subprocess - the opposite constraint from `WindowsBackend`, which must cross the WSL2/Win32 boundary through
`powershell.exe` for every single action. macOS needs neither of those crossings: the
console session this process runs in *is* the GUI session (confirmed: this backend was
built and verified over SSH into the same user account that owns the desktop), so a
plain in-process Core Graphics connection reaches the real screen directly, the same
way `LinuxX11Backend`'s Xlib connection reaches the real X server directly.

Two normal capabilities shell out anyway, for the same reason `LinuxX11Backend` shells
out to `xclip` for clipboard: a well-solved, battle-tested surface already exists, and
reimplementing it in-process would trade a working dependency for fragile code with no
capability upside. The narrow capture recovery described above is a separate exceptional
case, not a general subprocess capture path.

* Clipboard -> `pbcopy`/`pbpaste`. Apple's own, always present, gets NSPasteboard's
  UTI/type negotiation right for free.
* `focus_window` -> `osascript` driving System Events. Raising a *specific* window (not
  just activating its owning app) through the public Accessibility API in-process means
  matching a `CGWindowID` to an `AXUIElement` - there is no public API that hands you
  that mapping directly, only a private `_AXUIElementGetWindow` call Apple could break
  or reject from notarization without notice. System Events' AppleScript dictionary
  already exposes "raise window N of process P" as a first-class, publicly documented
  operation; shelling out to it is the honest choice, not a shortcut.

Two coordinate-space traps this module cannot get wrong (see `_display_scale` and the
module-level docstring in `geometry.py` for the SCREEN/MODEL split this sits under):

1. **Retina backing scale.** `CGDisplayCreateImage` (capture) and `CGDisplayPixelsWide`/
   `CGDisplayPixelsHigh` (monitor dimensions) all report *physical* pixels - on a Retina
   display, 2x (sometimes more) the *logical points* that `CGDisplayBounds` (monitor
   origin) and every `CGEvent` mouse-location parameter use. This backend's canonical
   `Backend` "SCREEN space" is physical pixels (matching `capture()`'s own output and
   every other backend's convention), so every point in/out of a `CGEvent` is converted
   through `_display_scale()`, computed empirically per display as
   `CGDisplayPixelsWide(id) / CGDisplayBounds(id).size.width` - not assumed to be 2.0,
   since some external displays and 1x-configured Retina displays are not.
2. **Coordinate origin convention.** Quartz's "global display coordinate space" (what
   `CGDisplayBounds`, `CGEventCreateMouseEvent`, and `CGWarpMouseCursorPosition` all
   share) has its origin at the *top-left* of the main display, y increasing downward -
   the opposite of AppKit/Cocoa's `NSScreen.frame`, which is bottom-left-origin,
   y increasing upward. This module never imports AppKit/Cocoa and never touches an
   `NSScreen`; every coordinate here comes from a `CG*` call, so there is exactly one
   coordinate convention in play throughout, not two silently disagreeing ones.

Accessibility (input injection) is a distinct, separately-granted TCC permission from
Screen Recording (capture): a process can capture the screen perfectly while every
`CGEventPost` call it makes is silently swallowed - delivered to no window, producing no
exception, no error return, nothing. `_ensure_input_trusted()` probes this explicitly
(via `AXIsProcessTrusted`, over ctypes so the check works even if pyobjc is partially
broken) before the *first* discrete-input action each session, exactly the same lazy,
cached-per-instance shape as `LinuxX11Backend._check_discrete_input_available()` - for
the same reason: capture/geometry/monitors/cursor_position genuinely work regardless of
this permission, so gating the whole backend on it at `probe()` time would discard real,
working capability. See that method's docstring for the exact remediation text surfaced
to the caller.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .backend import (
    BackendError,
    MonitorInfo,
    ProbeResult,
    ScreenGeometry,
    WindowInfo,
    WindowList,
)

logger = logging.getLogger(__name__)

# pyobjc-framework-Quartz is a macOS-only dependency (see pyproject.toml's
# `sys_platform == 'darwin'` marker) - it is simply not installed on Linux/Windows.
# Importing it unconditionally at module top would crash `registry.py`'s
# `from .macos import MacOSBackend` on every non-Mac machine, exactly the trap this
# bundle's `python-xlib` dependency already risks for non-Linux hosts (see D1 in
# `backend.py`: a backend that cannot possibly work must never take down `mount()`,
# and that guarantee starts at *import*, not just at `probe()`). Caught here, once,
# and reported through `probe()` as an ordinary unavailability reason.
_IMPORT_ERROR: str | None = None
try:
    import Quartz as _quartz_module  # type: ignore[import-not-found]
except Exception as exc:  # noqa: BLE001 - any import failure -> unavailable, not fatal
    _quartz_module = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
#: Typed `Any` (not `ModuleType | None`) so every `Quartz.CG*`/`kCG*` reference below
#: type-checks cleanly - the real, load-bearing `None` guard is `probe()`, which
#: refuses to make this backend available at all when the import failed.
Quartz: Any = _quartz_module

_BUTTON_CONST = {
    "left": 0,  # kCGMouseButtonLeft
    "right": 1,  # kCGMouseButtonRight
    "middle": 2,  # kCGMouseButtonCenter
}

#: Apple's virtual keycode table (HIToolbox `Events.h`'s `kVK_*` constants). These are
#: physical-key identifiers, not characters - there is no public API to look one up by
#: name, so this table is the standard, widely-published mapping every mac automation
#: tool (pyautogui, robotgo, pynput, ...) hardcodes. US ANSI layout only: this covers
#: what a "key combo" (e.g. "cmd+s") needs - a literal keycode for a named key - AND,
#: since the 2026-08-04 fix (see `type_text`'s docstring), every printable character
#: `type_text` can resolve via `_char_to_keycode_and_flags` below.
_KEYCODE = {
    "a": 0x00,
    "s": 0x01,
    "d": 0x02,
    "f": 0x03,
    "h": 0x04,
    "g": 0x05,
    "z": 0x06,
    "x": 0x07,
    "c": 0x08,
    "v": 0x09,
    "b": 0x0B,
    "q": 0x0C,
    "w": 0x0D,
    "e": 0x0E,
    "r": 0x0F,
    "y": 0x10,
    "t": 0x11,
    "1": 0x12,
    "2": 0x13,
    "3": 0x14,
    "4": 0x15,
    "6": 0x16,
    "5": 0x17,
    "=": 0x18,
    "9": 0x19,
    "7": 0x1A,
    "-": 0x1B,
    "8": 0x1C,
    "0": 0x1D,
    "]": 0x1E,
    "o": 0x1F,
    "u": 0x20,
    "[": 0x21,
    "i": 0x22,
    "p": 0x23,
    "return": 0x24,
    "enter": 0x24,
    "l": 0x25,
    "j": 0x26,
    "'": 0x27,
    "k": 0x28,
    ";": 0x29,
    "\\": 0x2A,
    ",": 0x2B,
    "/": 0x2C,
    "n": 0x2D,
    "m": 0x2E,
    ".": 0x2F,
    "tab": 0x30,
    "space": 0x31,
    "`": 0x32,
    "backspace": 0x33,
    "delete": 0x33,
    "escape": 0x35,
    "esc": 0x35,
    "cmd": 0x37,
    "command": 0x37,
    "super": 0x37,
    "win": 0x37,
    "windows": 0x37,
    "meta": 0x37,
    "shift": 0x38,
    "capslock": 0x39,
    "alt": 0x3A,
    "option": 0x3A,
    "ctrl": 0x3B,
    "control": 0x3B,
    "f17": 0x40,
    "f18": 0x4F,
    "f19": 0x50,
    "f20": 0x5A,
    "f5": 0x60,
    "f6": 0x61,
    "f7": 0x62,
    "f3": 0x63,
    "f8": 0x64,
    "f9": 0x65,
    "f11": 0x67,
    "f13": 0x69,
    "f16": 0x6A,
    "f14": 0x6B,
    "f10": 0x6D,
    "f12": 0x6F,
    "f15": 0x71,
    "help": 0x72,
    "home": 0x73,
    "pageup": 0x74,
    "page_up": 0x74,
    "forwarddelete": 0x75,
    "del": 0x75,
    "f4": 0x76,
    "end": 0x77,
    "f2": 0x78,
    "pagedown": 0x79,
    "page_down": 0x79,
    "f1": 0x7A,
    "left": 0x7B,
    "right": 0x7C,
    "down": 0x7D,
    "up": 0x7E,
}

#: Combo tokens that name a modifier rather than a "real" key - these set an event
#: flag (`CGEventSetFlags`) instead of contributing the combo's actual keycode.
_MODIFIER_FLAG_NAMES = {
    "cmd",
    "command",
    "super",
    "win",
    "windows",
    "meta",
    "shift",
    "alt",
    "option",
    "ctrl",
    "control",
}


def _cg_preflight_screen_capture_access() -> bool | None:
    """Best-effort, cheap, PROMPT-FREE probe of the Screen Recording TCC grant,
    via ctypes against CoreGraphics directly - the same "independent of
    pyobjc version" reasoning as `_ax_is_process_trusted()`'s Accessibility
    probe just above.

    `CGPreflightScreenCaptureAccess` (macOS 10.15+) reports the grant WITHOUT
    capturing anything and WITHOUT prompting the user, unlike attempting a
    real capture and inferring the reason from its failure. Returns `None`
    (not a guess) when the symbol cannot be resolved at all - too old an OS,
    or a stripped/exotic CoreGraphics - so `capture()`'s error message can
    fall back to naming BOTH known causes honestly instead of asserting one
    this probe was not actually able to check.
    """
    try:
        lib_path = ctypes.util.find_library("CoreGraphics")
        if not lib_path:
            lib_path = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        lib = ctypes.cdll.LoadLibrary(lib_path)
        fn = lib.CGPreflightScreenCaptureAccess
        fn.restype = ctypes.c_bool
        return bool(fn())
    except (OSError, AttributeError):
        return None


#: Named once so `capture()`'s three branches below and any test asserting on
#: this text stay in sync with a single source of truth.
_CONCURRENT_AGENT_CAUSE = (
    "a SECOND concurrent computer-use agent process launched against this "
    "same machine, which revokes/corrupts this one's Screen Recording grant "
    "too (macOS denies the permission to a second concurrent process in the "
    "same launch chain, silently, with no exception on either side) - check "
    "for one with `ps aux | grep amplifier_cu_agent` before assuming a "
    "one-time permission problem"
)


def _ax_is_process_trusted() -> bool:
    """Query `AXIsProcessTrusted()` directly via ctypes - deliberately independent of
    pyobjc/Quartz. This is a single, argument-free C boolean function in
    ApplicationServices; loading it through ctypes means the Accessibility check still
    works even in a partially-broken pyobjc install, and adds zero package
    dependencies for a single yes/no probe.
    """
    lib_path = ctypes.util.find_library("ApplicationServices")
    if not lib_path:
        lib_path = (
            "/System/Library/Frameworks/ApplicationServices.framework/"
            "ApplicationServices"
        )
    lib = ctypes.cdll.LoadLibrary(lib_path)
    lib.AXIsProcessTrusted.restype = ctypes.c_bool
    return bool(lib.AXIsProcessTrusted())


#: A locked screen and a missing TCC grant look IDENTICAL from the outside on
#: this platform: `CGDisplayCreateImage` returns a real, plausible-looking
#: image of the lock screen (not `None`, not an error) when locked, and
#: `CGEventPost` silently drops every click/keystroke sent to a locked
#: session exactly the way it drops them when Accessibility is not granted.
#: A prior real incident sat on a WRONG diagnosis ("Accessibility TCC not
#: granted") for days because nothing checked which of the two it actually
#: was - see `_macos_session_state()` and its two call sites (`capture()`,
#: `_ensure_not_locked()`) for the fix.
def _macos_session_state() -> tuple[str, str]:
    """Determine whether THIS host's console session is locked, has no GUI
    session at all, or is normally usable - via `ioreg -n Root -d1 -a`, the
    one property source proven live against a real target (see the
    accompanying report): unlocked, this command's plist output contains
    NEITHER `CGSSessionScreenIsLocked` nor a true `IOConsoleLocked`, and
    `IOConsoleUsers` holds one entry for the logged-in console user; locked,
    `CGSSessionScreenIsLocked` appears in the top-level dict as `True` (it is
    a key that is PRESENT only while locked, not present-and-`False` while
    unlocked) and/or `IOConsoleLocked` reads `True`. Both are consulted - an
    honest disjunction of two independently-sourced signals, not one
    guessed proxy for the other.

    `Quartz`/PyObjC's own session-property APIs are NOT importable on the
    real target this was verified against - `ioreg` is - so this shells out
    rather than depend on a module this host does not have (the same
    reasoning `focus_window`'s `osascript` shell-out already documents for a
    different capability).

    Returns `(state, detail)`:
      state: `"locked"` | `"unlocked"` | `"no_gui_session"` | `"unknown"`
      detail: the raw evidence the conclusion is based on - always surfaced
              to the caller (see the `_*_error` builders below) so a human
              or agent can verify THIS call's reasoning, not just trust its
              one-word label. This is the whole point: the previous silent
              failure was not merely "wrong", it was UNVERIFIABLE from the
              tool's own output.

    Never guesses when it cannot tell: an `ioreg` exec/parse failure returns
    `"unknown"` with the reason, never silently defaulting to `"unlocked"`
    (which would let a locked screen straight through - the exact bug this
    function exists to close) or to `"locked"` (which would block a healthy
    desktop on a transient tooling hiccup).
    """
    exe = shutil.which("ioreg") or "/usr/sbin/ioreg"
    try:
        proc = subprocess.run(
            [exe, "-n", "Root", "-d1", "-a"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unknown", f"ioreg invocation failed: {exc}"
    if proc.returncode != 0:
        return "unknown", (
            f"ioreg -n Root -d1 -a exited {proc.returncode}: "
            f"{proc.stderr.strip()[:300]}"
        )
    try:
        data = plistlib.loads(proc.stdout.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - malformed plist -> unknown, not a guess
        return "unknown", f"could not parse ioreg -a plist output: {exc}"
    if not isinstance(data, dict):
        return "unknown", f"ioreg -a plist root was {type(data).__name__}, not a dict"

    screen_locked = data.get("CGSSessionScreenIsLocked")
    console_locked = data.get("IOConsoleLocked")
    if bool(screen_locked) or bool(console_locked):
        return "locked", (
            f"CGSSessionScreenIsLocked={screen_locked!r} "
            f"IOConsoleLocked={console_locked!r} (ioreg -n Root -d1 -a)"
        )
    console_users = data.get("IOConsoleUsers")
    if not (isinstance(console_users, list) and console_users):
        return "no_gui_session", (
            "IOConsoleUsers is empty/absent in ioreg -n Root -d1 -a - no "
            "user is logged into the console at all"
        )
    return "unlocked", (
        "no lock indicators present (CGSSessionScreenIsLocked absent, "
        "IOConsoleLocked=False) and a console user is logged in "
        "(ioreg -n Root -d1 -a)"
    )


def _session_state_error(state: str, detail: str, doing: str) -> str:
    """Compose the fail-loud message for a non-`\"unlocked\"` session state,
    shared by `capture()` and `_ensure_not_locked()` so a human/agent sees
    the exact same diagnostic vocabulary for both - the previous incident's
    root cause was precisely that the two silent-failure modes (capture,
    input) gave NO diagnostic at all, forcing a guess that landed on the
    wrong one. `doing` names the refused action in the caller's own words
    (e.g. `\"capture a screenshot\"`, `\"send input\"`).
    """
    if state == "locked":
        return (
            f"refusing to {doing}: this macOS session is LOCKED ({detail}). "
            "A locked screen and a missing permission grant (Screen "
            "Recording for capture, Accessibility for input) are "
            "INDISTINGUISHABLE from the outside otherwise - a screenshot of "
            "the lock screen is a real, plausible-looking image, and "
            "keystrokes/clicks sent to a locked session are silently "
            "discarded by macOS with no error. That ambiguity previously "
            'produced a wrong diagnosis ("Accessibility TCC not granted") '
            "that stood as fact for days; this check exists so it never has "
            "to be guessed again. Fix: unlock the screen (sign back in) on "
            "the target host, then retry."
        )
    if state == "no_gui_session":
        return (
            f"refusing to {doing}: no GUI session is available on this host "
            f"at all ({detail}) - this is NOT a lock; nobody is logged into "
            "the console. Fix: log in at the physical console (or via "
            "Screen Sharing) before retrying."
        )
    # "unknown"
    return (
        f"refusing to {doing}: could not determine whether this macOS "
        f"session is locked or has no GUI session at all ({detail}). "
        "Refusing rather than guessing - a locked screen returned as a real "
        "screenshot, or input silently sent to a locked session, is exactly "
        "the failure this check exists to prevent. Verify "
        "`ioreg -n Root -d1 -a` runs successfully on this host, then retry."
    )


#: `CGEventFlags` bit masks (from CoreGraphics's public `CGEventTypes.h`), hardcoded as
#: plain ints rather than read off `Quartz.kCGEventFlagMask*`. This is a deliberate,
#: pure-logic seam: `_combo_flags_and_keycode` below does real work (combo parsing,
#: alias resolution, error messages for typos) that deserves the same zero-Quartz unit
#: test coverage `geometry.py`'s pure math gets - and these four values are public,
#: stable Apple constants, not something this module is guessing at.
_CG_FLAG_COMMAND = 1 << 20  # kCGEventFlagMaskCommand
_CG_FLAG_SHIFT = 1 << 17  # kCGEventFlagMaskShift
_CG_FLAG_CONTROL = 1 << 18  # kCGEventFlagMaskControl
_CG_FLAG_ALTERNATE = 1 << 19  # kCGEventFlagMaskAlternate

_MODIFIER_FLAG_BY_NAME = {
    "cmd": _CG_FLAG_COMMAND,
    "command": _CG_FLAG_COMMAND,
    "super": _CG_FLAG_COMMAND,
    "win": _CG_FLAG_COMMAND,
    "windows": _CG_FLAG_COMMAND,
    "meta": _CG_FLAG_COMMAND,
    "shift": _CG_FLAG_SHIFT,
    "alt": _CG_FLAG_ALTERNATE,
    "option": _CG_FLAG_ALTERNATE,
    "ctrl": _CG_FLAG_CONTROL,
    "control": _CG_FLAG_CONTROL,
}


def _combo_flags_and_keycode(combo: str) -> tuple[int, int]:
    """Parse a combo string (e.g. `"cmd+shift+s"`) into `(CGEventFlags, keycode)`.

    Pure logic, no Quartz dependency - runs and is tested on any platform.
    """
    parts = [p for p in combo.split("+") if p]
    if not parts:
        raise BackendError("empty key combo")
    flags = 0
    keycode: int | None = None
    for part in parts:
        lowered = part.lower()
        if lowered in _MODIFIER_FLAG_NAMES:
            flags |= _MODIFIER_FLAG_BY_NAME[lowered]
            continue
        code = _KEYCODE.get(lowered)
        if code is None and len(part) == 1:
            code = _KEYCODE.get(part.lower())
        if code is None:
            raise BackendError(f"unknown key name {part!r} in combo {combo!r}")
        keycode = code
    if keycode is None:
        raise BackendError(f"combo {combo!r} names only modifiers, no real key")
    return flags, keycode


#: US ANSI shifted characters, mapped to the BASE key in `_KEYCODE` that,
#: combined with Shift, produces them - e.g. '!' is Shift+'1' on every US
#: keyboard. Used by `_char_to_keycode_and_flags` below, the same "real
#: keycode, not a synthesized Unicode string" reliability `key()` already
#: depends on, extended to cover `type_text`'s printable-character range.
_SHIFTED_CHAR_TO_BASE = {
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "-",
    "+": "=",
    "{": "[",
    "}": "]",
    "|": "\\",
    ":": ";",
    '"': "'",
    "<": ",",
    ">": ".",
    "?": "/",
    "~": "`",
}


def _char_to_keycode_and_flags(ch: str) -> tuple[int, int] | None:
    """Resolve one character to a real `(keycode, CGEventFlags)` pair on the
    US ANSI layout `_KEYCODE`/`_SHIFTED_CHAR_TO_BASE` already encode - or
    `None` if this character has no real keycode on that layout (accented
    letters, non-Latin scripts, symbols/emoji outside both tables).

    `type_text` (see its docstring for the real-hardware defect this fixes)
    uses this for every character instead of the previous keycode-0
    `CGEventKeyboardSetUnicodeString` technique, which posts successfully
    but was measured, live, to deliver nothing to at least Spotlight's
    search field. This function returns `None` rather than guessing so the
    caller can fail loud on anything it cannot resolve, instead of silently
    routing unresolvable characters through that unverified technique.
    """
    if ch in ("\n", "\r"):
        return _KEYCODE["return"], 0
    if ch == "\t":
        return _KEYCODE["tab"], 0
    if ch == " ":
        return _KEYCODE["space"], 0
    if ch.isascii() and ch.isalpha():
        code = _KEYCODE.get(ch.lower())
        if code is None:
            return None
        return code, (_CG_FLAG_SHIFT if ch.isupper() else 0)
    code = _KEYCODE.get(ch)
    if code is not None:
        return code, 0
    base = _SHIFTED_CHAR_TO_BASE.get(ch)
    if base is not None:
        return _KEYCODE[base], _CG_FLAG_SHIFT
    return None


#: A native CoreGraphics capture call that takes at least this long has not
#: "been slow", it has been PATHOLOGICAL, and it will be again. Measured on one
#: machine across an OS update: on macOS 26.6.2 `CGDisplayCreateImage` blocked
#: ~5.0s and then returned NULL, and `CGWindowListCreateImage` took a flat 30.0s
#: to return a CORRECT image; on 26.7 the same calls took 0.02-0.08s and 0.07s.
#: Any value between those populations works - this sits in the middle of a gap
#: three orders of magnitude wide, which is why it is a threshold and not a
#: guess about a version number.
_NATIVE_CAPTURE_SLOW_SECONDS = 2.0


class _Unset:
    """Sentinel type: distinguishes "no observed preflight supplied" from a real
    `None` preflight result, which is itself one of the three diagnoses."""


_UNSET = _Unset()


class MacOSBackend:
    """Executes computer-use actions against the local macOS desktop, in-process."""

    name = "macos"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._osascript_timeout = float(cfg.get("timeout", 15.0))
        self._input_trusted_checked = False
        self._input_blocked_reason: str | None = None
        #: Has a native CoreGraphics capture call already proven itself
        #: pathological in THIS process? See `_call_native`. Per instance and
        #: never persisted: the remote agent is one process per session, so an
        #: OS update takes effect on the next session with no cache to
        #: invalidate. That is not incidental - this flag exists because an OS
        #: update silently changed the answer underneath a running branch.
        self._native_capture_degraded = False

    # -- capability probe (D1) ---------------------------------------------------
    def probe(self) -> ProbeResult:
        """Cheap, in-process checks only. Never raises - failure is reported."""
        if sys.platform != "darwin":
            return ProbeResult(False, f"not macOS (sys.platform={sys.platform!r})")
        if Quartz is None:
            return ProbeResult(
                False,
                "pyobjc-framework-Quartz is not importable "
                f"({_IMPORT_ERROR}); install it to enable the macOS backend",
            )
        try:
            err, _displays, count = Quartz.CGGetActiveDisplayList(32, None, None)
        except Exception as exc:  # noqa: BLE001 - any Quartz call failure -> unavailable
            return ProbeResult(False, f"CGGetActiveDisplayList failed: {exc}")
        if err != 0:
            return ProbeResult(False, f"CGGetActiveDisplayList returned error {err}")
        if not count:
            return ProbeResult(
                False,
                "zero active displays (screen may be asleep, or this is a "
                "clamshell-closed Mac with no external display attached)",
            )
        return ProbeResult(True)

    # -- lazy Accessibility (input) gate ------------------------------------------
    def _ensure_input_trusted(self) -> None:
        """Gate discrete input (move/click/drag/scroll/key/type) on Accessibility TCC.

        Checked lazily, once per instance, exactly like `LinuxX11Backend`'s exclusive-
        grab check: `probe()` only has to prove capture/geometry work (they do, without
        this permission), so it must not fail the whole backend for an input-only
        restriction. Without this explicit check, an untrusted process's `CGEventPost`
        calls are silently swallowed - the cursor may even appear to move (warping the
        raw HID position is not always gated the same way clicks/keys are) while clicks
        and keystrokes reach no window and no exception is ever raised. That silent-
        no-op is exactly the failure mode this bundle must never ship, so it is
        converted here into a loud, actionable `BackendError` naming the exact fix.
        """
        if self._input_trusted_checked:
            if self._input_blocked_reason:
                raise BackendError(self._input_blocked_reason)
            return
        self._input_trusted_checked = True
        if not _ax_is_process_trusted():
            self._input_blocked_reason = (
                "Accessibility permission not granted: this process is not trusted "
                "to control the computer (AXIsProcessTrusted() == False). Mouse/"
                "keyboard input (move, click, drag, scroll, key, type) will be "
                "silently swallowed by WindowServer until this is granted - macOS "
                "raises no exception for this, which is exactly why this check "
                "exists. Fix: open System Settings -> Privacy & Security -> "
                "Accessibility, and enable the process actually running this code "
                "(e.g. Terminal, sshd-launched login shell, or the python "
                "interpreter binary itself - whichever the Privacy pane lists after "
                "the first attempted action). Screenshots, monitor listing, and "
                "cursor position are unaffected by this permission and remain usable."
            )
            raise BackendError(self._input_blocked_reason)

    # -- lazy lock-state gate (D1 companion to the Accessibility gate above) ------
    def _ensure_not_locked(self) -> None:
        """Refuse to send input to a locked session, or one with no GUI session
        at all - checked FRESH on every call, never cached.

        Unlike Accessibility TCC (stable for the life of a session, so
        `_ensure_input_trusted` checks it once and remembers), lock state changes
        constantly - a session unlocked at mount can lock seconds later - so a
        cached answer here would get the exact case this check exists for wrong.
        See `_macos_session_state()`'s module-level docstring for the real
        incident (a locked screen misdiagnosed as a missing TCC grant) this
        refusal exists to prevent.
        """
        state, detail = _macos_session_state()
        if state != "unlocked":
            raise BackendError(_session_state_error(state, detail, "send input"))

    def _ensure_ready_for_input(self) -> None:
        """The full discrete-input gate: not locked, AND Accessibility-trusted.

        Order matters and is deliberate: a locked session silently swallows
        `CGEventPost` calls exactly like a missing Accessibility grant does (see
        `_macos_session_state()`'s docstring) - checking lock state FIRST means a
        locked-but-otherwise-trusted process gets the correct diagnosis (\"the
        screen is locked\") instead of the previous incident's wrong one (\"TCC not
        granted\"), which is precisely the ambiguity this pass closes.
        """
        self._ensure_not_locked()
        self._ensure_input_trusted()

    # -- geometry helpers ----------------------------------------------------------
    def _active_display_ids(self) -> list[int]:
        err, displays, count = Quartz.CGGetActiveDisplayList(32, None, None)
        if err != 0:
            raise BackendError(f"CGGetActiveDisplayList returned error {err}")
        return [int(d) for d in (displays or [])[:count]]

    @staticmethod
    def _physical_pixel_size(display_id: int) -> tuple[int, int]:
        """True physical framebuffer resolution for one display.

        Deliberately NOT `CGDisplayPixelsWide`/`CGDisplayPixelsHigh`: verified live on
        the real Retina machine this backend was built against (macOS 26.6), those two
        calls return the *logical point* resolution - identical to `CGDisplayBounds`'s
        width/height, NOT the 2x physical pixel count. That is exactly the kind of
        silent, wrong-by-a-scale-factor bug this module's docstring warns about, caught
        here empirically before being trusted: `CGDisplayCopyDisplayMode` +
        `CGDisplayModeGetPixelWidth/Height` is the API that actually reports the
        framebuffer's physical pixel dimensions - confirmed to match
        `CGImageGetWidth/Height` on a real `CGDisplayCreateImage` capture (3456x2234 on
        the verification machine, vs. 1728x1117 from `CGDisplayPixelsWide`/
        `CGDisplayBounds` - exactly 2x, the display's real Retina backing scale).
        """
        mode = Quartz.CGDisplayCopyDisplayMode(display_id)
        if mode is None:
            raise BackendError(
                f"CGDisplayCopyDisplayMode({display_id}) returned no mode "
                "(display may have gone to sleep or been disconnected)"
            )
        return (
            int(Quartz.CGDisplayModeGetPixelWidth(mode)),
            int(Quartz.CGDisplayModeGetPixelHeight(mode)),
        )

    @classmethod
    def _display_scale(cls, display_id: int) -> float:
        """Physical pixels per logical point for one display, measured empirically -
        never assumed to be 2.0 (some external/1x-configured displays are 1.0, and
        Apple has shipped 3x-class devices in other product lines)."""
        bounds = Quartz.CGDisplayBounds(display_id)
        point_w = bounds.size.width
        pixel_w, _pixel_h = cls._physical_pixel_size(display_id)
        if not point_w:
            return 1.0
        return float(pixel_w) / float(point_w)

    def _monitor_infos(self) -> list[MonitorInfo]:
        """One `MonitorInfo` per active display, in physical-pixel SCREEN space.

        `x`/`y`/`width`/`height` are each computed using *that display's own* backing
        scale - self-consistent for coordinate math confined to a single monitor
        (exactly how `ComputerTool` uses `Display`: scoped to one monitor's own
        bounds). Known, documented limitation: on a genuinely mixed-DPI multi-monitor
        setup, stitching these into one shared virtual-desktop pixel space (only used
        in `monitors.VIRTUAL_DESKTOP` mode, not the default) is not perfectly exact,
        because there is no single physical-pixel unit that is simultaneously correct
        for two displays with different scale factors. Not exercised by the real
        verification for this backend, which ran against a single-display Mac.
        """
        display_ids = self._active_display_ids()
        if not display_ids:
            return []
        main_id = Quartz.CGMainDisplayID()
        infos: list[MonitorInfo] = []
        for display_id in display_ids:
            scale = self._display_scale(display_id)
            bounds = Quartz.CGDisplayBounds(display_id)
            x = round(bounds.origin.x * scale)
            y = round(bounds.origin.y * scale)
            w, h = self._physical_pixel_size(display_id)
            infos.append(
                MonitorInfo(
                    id=str(display_id),
                    x=x,
                    y=y,
                    width=w,
                    height=h,
                    primary=(display_id == main_id),
                    name="",  # no public API for a human-readable display name
                )
            )
        return infos

    def _covering_monitor_for_pixel(self, x: int, y: int) -> MonitorInfo:
        """Which monitor's physical-pixel bounds contain `(x, y)`.

        Falls back to the primary monitor (clamped, not raised) if none contains the
        point - defensive only: real callers always pass coordinates already clamped
        to one monitor's bounds by `geometry.Display.to_screen`, so this path is not
        expected to fire in normal operation.
        """
        monitors = self._monitor_infos()
        if not monitors:
            raise BackendError("no active displays; cannot resolve a screen position")
        for m in monitors:
            if m.x <= x < m.x + m.width and m.y <= y < m.y + m.height:
                return m
        primary = next((m for m in monitors if m.primary), monitors[0])
        logger.warning(
            "macos: pixel (%d, %d) is outside every enumerated monitor; "
            "falling back to primary monitor %r for coordinate conversion",
            x,
            y,
            primary.id,
        )
        return primary

    def _pixel_to_point(self, x: int, y: int) -> Any:
        """Physical-pixel SCREEN coordinate -> Quartz global point (for `CGEvent*`)."""
        m = self._covering_monitor_for_pixel(x, y)
        scale = self._display_scale(int(m.id))
        bounds = Quartz.CGDisplayBounds(int(m.id))
        local_px_x = x - m.x
        local_px_y = y - m.y
        point_x = bounds.origin.x + local_px_x / scale
        point_y = bounds.origin.y + local_px_y / scale
        return Quartz.CGPointMake(point_x, point_y)

    def _point_to_pixel(self, point_x: float, point_y: float) -> tuple[int, int]:
        """Quartz global point (from `CGEventGetLocation`) -> physical-pixel SCREEN
        coordinate - the inverse of `_pixel_to_point`."""
        for m in self._monitor_infos():
            scale = self._display_scale(int(m.id))
            bounds = Quartz.CGDisplayBounds(int(m.id))
            bx, by, bw, bh = (
                bounds.origin.x,
                bounds.origin.y,
                bounds.size.width,
                bounds.size.height,
            )
            if bx <= point_x < bx + bw and by <= point_y < by + bh:
                local_pt_x = point_x - bx
                local_pt_y = point_y - by
                return (
                    m.x + round(local_pt_x * scale),
                    m.y + round(local_pt_y * scale),
                )
        # Outside every display's point-bounds (e.g. a stale cursor report during a
        # display reconfiguration) - fall back to the primary monitor's own scale
        # rather than raising, mirroring `_covering_monitor_for_pixel`'s fallback.
        monitors = self._monitor_infos()
        if not monitors:
            raise BackendError("no active displays; cannot resolve cursor position")
        primary = next((m for m in monitors if m.primary), monitors[0])
        scale = self._display_scale(int(primary.id))
        return (round(point_x * scale), round(point_y * scale))

    # -- Backend protocol: geometry + capture -------------------------------------
    def screen_geometry(self) -> ScreenGeometry:
        """Bounding box across every active display, in physical pixels.

        Origin is normalized using the *main* display's scale factor (documented
        limitation for mixed-DPI setups - see `_monitor_infos`). This is only reached
        in `monitors.VIRTUAL_DESKTOP` mode; the default (`PRIMARY`) targeting scopes to
        a single real monitor's own, exactly-correct bounds instead.
        """
        main_id = Quartz.CGMainDisplayID()
        main_scale = self._display_scale(main_id)
        ids = self._active_display_ids()
        if not ids:
            raise BackendError("no active displays; cannot report screen geometry")
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")
        for display_id in ids:
            b = Quartz.CGDisplayBounds(display_id)
            min_x = min(min_x, b.origin.x)
            min_y = min(min_y, b.origin.y)
            max_x = max(max_x, b.origin.x + b.size.width)
            max_y = max(max_y, b.origin.y + b.size.height)
        return ScreenGeometry(
            width=round((max_x - min_x) * main_scale),
            height=round((max_y - min_y) * main_scale),
            origin_x=round(min_x * main_scale),
            origin_y=round(min_y * main_scale),
        )

    def list_monitors(self) -> list[MonitorInfo]:
        monitors = self._monitor_infos()
        if not monitors:
            raise BackendError(
                "CGGetActiveDisplayList reported zero active displays; cannot "
                "select a per-monitor target on this Mac (screen asleep or "
                "clamshell-closed with no external display?)"
            )
        return monitors

    @staticmethod
    def _screen_recording_cause(granted: bool | None) -> str:
        """The cause-and-remediation clause for an OBSERVED preflight result.

        Split out of `_capture_none_error` so the same three diagnoses can be
        reused by any caller that has already read the preflight - notably the
        `screencapture` fallback's own refusal, which previously collapsed
        `False` and `None` into one generic "was not positive" message and
        threw away the remediation this text carries.

        Takes the observed value as an argument rather than reading it:
        a second read can disagree with the one the decision was actually
        made on, and then the message describes a state that never governed
        anything.
        """
        if granted is False:
            return (
                "Screen Recording permission is NOT granted to this "
                "process (CGPreflightScreenCaptureAccess() == False). macOS "
                "raises no exception for this - it just hands back nothing. "
                "Two known causes: (1) the grant was never made - open "
                "System Settings -> Privacy & Security -> Screen Recording "
                "and enable the process actually running this code; (2) "
                f"{_CONCURRENT_AGENT_CAUSE}."
            )
        if granted is True:
            # Permission genuinely is granted - a permissions guess would be
            # wrong here, so this is the one branch that mentions sleep.
            return (
                "Screen Recording permission is granted "
                "(CGPreflightScreenCaptureAccess() == True) - the display "
                "itself is the likely cause: it may have gone to sleep or "
                "been disconnected."
            )
        # Could not check (older macOS/pyobjc without the preflight symbol) -
        # name every known cause honestly rather than asserting one as fact.
        return (
            "this process could not determine Screen Recording "
            "permission status to narrow down why "
            "(CGPreflightScreenCaptureAccess unavailable on this system). "
            "Known causes, in likely order: a revoked or never-granted "
            "Screen Recording permission (macOS raises no exception for "
            f"this); {_CONCURRENT_AGENT_CAUSE}; or the display having gone "
            "to sleep or been disconnected."
        )

    def _capture_none_error(
        self, display_id: int | None, granted: bool | None | _Unset = _UNSET
    ) -> str:
        """Compose the error for `CGDisplayCreateImage` returning `None`.

        This used to be a single guessed message ("display may have gone to
        sleep or been disconnected") for every cause. That guess cost hours
        of investigation the one time it actually mattered: the real cause
        was a denied Screen Recording grant (poisoned by a second concurrent
        agent process against this same target), and `CGDisplayCreateImage`
        raises no exception for that either - it just returns `None`, exactly
        like it does for a sleeping/disconnected display. The two causes are
        distinguishable with one cheap, prompt-free preflight call
        (`_cg_preflight_screen_capture_access`), so this asks rather than
        guesses, and only reaches for "the display is asleep" once the
        permission itself is confirmed granted.

        `granted` may be supplied by a caller that has ALREADY observed the
        preflight, so the message describes the value the decision was made
        on rather than a second, possibly different read. Omitted, it reads
        the preflight itself - the original behaviour, unchanged.
        """
        if isinstance(granted, _Unset):
            granted = _cg_preflight_screen_capture_access()
        base = f"CGDisplayCreateImage({display_id}) returned no image"
        cause = self._screen_recording_cause(granted)
        if granted is False:
            return f"{base}: {cause}"
        if granted is True:
            return f"{base} even though {cause}"
        return f"{base}, and {cause}"

    @staticmethod
    def _single_display_fallback_error(reason: str) -> BackendError:
        """Return a fixed, non-sensitive error for the optional utility fallback."""
        return BackendError(f"single-display screencapture fallback {reason}")

    def _validate_single_display_fallback_target(
        self, expected: MonitorInfo, *, sole: bool = True, ids: list[int] | None = None
    ) -> None:
        """Fail closed unless the display being captured is still exactly `expected`.

        These independent reads cannot make display topology atomic. They only ensure
        the narrow fallback does not knowingly accept pixels after a changed, missing,
        ambiguous, or remapped target.

        `sole=True` (the default, and the single-display fallback's own contract) is
        the strictest form: exactly one active display, which is main, with unchanged
        geometry. `sole=False` is the per-display form the virtual-desktop compositor
        needs - the SAME guarantees scoped to one display of several: that display is
        still present, the active display LIST is unchanged in both membership and
        ORDER (which is what `-D`'s ordinal indexes, so a reorder mid-capture would
        silently retarget), and that display's own geometry is unchanged. It does not
        require the display to be main, because a secondary display never is.
        """
        try:
            expected_id = int(expected.id)
            active = self._active_display_ids()
            if sole:
                if active != [expected_id]:
                    raise ValueError("active display changed")
                if int(Quartz.CGMainDisplayID()) != expected_id:
                    raise ValueError("main display changed")
            else:
                if ids is not None and active != list(ids):
                    raise ValueError("active display list changed or reordered")
                if expected_id not in active:
                    raise ValueError("target display is no longer active")
            monitors = self._monitor_infos()
            if sole and len(monitors) != 1:
                raise ValueError("monitor set is not singular")
            current = next(
                (mi for mi in monitors if int(mi.id) == expected_id),
                None,
            )
            if current is None:
                raise ValueError("target display is no longer enumerated")
            if (
                int(current.id),
                current.x,
                current.y,
                current.width,
                current.height,
            ) != (
                expected_id,
                expected.x,
                expected.y,
                expected.width,
                expected.height,
            ):
                raise ValueError("monitor geometry changed")
        except Exception:  # noqa: BLE001 - fail closed without native/raw details
            raise self._single_display_fallback_error(
                "refused: target display changed or could not be validated"
            ) from None

    def _screencapture_single_display(
        self, expected: MonitorInfo, deadline: float
    ) -> Any:
        """Capture the current sole main display via a private PNG, then decode in memory.

        Reached only after a native `CGDisplayCreateImage` call returned `None` for an
        initially singular target. `-m` selects the freshly verified main display.
        Unchanged in behaviour: this is the strict, sole-display contract, and it is
        what the single-display fallback path still calls.
        """
        return self._screencapture_guarded(expected, deadline, sole=True)

    def _screencapture_display(
        self, expected: MonitorInfo, deadline: float, ordinal: int, ids: list[int]
    ) -> Any:
        """Per-display form of the same guarded capture, for the compositor.

        `ordinal` is `screencapture`'s 1-BASED `-D` index, not a `CGDirectDisplayID`.
        That mapping is `CGGetActiveDisplayList` ORDER, verified by content (not by
        size) on macOS 26.6.2 against a mixed-DPI pair - see the compositor's docstring
        for the measurement. The validator is given the full `ids` list so a reorder
        between resolving the ordinal and reading the pixels is caught rather than
        silently retargeting the capture.
        """
        return self._screencapture_guarded(
            expected, deadline, sole=False, ordinal=ordinal, ids=ids
        )

    def _screencapture_guarded(
        self,
        expected: MonitorInfo,
        deadline: float,
        *,
        sole: bool,
        ordinal: int | None = None,
        ids: list[int] | None = None,
    ) -> Any:
        """Shared implementation: one bounded `screencapture` child, fully guarded.

        Every guard is identical in both forms - fresh permission preflight, unlocked
        session before AND after the child, target re-validation before AND after,
        remaining-budget timeout, private 0700/0600 storage, DEVNULL stdio, in-memory
        decode before cleanup, and cleanup failure reported rather than swallowed.
        Only the display selector differs: `-m` for the sole-display contract,
        `-D <ordinal>` for one display of several.
        """
        preflight = _cg_preflight_screen_capture_access()
        if preflight is not True:
            # Fail closed, as before - but say WHICH non-positive result this
            # was and what to do about it. A denied grant and an unavailable
            # preflight symbol need different actions from the operator, and
            # the generic "was not positive" wording sent both to the same
            # dead end. Formats the value observed immediately above; never a
            # second read.
            raise self._single_display_fallback_error(
                "refused: fresh screen-capture preflight was not positive - "
                + self._screen_recording_cause(preflight)
            )

        temp_dir: str | None = None
        fd: int | None = None
        image_path: str | None = None
        try:
            try:
                temp_dir = tempfile.mkdtemp(prefix="amplifier-cu-capture-")
                os.chmod(temp_dir, 0o700)
                fd, image_path = tempfile.mkstemp(suffix=".png", dir=temp_dir)
                os.fchmod(fd, 0o600)
                os.close(fd)
                fd = None
            except OSError:
                raise self._single_display_fallback_error(
                    "could not create private temporary storage"
                ) from None

            self._validate_single_display_fallback_target(expected, sole=sole, ids=ids)
            state, _detail = _macos_session_state()
            if state != "unlocked":
                raise self._single_display_fallback_error(
                    "refused: session is not unlocked"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._single_display_fallback_error("exceeded capture budget")

            try:
                proc = subprocess.run(
                    [
                        "/usr/sbin/screencapture",
                        "-x",
                        *(["-m"] if sole else ["-D", str(ordinal)]),
                        "-t",
                        "png",
                        image_path,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=remaining,
                    check=False,
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                raise self._single_display_fallback_error("timed out") from None
            except subprocess.SubprocessError:
                raise self._single_display_fallback_error("failed") from None
            except OSError:
                raise self._single_display_fallback_error(
                    "could not launch screencapture"
                ) from None
            if proc.returncode != 0:
                raise self._single_display_fallback_error("failed")
            if deadline - time.monotonic() <= 0:
                raise self._single_display_fallback_error("exceeded capture budget")

            state, _detail = _macos_session_state()
            if state != "unlocked":
                raise self._single_display_fallback_error(
                    "refused: session is not unlocked"
                )
            self._validate_single_display_fallback_target(expected, sole=sole, ids=ids)
            if image_path is None or not os.path.isfile(image_path):
                raise self._single_display_fallback_error("produced no PNG file")
            try:
                raw = Path(image_path).read_bytes()
            except OSError:
                raise self._single_display_fallback_error(
                    "could not read PNG data"
                ) from None
            if not raw:
                raise self._single_display_fallback_error("produced no PNG data")
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise self._single_display_fallback_error("produced invalid PNG data")

            try:
                from CoreFoundation import (
                    CFDataCreate,  # type: ignore[import-not-found]
                )

                data = CFDataCreate(None, raw, len(raw))
                source = Quartz.CGImageSourceCreateWithData(data, None)
                if source is None:
                    raise self._single_display_fallback_error(
                        "could not decode PNG data"
                    )
                image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
                if image is None:
                    raise self._single_display_fallback_error(
                        "could not decode PNG image"
                    )
                actual_size = (
                    int(Quartz.CGImageGetWidth(image)),
                    int(Quartz.CGImageGetHeight(image)),
                )
            except BackendError:
                raise
            except Exception:  # noqa: BLE001 - decoder details may contain private paths
                raise self._single_display_fallback_error(
                    "could not decode PNG data"
                ) from None
            if actual_size != (expected.width, expected.height):
                raise self._single_display_fallback_error(
                    "produced unexpected image dimensions"
                )
            if deadline - time.monotonic() <= 0:
                raise self._single_display_fallback_error("exceeded capture budget")
            return image
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_dir is not None:
                try:
                    shutil.rmtree(temp_dir)
                except OSError:
                    raise self._single_display_fallback_error(
                        "cleanup failed: private capture data may remain"
                    ) from None

    def _call_native(self, fn: Any, *args: Any) -> Any:
        """Make a native capture call, and learn from how it behaved.

        The platform's own call stays PRIMARY everywhere; everything this module
        adds is the exception path. That is deliberate, and it is what keeps a
        healthy macOS paying nothing for code it does not need - on 26.7 the
        native calls answer in 0.02-0.08s and neither the `screencapture`
        fallback nor the compositor ever runs.

        What makes native-first safe on a macOS where it is NOT healthy is this
        function. There are two distinct degradation signatures, and only one of
        them looks like a failure:

            CGDisplayCreateImage     ~5.0s  -> NULL            (macOS 26.6.2)
            CGWindowListCreateImage  30.04s -> a CORRECT image (macOS 26.6.2)

        The second returns exactly what was asked for, just far too late to be
        usable - 30s is also `SshTransport.send()`'s per-op timeout, so over the
        remote transport it does not return a slow image, it drops the
        connection. Only the DURATION catches that one.

        A native call cannot be cancelled once started. It can, however, be
        refused a second time: a call that was pathological once is recorded as
        degraded, and no later capture in this process attempts it again. That
        single fact replaces any version comparison. It is observed rather than
        predicted, so it self-corrects in both directions - if a future macOS
        regresses these calls again, nothing here needs to know the version
        number.
        """
        started = time.monotonic()
        image = fn(*args)
        elapsed = time.monotonic() - started
        if image is None or elapsed >= _NATIVE_CAPTURE_SLOW_SECONDS:
            if not self._native_capture_degraded:
                logger.info(
                    "macos: native capture degraded on this system (%s in %.2fs, "
                    "image=%s); using the screencapture path for the rest of this "
                    "session",
                    getattr(fn, "__name__", fn),
                    elapsed,
                    image is not None,
                )
            self._native_capture_degraded = True
        return image

    def _learn_native_capture_health(self, ids: list[int]) -> None:
        """Settle the degraded question using the CHEAP call, never the expensive one.

        A session whose very first capture is a whole-virtual-desktop capture has
        nothing to go on yet. Asking `CGWindowListCreateImage` directly would cost
        30s on a degraded system to discover that it costs 30s - and that is
        precisely the op-timeout drop being avoided. `CGDisplayCreateImage` answers
        the same question about the same capture stack for ~5s worst case, so the
        cheap call is the one that gets to be wrong.

        Inference, stated plainly: a healthy `CGDisplayCreateImage` is taken as
        evidence that `CGWindowListCreateImage` is healthy too. They are different
        calls and could in principle diverge; if they do, `_call_native` observes
        it on the first whole-desktop capture and the session never pays twice.
        """
        if self._native_capture_degraded or not ids:
            return
        try:
            self._call_native(Quartz.CGDisplayCreateImage, ids[0])
        except Exception:  # noqa: BLE001 - a probe that cannot run teaches nothing
            # Deliberately NOT treated as degraded. This is a question we failed
            # to ask, not an answer: leaving the flag alone keeps native-first,
            # and `_call_native` still records the real outcome when the actual
            # capture runs.
            return

    def _screencapture_virtual_desktop(self, ids: list[int], deadline: float) -> Any:
        """Composite one guarded per-display capture into the virtual desktop image.

        A fallback for `CGWindowListCreateImage(CGRectInfinite, ...)`, which on the
        measured macOS 26.6.2 returned correct pixels but took ~30.0s per call:
        30.07/30.01/30.00s single-display and 30.04s with two displays attached. That
        is also `SshTransport.send()`'s per-op timeout, so over the remote transport
        this path does not return a slow image, it drops the connection.

        It reproduces that call's geometry rather than inventing its own: the
        point-space bounding box of every active display, scaled by the LARGEST backing
        scale among them. That is what makes it substitutable on a mixed-DPI setup,
        where no single physical-pixel unit is correct for two displays at once.

        Measured on macOS 26.6.2 with a 2x built-in (1728x1117 points at origin 0,0)
        beside a 1x 5120x1440 ultrawide at points x=1728: both paths return 13696x2880,
        this one in 0.47s against 30.04s, a 65x speedup. Pixel difference 2.53% mean /
        32 of 255 peak, concentrated entirely in the upscaled 1x half (3.31%) while the
        natively captured 2x half is 0.21%; a control capture 30s apart drifted 0.00%,
        so that residual is interpolation on the upscale, not content change.

        `-D`'s ordinal mapping is `CGGetActiveDisplayList` ORDER, verified by CONTENT
        on that machine rather than by size - each ordinal's capture was correlated
        against a per-display `CGWindowListCreateImage` reference:

            -D 1 vs reference(id 1)   7.83      -D 2 vs reference(id 3)   1.85
            -D 1 vs reference(id 3)  76.03      -D 2 vs reference(id 1)  74.82

        Returns `None` if compositor setup is unavailable or no final image is
        produced. Guard, per-display capture, topology, and cleanup failures
        propagate to the caller. `capture()` reports an unavailable composite
        without retrying native capture or returning a partial canvas.
        """
        # PHASE 1 - setup only. Nothing here starts a child process, touches disk,
        # or consults a guard. Unavailable setup (e.g. missing bitmap-context
        # symbols) returns None for capture() to report, not to retry native
        # capture. BackendError still propagates, including during setup.
        try:
            scales = {d: self._display_scale(d) for d in ids}
            bounds = {d: Quartz.CGDisplayBounds(d) for d in ids}
            monitors = {int(mi.id): mi for mi in self._monitor_infos()}
            if not scales or not bounds or not all(d in monitors for d in ids):
                return None

            max_scale = max(scales.values())
            min_x = min(b.origin.x for b in bounds.values())
            min_y = min(b.origin.y for b in bounds.values())
            max_x = max(b.origin.x + b.size.width for b in bounds.values())
            max_y = max(b.origin.y + b.size.height for b in bounds.values())
            canvas_w = int(round((max_x - min_x) * max_scale))
            canvas_h = int(round((max_y - min_y) * max_scale))
            if canvas_w <= 0 or canvas_h <= 0:
                return None

            context = Quartz.CGBitmapContextCreate(
                None,
                canvas_w,
                canvas_h,
                8,
                0,
                Quartz.CGColorSpaceCreateDeviceRGB(),
                Quartz.kCGImageAlphaPremultipliedFirst
                | Quartz.kCGBitmapByteOrder32Little,
            )
            if context is None:
                return None
        except BackendError:
            raise
        except Exception:  # noqa: BLE001 - unavailable setup; caller reports failure
            return None

        # PHASE 2 - from here a child process may run, private files may exist on
        # disk, and the per-display guards may refuse. NOTHING below is swallowed.
        #
        # The previous shape caught every BackendError here and returned None, which
        # the caller read as "compositor unavailable" and answered with a legacy
        # capture. Two consequences, both reproduced in review:
        #   - a cleanup failure left a private capture file on disk while capture()
        #     returned a legacy image as success: undisclosed retained data;
        #   - a session that locked mid-capture had its post-child refusal swallowed,
        #     and the legacy image was returned while the session was locked: an
        #     explicit safety refusal bypassed.
        # A guard that refuses must reach the caller, never be answered by trying a
        # different capture.
        for ordinal, display_id in enumerate(ids, start=1):
            image = self._screencapture_display(
                monitors[display_id], deadline, ordinal, ids
            )
            if image is None:
                raise self._single_display_fallback_error(
                    f"produced no image for display ordinal {ordinal} "
                    "while compositing the virtual desktop"
                )
            b = bounds[display_id]
            # CGDisplayBounds is top-left origin growing DOWN; a bitmap context
            # is bottom-left origin growing UP - so a top-aligned SHORTER display
            # does not sit at y=0 here, it sits above the gap beneath it.
            rect = Quartz.CGRectMake(
                (b.origin.x - min_x) * max_scale,
                (max_y - (b.origin.y + b.size.height)) * max_scale,
                b.size.width * max_scale,
                b.size.height * max_scale,
            )
            Quartz.CGContextDrawImage(context, rect, image)

        # FINAL CONSISTENCY CHECK. Each display was validated only around its OWN
        # child capture, which leaves a real hole: display 1 passes its post-check,
        # then changes GEOMETRY while `-D 2` is still running, with the active ID
        # list and its order unchanged - so the per-display reorder check cannot
        # see it either. Display 2 passes, and a composite placing display 1 at a
        # width it no longer has is returned as current.
        #
        # So re-derive every placement input and compare against the snapshot the
        # canvas was actually built from. Best effort by construction, not atomic
        # topology access: these are independent reads and the layout can move
        # again immediately after. What it guarantees is that a change OBSERVED by
        # the time the composite is accepted invalidates it rather than shipping.
        self._validate_composite_snapshot(ids, scales, bounds)

        return Quartz.CGBitmapContextCreateImage(context)

    def _validate_composite_snapshot(
        self, ids: list[int], scales: dict[int, float], bounds: dict[int, Any]
    ) -> None:
        """Fail closed unless the whole layout still matches what was composited.

        `scales` and `bounds` are the values the canvas geometry and every draw
        rect were computed from. Re-reading them and comparing is the only way to
        notice a display that moved or resized mid-composite while the ID list
        stayed identical.
        """
        try:
            current_ids = self._active_display_ids()
            if current_ids != list(ids):
                raise ValueError("active display list changed or reordered")
            for display_id in ids:
                if self._display_scale(display_id) != scales[display_id]:
                    raise ValueError(f"display {display_id} backing scale changed")
                was, now = bounds[display_id], Quartz.CGDisplayBounds(display_id)
                if (
                    was.origin.x,
                    was.origin.y,
                    was.size.width,
                    was.size.height,
                ) != (
                    now.origin.x,
                    now.origin.y,
                    now.size.width,
                    now.size.height,
                ):
                    raise ValueError(f"display {display_id} geometry changed")
        except BackendError:
            raise
        except Exception:  # noqa: BLE001 - fail closed without native detail
            raise self._single_display_fallback_error(
                "refused: the display layout changed while the virtual desktop was "
                "being composited, so the composite no longer describes the screen"
            ) from None

    def capture(self, region: tuple[int, int, int, int] | None = None) -> bytes:
        """Return PNG bytes at native (physical-pixel) resolution.

        Single-display or region-within-one-display path (the common case, and the
        only one exercised on the real verification machine): `CGDisplayCreateImage`
        already returns pixels at that display's own physical resolution, so a region
        is applied with a plain pixel-space crop (`CGImageCreateWithImageInRect`) - no
        point/pixel conversion needed here, because both the captured image and the
        region (SCREEN space, physical pixels per the `Backend` protocol) are already
        in the same unit.

        Whole-virtual-desktop path (`region=None` with more than one active display,
        i.e. `monitors.VIRTUAL_DESKTOP` mode): `CGWindowListCreateImage` answers it
        whenever it is healthy - 0.07s on macOS 26.7. Only when it is not does this
        composite the per-display guarded captures into one canvas: the point-space
        bounding box of every active display at the LARGEST backing scale among them,
        which reproduces what that call itself produces, including its approximation
        for genuinely mixed-DPI setups (a 1x display is upscaled to the 2x canvas).
        Verified against it on a mixed-DPI pair: same 13696x2880 output, 0.47s against
        that call's 30.04s on macOS 26.6.2, where it was pathological. On 26.7 the
        same call takes 0.07s and this compositor never runs. Both are measurements
        of those two releases; nothing here infers behaviour on any other.

        `-D` ordinals index `CGGetActiveDisplayList`, whose documented contract puts
        the main display first - this relies on that contract rather than re-checking
        it. Secondary ordering with three or more displays is an explicitly unverified
        configuration.

        NATIVE FIRST, both here and on the per-display path below - see
        `_call_native`. Nothing in this backend compares OS versions; a native call
        that behaves pathologically once is recorded and not attempted again in this
        process, which is observed rather than predicted and so survives Apple fixing
        or re-breaking these calls.

        Checked BEFORE any Quartz capture call, every time (never cached): a locked
        screen produces a real, plausible-looking `CGDisplayCreateImage` result, not
        `None` and not an exception - see `_macos_session_state()`'s module-level
        docstring for the real incident this refusal exists to prevent.

        If that native per-display call returns `None`, a guarded `screencapture`
        alternative runs for the display being captured: `-m` when it is the sole
        active display (which must still be main, with unchanged geometry), else
        `-D <1-based ordinal>` into the active display list, which must still contain
        that display, with unchanged geometry and no reordering. Both forms require a
        fresh positive permission preflight and an unlocked session before and after
        the child, decode the private temporary PNG into memory before cleanup, and
        then follow this method's existing crop/encode path.

        FAILURE POLICY. A guard that refuses - permission, session, topology, budget,
        or a cleanup failure - propagates to the caller. It is never answered by
        trying a different capture: that would hand back an image taken after an
        explicit refusal, or report success while a private capture file remained on
        disk.

        There is NO retry of `CGWindowListCreateImage` when the compositor cannot be
        set up. The native call was either skipped as degraded or returned None.
        Retrying a call already known to be pathological costs ~30s on
        the macOS where that is true - which is also the transport's per-op timeout,
        so the "retry" drops the connection rather than producing an image. That
        condition is reported instead.

        The session is re-read at two points where wall-clock has passed since this
        method's entry check: after the health probe and before the real native
        capture, and before compositing. Both exist because a native capture call is
        not free - it is time in which a screen can lock, after which a locked screen
        returns a real, plausible-looking image that nothing downstream questions.

        None of this makes topology or permission use atomic, and none of it promises
        a hard capture wall time - the budget is elapsed-time accounting for the
        child, not a native-call deadline, which CoreGraphics does not offer.
        """
        fallback_deadline = time.monotonic() + 20.0
        state, detail = _macos_session_state()
        if state != "unlocked":
            raise BackendError(
                _session_state_error(state, detail, "capture a screenshot")
            )
        ids = self._active_display_ids()
        if not ids:
            raise BackendError("no active displays; cannot capture the screen")

        if region is None and len(ids) > 1:
            # NATIVE FIRST, exactly as the per-display path below already does.
            # `CGWindowListCreateImage` is the platform's own answer here and on a
            # healthy macOS it is the fast one (0.07s on 26.7); the compositor is
            # the exception path, not the default. Symmetry with the per-display
            # branch is what removes any need to gate on an OS version.
            cg_image = None
            self._learn_native_capture_health(ids)
            if not self._native_capture_degraded:
                # The probe above is not free. It is a real native capture call
                # that consumes real wall-clock - up to ~5s on a degraded system -
                # which makes it a window in which the screen can lock, between
                # this method's entry check and the capture that check was meant
                # to guard. Whether the probe answered fast, or raised, it moved
                # time forward, so the session has to be read again before the
                # actual whole-desktop capture starts. This is a NEW intervening
                # call introduced by native-first; it does not exist upstream.
                state, detail = _macos_session_state()
                if state != "unlocked":
                    raise BackendError(
                        _session_state_error(state, detail, "capture a screenshot")
                    )
                cg_image = self._call_native(
                    Quartz.CGWindowListCreateImage,
                    Quartz.CGRectInfinite,
                    Quartz.kCGWindowListOptionOnScreenOnly,
                    Quartz.kCGNullWindowID,
                    Quartz.kCGWindowImageDefault,
                )
                if cg_image is not None and self._native_capture_degraded:
                    # It answered, but pathologically slowly - so it is recorded
                    # degraded and will not be attempted again this session. The
                    # image itself is real; use it rather than waste the wait.
                    return self._encode_png(cg_image)
            if cg_image is None:
                # Re-read the session before compositing. The check at the top of
                # this method can be arbitrarily stale by now: a degraded native
                # call can burn 30s on its own, long enough for a screen to lock
                # inside one capture, and a locked screen returns a real,
                # plausible-looking image that nothing downstream would question.
                state, detail = _macos_session_state()
                if state != "unlocked":
                    raise BackendError(
                        _session_state_error(state, detail, "capture a screenshot")
                    )
                # A guard refusal inside the compositor propagates out of this
                # call - it is never answered by trying a different capture.
                # `None` means only "this platform cannot composite".
                cg_image = self._screencapture_virtual_desktop(ids, fallback_deadline)
            if cg_image is None:
                raise BackendError(
                    "neither CGWindowListCreateImage nor the screencapture "
                    "compositor produced a virtual-desktop image"
                )
            return self._encode_png(cg_image)

        display_id = ids[0] if region is None else None
        if region is not None:
            x1, y1, x2, y2 = region
            m = self._covering_monitor_for_pixel(x1, y1)
            display_id = int(m.id)
            local_x, local_y = x1 - m.x, y1 - m.y
            w, h = max(1, x2 - x1), max(1, y2 - y1)
        else:
            local_x = local_y = 0
            m = next(mi for mi in self._monitor_infos() if int(mi.id) == display_id)
            w, h = m.width, m.height

        # Same rule as the branch above: native first, unless this process has
        # already watched it fail. Skipping it once degraded is what removes the
        # ~5s-per-screenshot cost on a macOS where it returns NULL the slow way.
        full_image = (
            None
            if self._native_capture_degraded
            else self._call_native(Quartz.CGDisplayCreateImage, display_id)
        )
        if full_image is None:
            if len(ids) == 1 and display_id == ids[0] and int(m.id) == ids[0]:
                full_image = self._screencapture_single_display(m, fallback_deadline)
            elif display_id is not None and int(m.id) in ids:
                # Per-display form of the same guarded fallback. Without this, a Mac
                # with a second display attached cannot take ANY per-monitor capture
                # on macOS 26.6.2 - and per-monitor IS the default (`target_monitor`
                # defaults to "primary", which routes here, not through the
                # whole-virtual-desktop branch above). Measured: attaching one more
                # display turned every screenshot into
                # "CGDisplayCreateImage(1) returned no image ... the display itself is
                # the likely cause", on a display that was awake and capturable.
                full_image = self._screencapture_display(
                    m, fallback_deadline, ids.index(int(m.id)) + 1, ids
                )
            else:
                raise BackendError(self._capture_none_error(display_id))
        if (local_x, local_y, w, h) == (
            0,
            0,
            Quartz.CGImageGetWidth(full_image),
            Quartz.CGImageGetHeight(full_image),
        ):
            cg_image = full_image
        else:
            crop_rect = Quartz.CGRectMake(local_x, local_y, w, h)
            cg_image = Quartz.CGImageCreateWithImageInRect(full_image, crop_rect)
            if cg_image is None:
                raise BackendError(f"failed to crop capture to region {region!r}")
        return self._encode_png(cg_image)

    @staticmethod
    def _encode_png(cg_image: Any) -> bytes:
        """Encode a `CGImageRef` to PNG bytes via ImageIO - delegates all pixel-format
        handling (byte order, alpha channel placement, ...) to Apple's own encoder
        rather than guessing `CGDisplayCreateImage`'s raw buffer layout, the same
        "let the platform's own tooling do this" reasoning `bridge.ps1` follows on
        Windows (`Bitmap.Save` writes real PNG/JPEG files; this backend never parses
        raw Win32 pixel buffers either)."""
        from CoreFoundation import (  # type: ignore[import-not-found]
            CFDataCreateMutable,
        )

        data = CFDataCreateMutable(None, 0)
        dest = Quartz.CGImageDestinationCreateWithData(data, "public.png", 1, None)
        if dest is None:
            raise BackendError("CGImageDestinationCreateWithData failed")
        Quartz.CGImageDestinationAddImage(dest, cg_image, None)
        if not Quartz.CGImageDestinationFinalize(dest):
            raise BackendError("CGImageDestinationFinalize failed to encode PNG")
        return bytes(data)

    # -- pointer -------------------------------------------------------------------
    def cursor_position(self) -> tuple[int, int]:
        event = Quartz.CGEventCreate(None)
        loc = Quartz.CGEventGetLocation(event)
        return self._point_to_pixel(loc.x, loc.y)

    def move(self, x: int, y: int) -> None:
        self._ensure_ready_for_input()
        point = self._pixel_to_point(x, y)
        event = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, point, Quartz.kCGMouseButtonLeft
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def _mouse_event_types(self, button: str) -> tuple[int, int]:
        if button == "left":
            return Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp
        if button == "right":
            return Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp
        if button == "middle":
            return Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp
        raise BackendError(f"unsupported button {button!r}")

    def click(
        self, x: int | None, y: int | None, button: str = "left", count: int = 1
    ) -> None:
        self._ensure_ready_for_input()
        btn_const = _BUTTON_CONST.get(button)
        if btn_const is None:
            raise BackendError(f"unsupported click button {button!r}")
        down_type, up_type = self._mouse_event_types(button)
        if x is not None and y is not None:
            self.move(x, y)
            point = self._pixel_to_point(x, y)
        else:
            loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            point = loc
        import time as _time

        for i in range(1, max(1, count) + 1):
            down = Quartz.CGEventCreateMouseEvent(None, down_type, point, btn_const)
            Quartz.CGEventSetIntegerValueField(down, Quartz.kCGMouseEventClickState, i)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
            up = Quartz.CGEventCreateMouseEvent(None, up_type, point, btn_const)
            Quartz.CGEventSetIntegerValueField(up, Quartz.kCGMouseEventClickState, i)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
            if i < count:
                _time.sleep(0.05)

    def mouse_down(self, x: int | None, y: int | None, button: str = "left") -> None:
        self._ensure_ready_for_input()
        btn_const = _BUTTON_CONST.get(button)
        if btn_const is None:
            raise BackendError(f"unsupported button {button!r}")
        down_type, _ = self._mouse_event_types(button)
        if x is not None and y is not None:
            self.move(x, y)
            point = self._pixel_to_point(x, y)
        else:
            point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        event = Quartz.CGEventCreateMouseEvent(None, down_type, point, btn_const)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def mouse_up(self, x: int | None, y: int | None, button: str = "left") -> None:
        self._ensure_ready_for_input()
        btn_const = _BUTTON_CONST.get(button)
        if btn_const is None:
            raise BackendError(f"unsupported button {button!r}")
        _, up_type = self._mouse_event_types(button)
        if x is not None and y is not None:
            self.move(x, y)
            point = self._pixel_to_point(x, y)
        else:
            point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        event = Quartz.CGEventCreateMouseEvent(None, up_type, point, btn_const)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def drag(self, start: tuple[int, int] | None, end: tuple[int, int]) -> None:
        self._ensure_ready_for_input()
        import time as _time

        if start is not None:
            self.move(*start)
        self.mouse_down(None, None, "left")
        _time.sleep(0.05)
        end_point = self._pixel_to_point(*end)
        dragged = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventLeftMouseDragged, end_point, Quartz.kCGMouseButtonLeft
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, dragged)
        _time.sleep(0.05)
        self.mouse_up(*end, "left")

    def scroll(self, x: int | None, y: int | None, direction: str, amount: int) -> None:
        self._ensure_ready_for_input()
        if x is not None and y is not None:
            self.move(x, y)
        vertical = {"up": 1, "down": -1}.get(direction)
        horizontal = {"left": -1, "right": 1}.get(direction)
        if vertical is None and horizontal is None:
            raise BackendError(f"unsupported scroll direction {direction!r}")
        wheel1 = vertical * max(1, amount) if vertical is not None else 0
        wheel2 = horizontal * max(1, amount) if horizontal is not None else 0
        event = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 2, wheel1, wheel2
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    # -- keyboard --------------------------------------------------------------
    def key(self, combo: str) -> None:
        self._ensure_ready_for_input()
        flags, keycode = _combo_flags_and_keycode(combo)
        down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
        Quartz.CGEventSetFlags(down, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
        Quartz.CGEventSetFlags(up, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    def hold_key(self, combo: str, duration: float) -> None:
        self._ensure_ready_for_input()
        import time as _time

        flags, keycode = _combo_flags_and_keycode(combo)
        down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
        Quartz.CGEventSetFlags(down, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        _time.sleep(max(0.0, duration))
        up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
        Quartz.CGEventSetFlags(up, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    def type_text(self, text: str, guard: Any = None) -> None:
        """Type literal text by posting a REAL, non-zero virtual keycode (plus
        Shift when needed) for every character, one Python string character
        at a time - the exact mechanism `key()` already uses and that has
        been verified, on real hardware, to reliably reach the focused
        element.

        FIXED 2026-08-04 - real-hardware defect, "a write that reports success
        while doing nothing"
        --------------------------------------------------------------------
        This previously typed via `CGEventKeyboardSetUnicodeString` on a
        keycode-0 event (Apple's documented "arbitrary Unicode, no layout
        table needed" technique - the one real automation tools use too, and
        this module's own prior docstring called "the macOS-native
        equivalent of `LinuxX11Backend.type_text`'s dynamic scratch-keysym
        mapping"). Live-hardware verification - screenshots compared
        before/after, not just "`CGEventPost` did not raise" - proved that
        assumption wrong: the keycode-0 technique posts successfully
        (`CGEventPost` never raises or signals failure either way) and
        delivered NOTHING. Confirmed on a direct in-process call AND through
        the full remote-agent wire path, at both `kCGHIDEventTap` and
        `kCGSessionEventTap`: Spotlight's search field was byte-for-byte
        unchanged after `type_text`. `key()` - a REAL, non-zero keycode plus
        `CGEventFlags` for any held modifiers, same `kCGHIDEventTap` - was
        verified, on the SAME target and field, to reliably land (a single
        `key("a")` correctly triggered Spotlight's own autocomplete). Two
        earlier reports of this exact defect were wrongly retracted as "the
        locked-screen defect, re-tested and it landed" - that re-test never
        compared actual pixel content, only that no exception was raised;
        this fix is the first verification that actually looked at what
        appeared on screen.

        `_char_to_keycode_and_flags` resolves each character via
        `_KEYCODE`/`_SHIFTED_CHAR_TO_BASE` - the SAME US-ANSI table `key()`
        already depends on. A character with no keycode on that layout
        (accented letters, non-Latin scripts, symbols/emoji outside both
        tables) cannot be resolved - and per the no-fallback rule, this
        method does NOT silently retry it through the unverified keycode-0
        technique and call that success. It raises `BackendError` naming
        every unsupported character before typing anything (atomic: nothing
        is typed, not even the supported prefix, so a caller never has to
        guess how far a partially-typed string got).

        Per-character, not one `CGEvent` for the whole string - this is what
        makes `guard` (`coexistence_guard.CoexistenceGuard`, \u00a75.2/\u00a78.6 of
        `docs/designs/coexistence.md`) meaningful here at all. `GUARD_MS["macos"]`
        is measured (O4 - see `presence.py`), so this backend claims the same
        intra-`type_text` detection Linux already does (\u00a75.2): a human keystroke
        landing MID-STRING must be able to interrupt between characters, which a
        single whole-string `CGEvent` made structurally impossible. Omitting `guard`
        (the default) preserves the per-character loop but skips every guard call -
        existing callers with no guard are unaffected.
        """
        self._ensure_ready_for_input()
        resolved: list[tuple[str, int, int]] = []
        unsupported: list[str] = []
        seen_unsupported: set[str] = set()
        for ch in text:
            hit = _char_to_keycode_and_flags(ch)
            if hit is None:
                if ch not in seen_unsupported:
                    seen_unsupported.add(ch)
                    unsupported.append(ch)
                continue
            resolved.append((ch, hit[0], hit[1]))
        if unsupported:
            raise BackendError(
                "type_text: cannot reliably deliver "
                f"{len(unsupported)} character(s) with no keycode on the US "
                f"ANSI layout this backend verifies against: {unsupported!r}. "
                "The alternative (CGEventKeyboardSetUnicodeString on a "
                "keycode-0 event) was measured on real hardware to post "
                "successfully while delivering nothing - see this method's "
                "docstring - so it is never used as a silent fallback here. "
                "Nothing in this call was typed. Split the text around the "
                "unsupported character(s), or send them via a different "
                "mechanism (e.g. the clipboard + 'key' paste combo)."
            )
        for _ch, keycode, flags in resolved:
            if guard is not None:
                guard.before_event()
            # `CGEventSetFlags` is called unconditionally, even for flags=0 -
            # verified live: skipping it for the (far more common) no-modifier
            # case left plain lowercase/digit characters undelivered while
            # shifted ones landed, on the same target and field. A freshly
            # created event's default flags are not a substitute for an
            # explicit, always-present flags call.
            down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
            Quartz.CGEventSetFlags(down, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
            up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
            Quartz.CGEventSetFlags(up, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
            if guard is not None:
                guard.after_event()

    # -- windows (CGWindowList) ----------------------------------------------------
    def list_windows(self) -> WindowList:
        """Enumerate on-screen windows via `CGWindowListCopyWindowInfo`.

        Requires Screen Recording permission for other apps' window *titles* to be
        populated (macOS 10.15+); confirmed granted for this process (see top-level
        report). Does NOT require Accessibility - this is a read query, not injection.

        `kCGWindowListOptionOnScreenOnly` returns windows in front-to-back z-order
        (Apple's documented behavior), so the first normal-layer window in the list
        is the true foreground window - no separate "active window" lookup needed,
        unlike the EWMH `_NET_ACTIVE_WINDOW` atom `LinuxX11Backend` reads explicitly.
        """
        info_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        if not info_list:
            raise BackendError(
                "CGWindowListCopyWindowInfo returned no windows; list_windows is "
                "unsupported in this session (no windows on screen, or Screen "
                "Recording permission missing)"
            )
        windows: list[WindowInfo] = []
        foreground: str | None = None
        for entry in info_list:
            layer = int(entry.get("kCGWindowLayer", 0))
            if layer != 0:
                continue  # menu bar, dock, overlays, ... - not a normal app window
            wid = entry.get("kCGWindowNumber")
            if wid is None:
                continue
            title = str(
                entry.get("kCGWindowName") or entry.get("kCGWindowOwnerName") or ""
            )
            handle = str(int(wid))
            if foreground is None:
                foreground = handle
            rect = self._window_rect(entry.get("kCGWindowBounds"))
            windows.append(WindowInfo(handle, title, minimized=False, rect=rect))
        return WindowList(windows, foreground)

    def _window_rect(self, bounds: Any) -> tuple[int, int, int, int] | None:
        """`kCGWindowBounds` (a `CFDictionary` with `X`/`Y`/`Width`/`Height`, in
        Quartz global-display POINTS, top-left origin - same convention as
        `CGDisplayBounds`, see the module docstring's coordinate-origin note)
        converted to SCREEN-space physical-pixel `(left, top, right, bottom)`.

        Reuses `_point_to_pixel` (already proven correct for `cursor_position`)
        for the top-left corner, then scales width/height by the SAME
        monitor's backing scale - consistent with `_monitor_infos`' documented
        single-display-per-window assumption (a window straddling two
        differently-scaled displays is not perfectly exact here, a known,
        already-documented limitation, not a new one this introduces).

        `None` (never a guess) if `bounds` is missing or malformed, or if no
        active display's point-space contains the window's top-left corner
        (`_point_to_pixel` itself only raises when there are zero active
        displays at all - that propagates, since a call with literally no
        display data cannot report window geometry either).

        FIXED 2026-08-04 - real-hardware defect, "a function that returns
        None for every input"
        --------------------------------------------------------------------
        This previously gated `bounds` with `isinstance(bounds, dict)`, never
        verified against real hardware. Confirmed live on a Mac:
        `entry.get("kCGWindowBounds")` hands back a genuine Objective-C
        `NSDictionary` (`type(bounds)` prints `<objective-c class
        '__NSDictionaryI'>`) - it supports `.get()`/`.keys()`/`__getitem__`
        (all used elsewhere in this file) but is NOT a Python `dict`
        subclass, so `isinstance(bounds, dict)` was `False` for every window,
        every call - the function returned `None` before ever reading `X`/
        `Y`/`Width`/`Height`, with no exception and no log line. This shipped
        past 594 passing tests because every existing test double for
        `kCGWindowBounds` used a plain Python `{...}` literal - a real
        `dict` - which the `isinstance` check accepted; the mock was more
        permissive than the real bridge type it stood in for. The fix drops
        the type gate for plain `bounds is None` and leans on the
        `try`/`except` immediately below (already present) to turn any
        genuinely missing key or non-numeric value into the same honest
        `None` - malformed/absent `bounds` is still `None`, but a
        well-formed `NSDictionary` (the only kind real hardware produces) is
        no longer rejected on the way in.
        """
        if bounds is None:
            return None
        try:
            px, py = float(bounds["X"]), float(bounds["Y"])
            pw, ph = float(bounds["Width"]), float(bounds["Height"])
        except (KeyError, TypeError, ValueError):
            return None
        try:
            left, top = self._point_to_pixel(px, py)
        except BackendError:
            return None
        for m in self._monitor_infos():
            bnds = Quartz.CGDisplayBounds(int(m.id))
            if bnds.origin.x <= px < bnds.origin.x + bnds.size.width and (
                bnds.origin.y <= py < bnds.origin.y + bnds.size.height
            ):
                scale = self._display_scale(int(m.id))
                return left, top, left + round(pw * scale), top + round(ph * scale)
        return None

    def focus_window(self, handle: str) -> None:
        """Raise and activate window `handle` via `osascript` + System Events.

        See the module docstring for why this shells out rather than bridging the
        Accessibility API in-process: matching a `CGWindowID` to an `AXUIElement`
        publicly requires re-deriving (owner pid, title) and searching that process's
        AX window list by title, which is exactly what System Events' own AppleScript
        dictionary already does correctly as "window N whose ...". This also shares
        the same Accessibility TCC gate as direct `CGEventPost` input, gated the same
        way here.
        """
        self._ensure_ready_for_input()
        try:
            wid = int(handle)
        except ValueError as exc:
            raise BackendError(f"invalid window handle {handle!r}") from exc

        info_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
        )
        match = next(
            (e for e in (info_list or []) if int(e.get("kCGWindowNumber", -1)) == wid),
            None,
        )
        if match is None:
            raise BackendError(
                f"window handle {handle!r} not found (window may have closed)"
            )
        pid = int(match.get("kCGWindowOwnerPID", -1))
        title = str(match.get("kCGWindowName") or "")
        if pid < 0:
            raise BackendError(f"window {handle!r} has no owning process")

        script = (
            f'tell application "System Events"\n'
            f"  set theProc to first process whose unix id is {pid}\n"
            f"  set frontmost of theProc to true\n"
        )
        if title:
            escaped = title.replace("\\", "\\\\").replace('"', '\\"')
            script += (
                f'  perform action "AXRaise" of '
                f'(first window of theProc whose name is "{escaped}")\n'
            )
        script += "end tell"

        proc = subprocess.run(
            ["osascript", "-e", script],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self._osascript_timeout,
            check=False,
        )
        if proc.returncode != 0:
            raise BackendError(
                f"focus_window failed for pid={pid} title={title!r}: "
                f"{proc.stderr.strip() or proc.returncode}"
            )

    # -- clipboard (shells out to pbcopy/pbpaste - see module docstring) ----------
    def get_clipboard(self) -> str:
        exe = shutil.which("pbpaste")
        if not exe:
            raise BackendError(
                "pbpaste not found on PATH; required for clipboard access"
            )
        proc = subprocess.run(
            [exe],
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
                f"pbpaste failed: {proc.stderr.strip() or proc.returncode}"
            )
        return proc.stdout

    def set_clipboard(self, text: str) -> None:
        exe = shutil.which("pbcopy")
        if not exe:
            raise BackendError(
                "pbcopy not found on PATH; required for clipboard access"
            )
        proc = subprocess.run(
            [exe],
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
                f"pbcopy failed: {proc.stderr.strip() or proc.returncode}"
            )

    # -- coexistence: presence + target binding (docs/designs/coexistence.md) ----
    def presence_idle_ms(self) -> float:
        """Milliseconds since the last input event (real OR synthetic) reached
        this machine, via `CGEventSourceSecondsSinceLastEventType` against the
        HID system input state - the `idle_source` the presence detector
        (`presence.PresenceMonitor`) reconciles against its own injection
        timestamps (\u00a75 of the coexistence design), the macOS counterpart to
        `LinuxX11Backend.presence_idle_ms`.

        `kCGEventSourceStateHIDSystemState` (not `...CombinedSessionState`)
        matches what a real human's hardware keyboard/mouse and this backend's
        own `CGEventPost` calls both feed - the same system-wide HID event
        stream, so our own injections reset this counter exactly like a real
        keystroke does (the same U1b property already relied on for Linux/
        Windows). `kCGAnyInputEventType` reports the most recent event of ANY
        type (motion, click, or key), matching \u00a75's "any input, synthetic or
        real" requirement.

        Verified working over SSH on a live MacBook (macOS 26.6 arm64) as part
        of the O4 measurement run `presence.GUARD_MS["macos"]` is now based on
        - see `presence.py`. Cheap: one in-process Quartz call, no subprocess,
        safe to call once per elementary event.

        Not part of the `Backend` protocol (`backend.py`) - looked up via
        `getattr` by whatever constructs the `CoexistenceGuard` for this
        backend, exactly like the Linux equivalent.
        """
        seconds = Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType
        )
        return float(seconds) * 1000.0

    def current_target(self) -> str | None:
        """Frontmost window handle, for target binding (\u00a78.6) - the O9 answer
        for macOS.

        Reuses the exact z-order signal `list_windows()` already trusts: Apple
        documents `kCGWindowListOptionOnScreenOnly` as returning windows in
        front-to-back order, so the first normal-layer (`kCGWindowLayer == 0`)
        entry is the true frontmost window. This needs only the Screen
        Recording permission `capture()`/`list_windows()` already depend on -
        NOT the Accessibility/Automation TCC prompt a `CGWindowID ->
        AXUIElement` lookup (`focus_window()`'s `osascript` approach) would
        require. That is the deliberate choice made here: a reliable
        frontmost-window identity IS obtainable without a new permission
        prompt, so target binding can be enforced on macOS rather than merely
        declared "unverified" as \u00a78.6 anticipated might be necessary
        pending O9.

        Returns `None` - never a guessed handle - when window enumeration
        itself is unavailable (Screen Recording revoked, or zero on-screen
        windows). `TargetBinding.status` then reports `"unverified"` for the
        operation rather than silently pretending to enforce a check it could
        not make (\u00a78.6) - the same stable, declared-honest fallback
        `LinuxX11Backend.current_target` uses when its desktop has no
        `_NET_ACTIVE_WINDOW`; `None` IS that stable sentinel, not a new one,
        because `TargetBinding` already defines exactly what it means.

        Caveat, stated plainly: this is reasoned from the same documented
        z-order behavior `list_windows()` already relies on in production, but
        has not been independently re-verified against a live Mac for
        target-binding use specifically - no macOS hardware was driven for
        this change (see the accompanying report). Treat this as a
        well-founded implementation pending that confirmation, not as a
        second O9 probe result.
        """
        try:
            info_list = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            )
        except Exception:  # noqa: BLE001 - a read failure here must never crash
            return None
        for entry in info_list or []:
            if int(entry.get("kCGWindowLayer", 0)) != 0:
                continue
            wid = entry.get("kCGWindowNumber")
            if wid is None:
                continue
            return str(int(wid))
        return None

    def close(self) -> None:
        """No persistent resources held: every Core Graphics/AX call above is a
        stateless, connectionless system call - there is nothing to release, the
        same shape as `WindowsBackend.close()` (each action is its own round trip)
        rather than `LinuxX11Backend.close()` (a persistent Xlib socket)."""
