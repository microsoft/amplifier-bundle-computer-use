"""The backend protocol: what any computer-use backend must be able to do.

This is the seam introduced to fix D1 (no capability probe) and to let this bundle
run somewhere other than "Windows over WSL2". It is deliberately shaped around
*what a backend must do* - probe availability, report display geometry, capture the
screen, move/click/scroll, send keys/text, read/write the clipboard, enumerate/focus
windows - never around *how* one particular backend happens to do it.

Why this shape, specifically
-----------------------------
It was designed from two real implementations, not guessed from one:

* `WindowsBackend` crosses the WSL2 -> Win32 boundary. There is no in-process route
  across that boundary from WSL2, so every action necessarily subprocesses out to
  `powershell.exe`. Its `probe()` is a cheap PATH lookup; its actions cost tens of
  milliseconds each.
* `LinuxX11Backend` talks to the X server in-process via Xlib/XTEST. There is no
  subprocess anywhere on its hot path; its actions cost microseconds.

Because these two implementations disagree on nearly everything *except* the shape
of what they can be asked to do, the protocol only commits to that shape. Nothing
here assumes a subprocess, a JSON round trip, a `raw(action, **kwargs)` dispatcher,
or any other implementation detail belonging to one backend - `ComputerTool` and
`DesktopTool` only ever see this protocol, never the concrete classes.

Coordinate convention: every method that takes or returns a position operates in
SCREEN space (the backend's native pixel space). MODEL<->SCREEN conversion is the
caller's job (see `geometry.Display`) - a backend never sees or produces model-space
coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class BackendError(RuntimeError):
    """A backend could not complete a requested action, or could not be reached."""


class MonitorEnumerationUnavailable(BackendError):
    """`Backend.list_monitors()` could not enumerate monitors, carrying one
    extra fact `_resolve_display_for_target` (`__init__.py`) needs to decide
    how loud the resulting whole-desktop-bounding-box fallback should be:
    whether the backend could affirmatively PROVE no external monitor is
    connected.

    `expected=True` means the backend confirmed, via its own honest probe
    (never a guess), that this is genuinely a headless/no-monitor session -
    the whole-desktop fallback is not merely "the best we can do", it is
    provably the correct answer, so no human needs to see it.

    `expected=False` (the default - and what a plain, un-decorated
    `BackendError` from an older/other `list_monitors()` implicitly means,
    via `getattr(exc, "expected", False)`) covers two cases the caller
    cannot otherwise tell apart, and treats both the same, conservatively:
      - the backend actively detected the OPPOSITE (a real monitor IS
        connected, yet RandR/equivalent still reports zero monitors) - a
        genuine anomaly worth a human's attention, or
      - the backend has no way to make this determination at all.
    Either way the fallback stays exactly as loud as it always was before
    this distinction existed - only the PROVEN-benign case gets quieter.
    """

    def __init__(self, message: str, *, expected: bool = False) -> None:
        super().__init__(message)
        self.expected = expected


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of `Backend.probe()`.

    `probe()` must never raise; a backend that cannot determine its own availability
    reports that as `ProbeResult(available=False, reason=...)`, not an exception.
    """

    available: bool
    reason: str = ""


@dataclass(frozen=True)
class ScreenGeometry:
    """Real physical pixel dimensions of the desktop, in SCREEN space."""

    width: int
    height: int
    origin_x: int = 0
    origin_y: int = 0


@dataclass(frozen=True)
class MonitorInfo:
    """One REAL physical monitor, in SCREEN space.

    `ScreenGeometry` (above) may describe a virtual-desktop bounding box spanning
    every monitor on a multi-monitor machine - on the real machine that motivated
    this, a 9626x4323 box containing four 3840x2160 monitors plus ~20% dead space
    where no monitor exists. `MonitorInfo` is the antidote: one entry per monitor
    actually reported by the OS, with its own bounds, so a `geometry.Display` can
    be built for a *monitor* instead of the bounding box around all of them.

    `id` must be stable across calls within a session (a device name / RandR
    output name), so `target_monitor` config values and `desktop.select_monitor`
    requests can name a specific monitor and get the same one back every time.
    """

    id: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False
    #: Human-readable label, if the backend has one distinct from `id`.
    name: str = ""


@dataclass(frozen=True)
class WindowInfo:
    """One entry from `Backend.list_windows()`.

    `rect`, when known, is the window's bounding box in SCREEN space as
    `(left, top, right, bottom)` - the same convention `capture()`'s `region`
    parameter and every `MonitorInfo` already use, so a caller can compare a
    window's rect directly against `MonitorInfo` bounds with no unit
    conversion.

    `rect` is `None` when this backend could not determine the window's
    geometry for this call - never a guessed or synthetic box. This is the
    fix for a real incident: `list_windows` used to report a window's
    existence and title but nothing about *where* it was, so a
    `focus_window` call that raised a window on a monitor other than the one
    `computer` screenshots were scoped to looked, from the caller's side,
    identical to a `focus_window` call that silently did nothing - three
    sessions in a row misdiagnosed a working `focus_window` as broken because
    of this exact gap. `monitors.attribute_monitor` is the pure function that
    turns a `rect` plus a `list_monitors()` result into "which monitor is
    this window actually on" - deliberately NOT a field here, since that
    answer depends on which monitors are currently enumerated, a fact this
    dataclass (one backend call, no monitor list) does not have.
    """

    handle: str
    title: str
    minimized: bool = False
    rect: tuple[int, int, int, int] | None = None
    # OS-reported owning application, when the backend can identify it.
    app_name: str | None = None


