"""WSL2 -> Windows desktop backend.

Every action crosses the WSL2/Win32 boundary through `bridge.ps1` via `powershell.exe`
interop - there is no in-process alternative for this boundary, so every method on
this backend necessarily subprocesses. Requests are handed over as a temp JSON file
(never as a -Command string) so no user text can be interpolated into a PowerShell
expression.

This module implements the `Backend` protocol (see `backend.py`). It is a mechanical
refactor of the original `WindowsBridge`: the wire format, the PowerShell invocation,
and every action's behavior are unchanged. It could not be exercised on this box (no
Windows target was available) - see the top-level report for what remains unverified.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import subprocess
import tempfile
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
from .target_clock import CURRENT_TARGET_CLOCK, guarded_target_call

logger = logging.getLogger(__name__)

BRIDGE_PS1 = Path(__file__).parent / "bridge.ps1"

#: Anthropic recommends staying at/below WXGA; larger costs tokens and *loses* accuracy.
#: (Enforced by the caller via `ComputerTool`/`compute_display`, not by this backend.)

_CLICK_ACTIONS = {
    ("left", 1): "left_click",
    ("right", 1): "right_click",
    ("middle", 1): "middle_click",
    ("left", 2): "double_click",
    ("left", 3): "triple_click",
}

_INPUT_ACTIONS = {
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


def _parse_rect(raw: Any) -> tuple[int, int, int, int] | None:
    """Parse `bridge.ps1`'s `list_windows` per-entry `rect` (`[L, T, R, B]`,
    from `GetWindowRect`) into `WindowInfo.rect`'s `(left, top, right,
    bottom)` shape.

    `None` (never a guess) if `raw` is missing or malformed - `bridge.ps1`
    already only emits `rect` for windows where `GetWindowRect` succeeded
    and reported a positive width/height (see its `list_windows` case), so
    a missing/malformed value here means this PARSE step failed, not that
    the window has no real geometry.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        left, top, right, bottom = (int(v) for v in raw[:4])
    except (TypeError, ValueError):
        return None
    return left, top, right, bottom


def _translate(path: str, flag: str) -> str:
    proc = subprocess.run(
        ["wslpath", flag, str(path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        raise BackendError(f"wslpath {flag} failed for {path!r}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _windows_mount_root() -> str | None:
    r"""Ask `wslpath` for WSL's actual automount root, instead of assuming `/mnt/c`.

    WSL's automount root is configurable (`/etc/wsl.conf`, `[automount] root = ...`),
    so `/mnt/c` is a default, not a guarantee. `wslpath` already knows the real
    answer - it is the same interop machinery that resolves the setting - so ask it
    directly rather than re-implementing wsl.conf parsing: converting the Windows
    path `C:\` back to a WSL path returns exactly the current mount root for the
    `C:` drive on this host.

    Returns `None` (never raises) if `wslpath` is missing or fails; callers treat
    that as "this candidate is unavailable" and fall through to the next one.
    """
    try:
        root = _translate("C:\\", "-u")
    except (BackendError, OSError):
        return None
    return root.rstrip("/") if root else None


def _which_powershell(configured: str | None) -> tuple[str | None, list[str]]:
    """Resolve the `powershell.exe` to invoke, without depending on PATH.

    Returns `(path, attempts)`: `path` is `None` if nothing usable was found, and
    `attempts` names every candidate that was tried and why it was rejected - so a
    caller can fail loudly with a diagnostic instead of silently mounting a backend
    that cannot work.

    `shutil.which` alone is not sufficient here: interactive WSL shells put the
    Windows interop directories on `PATH`, but non-login/non-interactive shells -
    exactly what remote agents, services, and `ssh host -- command` invocations use -
    typically do not (verified directly: `ssh <host> -- command -v powershell.exe`
    fails on a real WSL2 host where the full path works fine). The path-derivation
    fallback below is therefore load-bearing, not a nicety.

    It must not hardcode `/mnt/c`, though: WSL's automount root is configurable
    (see `_windows_mount_root`). The derived root is tried first; the conventional
    `/mnt/c` path is kept as a last-resort candidate only for hosts where `wslpath`
    itself is unavailable but the default mount happens to be correct anyway.
    """
    attempts: list[str] = []

    if configured:
        if Path(configured).exists() or shutil.which(configured):
            return configured, attempts
        attempts.append(
            f"configured powershell_path {configured!r} is not an existing file "
            "and is not resolvable on PATH"
        )
        return None, attempts

    found = shutil.which("powershell.exe")
    if found:
        return found, attempts
    attempts.append("powershell.exe not found via PATH (shutil.which)")

    candidates: list[str] = []
    root = _windows_mount_root()
    if root:
        candidates.append(
            f"{root}/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        )
    else:
        attempts.append("could not derive the Windows mount root via `wslpath -u C:\\`")
    # Conventional default, kept ONLY as a last resort for hosts where wslpath
    # itself is unavailable (e.g. WSL1, or interop disabled) but the mount root
    # still happens to be the common default.
    candidates.append("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if Path(candidate).exists():
            return candidate, attempts
        attempts.append(f"{candidate} does not exist")

    return None, attempts


class WindowsBackend:
    """Executes computer-use actions against a real Windows desktop via PowerShell."""

    name = "windows-wsl2"
    type_text_guarded = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self._ps_config = cfg.get("powershell_path")
        self._ps: str | None = None
        self._timeout = float(cfg.get("timeout", 90.0))
        # Diagnostic correlation only: stable across this backend's one-shot
        # bridge processes. Never part of public action/guard/receipt state.
        # WSL Python and the configured PowerShell executable can differ in
        # pointer width. Stay positive on both native IntPtr architectures.
        self._input_marker = secrets.randbelow((1 << 31) - 1) + 1

    def prepare_input_probe(self, duration_ms: int = 35000):
        """Prepare private stdin for an explicitly owned passive helper.

        Does not resolve paths, spawn processes, observe a desktop or reset a
        halt. The caller owns launch, readiness, deadline, result and reaping.
        """
        from .windows_input_diagnostics import prepare_probe

        return prepare_probe(self._input_marker, duration_ms)

    # -- capability probe (D1) ---------------------------------------------------
    def probe(self) -> ProbeResult:
        """Cheap PATH lookups only - no subprocess, no bridge round trip.

        This is the fix for D1: previously `mount()` unconditionally registered the
        `computer`/`desktop` tools, so a Linux box with no `powershell.exe` mounted a
        tool that could not possibly work. Now this check runs first.
        """
        if shutil.which("wslpath") is None:
            return ProbeResult(False, "wslpath not on PATH (not running under WSL2?)")
        ps, attempts = _which_powershell(self._ps_config)
        if ps is None:
            detail = "; ".join(attempts) if attempts else "no candidates found"
            return ProbeResult(
                False,
                f"powershell.exe not found (tried: {detail}); WSL<->Windows interop "
                "must be enabled (on by default in WSL2), or set tool config "
                "'powershell_path'",
            )
        self._ps = ps
        return ProbeResult(True)

    @property
    def powershell(self) -> str:
        if self._ps is None:
            ps, attempts = _which_powershell(self._ps_config)
            if ps is None:
                detail = "; ".join(attempts) if attempts else "no candidates found"
                raise BackendError(
                    f"powershell.exe not found (tried: {detail}). WSL<->Windows "
                    "interop must be enabled (it is on by default in WSL2). Set "
                    "the tool config key 'powershell_path' to override."
                )
            self._ps = ps
        return self._ps

    def raw(
        self, action: str, timeout: float | None = None, **params: Any
    ) -> dict[str, Any]:
        payload = {
            "action": action,
            **{k: v for k, v in params.items() if v is not None},
        }
        # Not a caller-selectable action parameter (including through raw()).
        payload.pop("_input_marker", None)
        if action in _INPUT_ACTIONS:
            payload["_input_marker"] = self._input_marker
        exchange = CURRENT_TARGET_CLOCK.get() if action in _INPUT_ACTIONS else None
        if exchange is not None:
            payload["target_clock_condition"] = exchange.condition
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(payload, fh)
            req_path = fh.name
        try:
            proc = subprocess.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    _translate(str(BRIDGE_PS1), "-w"),
                    "-RequestFile",
                    _translate(req_path, "-w"),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError(
                f"action {action!r} timed out after {timeout or self._timeout}s"
            ) from exc
        finally:
            Path(req_path).unlink(missing_ok=True)

        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if not lines:
            raise BackendError(
                f"bridge produced no output for {action!r} (rc={proc.returncode}): "
                f"{(proc.stderr or '').strip()[:400]}"
            )
        try:
            result = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise BackendError(
                f"bridge returned non-JSON for {action!r}: {lines[-1][:300]}"
            ) from exc
        if exchange is not None:
            exchange.finish(
                result, require_injection=action not in {"mouse_move", "set_clipboard"}
            )
        return result

    # -- Backend protocol ---------------------------------------------------------
    # -- coexistence: presence (docs/designs/coexistence.md) ---------------------
    def presence_observation(self) -> dict[str, Any]:
        result = self.raw("presence_idle", timeout=15)
        if not result.get("ok"):
            raise BackendError(result.get("error", "presence_idle failed"))
        return result.get("observation")

    def presence_idle_ms(self) -> float:
        """Milliseconds since the last input event (real OR synthetic) reached
        this Windows desktop, via `GetLastInputInfo`/`GetTickCount` on the far
        side of the WSL2 bridge - the `idle_source` the presence detector
        (`presence.PresenceMonitor`) reconciles against its own injection
        timestamps (\u00a75 of the coexistence design), the Windows counterpart to
        `LinuxX11Backend.presence_idle_ms`/`MacOSBackend.presence_idle_ms`.

        Unlike those two (a single in-process syscall, no subprocess), this
        crosses the WSL2->Win32 boundary via `powershell.exe`, exactly like
        every other action on this backend (\u00a75.5 - "Windows calls cross the
        WSL boundary via powershell.exe"). It is deliberately implemented as
        the CHEAPEST possible action dispatched through the SAME `bridge.ps1`
        script and the SAME `raw()` call path every other action already
        uses (`presence_idle` in `bridge.ps1`'s switch) - no new script, no
        new transport, no new mechanism - rather than inventing a persistent
        side-channel (that is Phase 4, tracked separately in `BACKLOG.md`).

        Honest cost note (\u00a75.5/\u00a712 "added powershell.exe spawns per action:
        zero" is a Linux/macOS-in-process property, not a Windows one): a
        correct presence read must be sampled fresh at call time - `idle_ms`
        cached from an earlier action's own response would be stale by
        however long has elapsed since that action completed, and plugging a
        stale sample into `PresenceMonitor.sample()`'s `now - idle_ms`
        reconciliation produces a WRONG (not just less-fresh) margin, which
        is the kind of silent, dangerous miss this whole feature exists to
        prevent (\u00a79.6 - never guess a fallback value). So this DOES add one
        additional `powershell.exe` spawn per guarded WRITE action on
        Windows (not per keystroke - `type_text` below does not accept a
        per-event guard, so this is capped at op granularity, matching
        \u00a75.5's \"Windows presence detection at op granularity is fine\").
        Eliminating that spawn requires either the Phase 4 persistent-
        PowerShell bridge, or moving the halt DECISION itself into
        `bridge.ps1` (a materially bigger redesign) - both out of scope here.

        Not part of the `Backend` protocol (`backend.py`) - looked up via
        `getattr` by whatever constructs the `CoexistenceGuard` for this
        backend, exactly like the Linux/macOS equivalents.
        """
        res = self.raw("presence_idle", timeout=15)
        if not res.get("ok"):
            raise BackendError(res.get("error", "presence_idle failed"))
        return float(res["idle_ms"])

    def session_state(self) -> tuple[str, str]:
        """Is this Windows session locked, has it no GUI session at all, or is
        it normally usable? The Windows counterpart to
        `MacOSBackend._macos_session_state` - see that method's docstring for
        the real incident (a locked screen misdiagnosed as a missing
        permission grant) this exists to prevent on the other platform.

        `bridge.ps1`'s dispatcher already refuses `capture`/mutating actions
        itself once locked/no-GUI is detected (`Get-SessionState`, checked
        once per bridge invocation - no extra `powershell.exe` round trip
        beyond the one every action already pays). This method exists for
        direct introspection (tests, `desktop` diagnostics) via the same
        `session_state` bridge action, dispatched through the identical
        `raw()` call path every other read here uses.

        NOT independently verified against a live Windows target for this
        change (see the accompanying report - no Windows host was reachable
        this session): `LogonUI.exe` presence-as-lock-signal and
        `Get-VirtualScreen` failure-as-no-GUI-signal are both well-documented
        techniques, not guesses, but neither has been exercised on real
        hardware here.
        """
        res = self.raw("session_state", timeout=15)
        if not res.get("ok"):
            return "unknown", res.get("error", "session_state failed")
        if res.get("no_gui_session"):
            return "no_gui_session", str(
                res.get("detail") or "Get-VirtualScreen failed"
            )
        if res.get("locked"):
            return "locked", str(res.get("detail") or "LogonUI.exe process present")
        return "unlocked", "no LogonUI.exe process; virtual screen reachable"

    def screen_geometry(self) -> ScreenGeometry:
        info = self.raw("screen_info", timeout=30)
        if not info.get("ok"):
            raise BackendError(f"screen_info failed: {info.get('error')}")
        return ScreenGeometry(
            int(info["width"]),
            int(info["height"]),
            int(info.get("x", 0)),
            int(info.get("y", 0)),
        )

    def list_monitors(self) -> list[MonitorInfo]:
        """Enumerate real monitors from `[System.Windows.Forms.Screen]::AllScreens`.

        The same `screen_info` bridge action already fetches this (`bridge.ps1`'s
        `screen_info` case populates `$out.screens` from `AllScreens` alongside the
        virtual-desktop bounding box `screen_geometry` uses) - it was simply being
        discarded by the Python side until now. `DeviceName` (e.g. `\\\\.\\DISPLAY3`)
        is used as `id`: Windows assigns these deterministically per adapter/output
        and they are stable across calls within a session, which is exactly what
        `target_monitor` config and `desktop.select_monitor` need to name a monitor
        and get the same one back.
        """
        info = self.raw("screen_info", timeout=30)
        if not info.get("ok"):
            raise BackendError(f"screen_info failed: {info.get('error')}")
        screens = info.get("screens")
        if not screens:
            raise BackendError(
                "screen_info returned no per-monitor data ('screens' missing or "
                "empty); cannot enumerate monitors on this Windows host"
            )
        monitors: list[MonitorInfo] = []
        for i, s in enumerate(screens):
            bounds = s.get("bounds")
            if not isinstance(bounds, (list, tuple)) or len(bounds) < 4:
                raise BackendError(f"malformed monitor entry in screen_info: {s!r}")
            x, y, w, h = (int(v) for v in bounds[:4])
            name = str(s.get("name") or "") or f"monitor-{i}"
            monitors.append(
                MonitorInfo(
                    id=name,
                    x=x,
                    y=y,
                    width=w,
                    height=h,
                    primary=bool(s.get("primary", False)),
                    name=name,
                )
            )
        return monitors

    def capture(self, region: tuple[int, int, int, int] | None = None) -> bytes:
        if region:
            x1, y1, x2, y2 = region
            res = self.raw(
                "zoom", coordinate=[x1, y1, max(x1 + 8, x2), max(y1 + 8, y2)]
            )
        else:
            res = self.raw("screenshot")
        if not res.get("ok"):
            raise BackendError(f"capture failed: {res.get('error')}")
        wsl_path = _translate(res["path"], "-u")
        try:
            return Path(wsl_path).read_bytes()
        finally:
            Path(wsl_path).unlink(missing_ok=True)

    def cursor_position(self) -> tuple[int, int]:
        res = self.raw("cursor_position")
        if not res.get("ok"):
            raise BackendError(res.get("error", "cursor_position failed"))
        sx, sy = res["coordinate"]
        return int(sx), int(sy)

    def move(self, x: int, y: int) -> None:
        res = self.raw("mouse_move", coordinate=[x, y])
        if not res.get("ok"):
            raise BackendError(res.get("error", "mouse_move failed"))

    def click(
        self, x: int | None, y: int | None, button: str = "left", count: int = 1
    ) -> None:
        action = _CLICK_ACTIONS.get((button, count))
        if action is None:
            raise BackendError(f"unsupported click: button={button!r} count={count!r}")
        kwargs: dict[str, Any] = {}
        if x is not None and y is not None:
            kwargs["coordinate"] = [x, y]
        res = self.raw(action, **kwargs)
        if not res.get("ok"):
            raise BackendError(res.get("error", f"{action} failed"))

    def mouse_down(self, x: int | None, y: int | None, button: str = "left") -> None:
        if button != "left":
            raise BackendError(f"mouse_down only supports 'left' (got {button!r})")
        kwargs: dict[str, Any] = (
            {"coordinate": [x, y]} if x is not None and y is not None else {}
        )
        res = self.raw("left_mouse_down", **kwargs)
        if not res.get("ok"):
            raise BackendError(res.get("error", "left_mouse_down failed"))

    def mouse_up(self, x: int | None, y: int | None, button: str = "left") -> None:
        if button != "left":
            raise BackendError(f"mouse_up only supports 'left' (got {button!r})")
        kwargs: dict[str, Any] = (
            {"coordinate": [x, y]} if x is not None and y is not None else {}
        )
        res = self.raw("left_mouse_up", **kwargs)
        if not res.get("ok"):
            raise BackendError(res.get("error", "left_mouse_up failed"))

    def drag(self, start: tuple[int, int] | None, end: tuple[int, int]) -> None:
        kwargs: dict[str, Any] = {"coordinate": list(end)}
        if start is not None:
            kwargs["start_coordinate"] = list(start)
        res = self.raw("left_click_drag", **kwargs)
        if not res.get("ok"):
            raise BackendError(res.get("error", "drag failed"))

    def scroll(self, x: int | None, y: int | None, direction: str, amount: int) -> None:
        kwargs: dict[str, Any] = {
            "scroll_direction": direction,
            "scroll_amount": amount,
        }
        if x is not None and y is not None:
            kwargs["coordinate"] = [x, y]
        res = self.raw("scroll", **kwargs)
        if not res.get("ok"):
            raise BackendError(res.get("error", "scroll failed"))

    def key(self, combo: str) -> None:
        res = self.raw("key", text=combo)
        if not res.get("ok"):
            raise BackendError(res.get("error", "key failed"))

    def hold_key(self, combo: str, duration: float) -> None:
        res = self.raw("hold_key", text=combo, duration=duration)
        if not res.get("ok"):
            raise BackendError(res.get("error", "hold_key failed"))

    def type_text(self, text: str, guard: Any = None) -> None:
        """Check and account around one bridge invocation.

        The controller supplies one character per guarded call, including
        zero pacing. Callback support does not qualify hardware detection
        timing inside PowerShell. No guard threshold is changed here.
        """
        if guard is not None:
            guard.before_event()
        with guarded_target_call(guard):
            res = self.raw(
                "type", text=text, timeout=max(self._timeout, 10 + len(text) * 0.05)
            )
        if not res.get("ok"):
            raise BackendError(res.get("error", "type failed"))
        if guard is not None:
            guard.after_event()

    def list_windows(self) -> WindowList:
        res = self.raw("list_windows")
        if not res.get("ok"):
            raise BackendError(res.get("error", "list_windows failed"))
        windows = [
            WindowInfo(
                str(w["handle"]),
                str(w["title"]),
                bool(w.get("minimized", False)),
                rect=_parse_rect(w.get("rect")),
            )
            for w in res.get("windows", [])
        ]
        return WindowList(windows, res.get("foreground"))

    def focus_window(self, handle: str) -> None:
        res = self.raw("focus_window", handle=handle)
        if not res.get("ok"):
            raise BackendError(res.get("error", "focus_window failed"))

    def get_clipboard(self) -> str:
        res = self.raw("get_clipboard")
        if not res.get("ok"):
            raise BackendError(res.get("error", "get_clipboard failed"))
        return res.get("text", "")

    def set_clipboard(self, text: str) -> None:
        res = self.raw("set_clipboard", text=text)
        if not res.get("ok"):
            raise BackendError(res.get("error", "set_clipboard failed"))

    def close(self) -> None:
        pass  # no persistent resources held (each action is its own subprocess)