@dataclass(frozen=True)
class WindowList:
    windows: list[WindowInfo] = field(default_factory=list)
    foreground: str | None = None


@runtime_checkable
class Backend(Protocol):
    """Everything a computer-use backend must implement, platform-agnostic.

    All coordinates are SCREEN space unless documented otherwise.
    """

    #: Short, stable identifier used in logs and error messages (e.g. "windows-wsl2").
    name: str

    def __init__(self, config: dict[str, Any] | None = None) -> None: ...

    def probe(self) -> ProbeResult:
        """Can this backend serve this machine?

        Must be cheap (no multi-second network/subprocess round trips - a PATH lookup
        or a lightweight connection attempt is fine) and must never raise. This is
        what makes D1 possible: `mount()` calls this *before* registering any tool, so
        a backend that cannot possibly work never gets mounted.
        """
        ...

    def screen_geometry(self) -> ScreenGeometry:
        """Real physical pixel dimensions of the desktop.

        May be expensive on first call (e.g. `WindowsBackend` crosses a subprocess
        boundary). Callers must resolve this once and cache the result (see
        `ComputerTool.resolve_display`) rather than calling it on every request - that
        caching, not this method, is what fixes D2.
        """
        ...

    def list_monitors(self) -> list[MonitorInfo]:
        """Enumerate physical monitors, in SCREEN space.

        Unlike `screen_geometry` (which may report a virtual-desktop bounding box
        spanning every monitor), this returns one entry per REAL physical monitor,
        each with its own bounds - `Display`'s `origin_x`/`origin_y` need a real
        monitor's bounds, not the bounding box, for per-monitor targeting to mean
        anything.

        Must raise `BackendError` if this machine's display server cannot
        enumerate monitors (e.g. no RandR extension, or an RandR version
        predating monitor enumeration) - never silently return a single
        synthetic entry standing in for reality. Whole-desktop operation
        (`screen_geometry`/`capture` with no region) does not depend on this
        method at all; only per-monitor targeting does.
        """
        ...

    def capture(self, region: tuple[int, int, int, int] | None = None) -> bytes:
        """Return PNG bytes at native resolution.

        `region`, if given, is a SCREEN-space `(x1, y1, x2, y2)` box; omit it to
        capture the whole desktop.
        """
        ...

    def cursor_position(self) -> tuple[int, int]:
        """Current pointer position, in SCREEN space."""
        ...

    def move(self, x: int, y: int) -> None:
        """Move the pointer to an absolute SCREEN-space position."""
        ...

    def click(
        self, x: int | None, y: int | None, button: str = "left", count: int = 1
    ) -> None:
        """Click `button` (`left`/`right`/`middle`) `count` times (1, 2, or 3).

        If `x`/`y` are `None`, click at the pointer's current position.
        """
        ...

    def mouse_down(
        self, x: int | None, y: int | None, button: str = "left"
    ) -> None: ...

    def mouse_up(self, x: int | None, y: int | None, button: str = "left") -> None: ...

    def drag(self, start: tuple[int, int] | None, end: tuple[int, int]) -> None:
        """Press at `start` (or the current position if `None`), move to `end`, release."""
        ...

    def scroll(
        self,
        x: int | None,
        y: int | None,
        direction: str,
        amount: int,
    ) -> None:
        """Scroll `amount` notches `direction` (`up`/`down`/`left`/`right`), optionally
        at a given position first."""
        ...

    def key(self, combo: str) -> None:
        """Press and release a key combo, e.g. `"ctrl+s"`, `"Return"`, `"a"`."""
        ...

    def hold_key(self, combo: str, duration: float) -> None:
        """Hold a key combo down for `duration` seconds, then release it."""
        ...

    def type_text(self, text: str, guard: Any = None) -> None:
        """Type literal text, one character at a time.

        `guard` is optional coexistence infrastructure
        (`coexistence_guard.CoexistenceGuard`, see that module and
        `docs/designs/coexistence.md` \u00a75.2/\u00a78.6) - a backend that supports
        per-keystroke intra-operation presence/pause/target-binding checks
        accepts it and calls `guard.before_event()`/`guard.after_event()`
        around each character (see `linux_x11.LinuxX11Backend.type_text`).
        A backend with no proven per-platform `GUARD` band (\u00a75.5) may accept
        and ignore it - the caller only ever passes a non-`None` guard when
        `presence.GUARD_MEASURED` (or an explicit override) supports it for
        this platform.
        """
        ...

    def list_windows(self) -> WindowList: ...

    def focus_window(self, handle: str) -> None: ...

    def get_clipboard(self) -> str: ...

    def set_clipboard(self, text: str) -> None: ...

    def close(self) -> None:
        """Release any held resources (connections, temp files, ...).

        Best-effort; safe to call multiple times; safe to never call.
        """
        ...
