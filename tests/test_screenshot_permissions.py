"""Unit tests: TASK 3.1 - screenshot files no longer rely on the inherited
umask, and no longer share one flat directory across every session.

`SHOT_DIR` is monkeypatched to a `tmp_path` for every test here - never the
real `~/.amplifier/computer-use/shots`, so these tests cannot pollute (or be
polluted by) a real machine's screenshot directory.
"""

from __future__ import annotations

import asyncio
import io
import json
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import amplifier_module_tool_computer_use as tool_mod
from amplifier_module_tool_computer_use import ComputerTool
from amplifier_module_tool_computer_use.backend import BackendError, ScreenGeometry


class _FakeCaptureBackend:
    """Real PIL-encoded PNG bytes on `capture()` - just enough of `Backend`
    for `ComputerTool.resolve_display()` + a `screenshot` action to work."""

    name = "linux-x11"

    def screen_geometry(self) -> ScreenGeometry:
        return ScreenGeometry(4, 4, 0, 0)

    def list_monitors(self):
        raise BackendError("no monitor enumeration on this fake")

    def capture(self, region=None) -> bytes:
        from PIL import Image

        img = Image.new("RGB", (4, 4), (10, 20, 30))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def cursor_position(self) -> tuple[int, int]:
        return (0, 0)

    def type_text(self, text, guard=None) -> None:  # pragma: no cover - unused
        pass

    def close(self) -> None:  # pragma: no cover
        pass


def _run(coro):
    return asyncio.run(coro)


def _make_computer(tmp_path, monkeypatch) -> ComputerTool:
    monkeypatch.setattr(tool_mod, "SHOT_DIR", tmp_path / "shots")
    backend = _FakeCaptureBackend()
    computer = ComputerTool(backend, {})
    computer.resolve_display()
    return computer


def test_screenshot_dir_and_file_are_owner_only(tmp_path, monkeypatch):
    computer = _make_computer(tmp_path, monkeypatch)

    result = _run(computer.execute({"action": "screenshot"}))

    assert result.success is True
    payload = json.loads(result.output)
    shot_path = Path(payload["images"][0])
    assert shot_path.exists()

    shots_root = tool_mod.SHOT_DIR
    session_dir = shots_root / computer._session_id
    assert shot_path.parent == session_dir

    # Parent shared directory, per-session subdirectory, and the file
    # itself: all owner-only (0700/0600), regardless of the process umask -
    # an explicit os.chmod is what guarantees this, not the mode requested
    # at mkdir/write time (see `execute()`'s docstring comment).
    assert stat.S_IMODE(shots_root.stat().st_mode) == tool_mod._PRIVATE_DIR_MODE
    assert stat.S_IMODE(session_dir.stat().st_mode) == tool_mod._PRIVATE_DIR_MODE
    assert stat.S_IMODE(shot_path.stat().st_mode) == tool_mod._PRIVATE_FILE_MODE


def test_permissions_hold_even_under_a_permissive_umask(tmp_path, monkeypatch):
    """The whole point of an explicit chmod: even a wide-open umask (e.g. a
    shared controller misconfigured to 000) must not leave the screenshot
    world-writable/readable."""
    import os

    old_umask = os.umask(0o000)
    try:
        computer = _make_computer(tmp_path, monkeypatch)
        result = _run(computer.execute({"action": "screenshot"}))
    finally:
        os.umask(old_umask)

    payload = json.loads(result.output)
    shot_path = Path(payload["images"][0])
    assert stat.S_IMODE(shot_path.stat().st_mode) == tool_mod._PRIVATE_FILE_MODE
    assert stat.S_IMODE(shot_path.parent.stat().st_mode) == tool_mod._PRIVATE_DIR_MODE


def test_two_computer_tool_instances_get_distinct_session_subdirectories(
    tmp_path, monkeypatch
):
    """Screenshots from two concurrent sessions must not land in the same
    flat shared directory - each `ComputerTool` gets its own scoped
    subdirectory."""
    computer1 = _make_computer(tmp_path, monkeypatch)
    computer2 = _make_computer(tmp_path, monkeypatch)
    assert computer1._session_id != computer2._session_id

    r1 = _run(computer1.execute({"action": "screenshot"}))
    r2 = _run(computer2.execute({"action": "screenshot"}))

    p1 = Path(json.loads(r1.output)["images"][0])
    p2 = Path(json.loads(r2.output)["images"][0])
    assert p1.parent != p2.parent
    assert p1.parent == tool_mod.SHOT_DIR / computer1._session_id
    assert p2.parent == tool_mod.SHOT_DIR / computer2._session_id


def test_prune_shots_walks_every_session_subdirectory(tmp_path, monkeypatch):
    """`_prune_shots()` must find expired files under ANY session
    subdirectory (`*/*.png`), not just a flat `*.png` glob at the top -
    otherwise nothing would ever be pruned again after this change."""
    monkeypatch.setattr(tool_mod, "SHOT_DIR", tmp_path / "shots")
    monkeypatch.setattr(tool_mod, "SHOT_TTL_SECONDS", 0)  # everything is "expired"

    session_dir = tool_mod.SHOT_DIR / "some-session-id"
    session_dir.mkdir(parents=True)
    old_file = session_dir / "old.png"
    old_file.write_bytes(b"\x89PNG\r\n")
    # Backdate mtime well past a zero-second TTL.
    import os
    import time

    os.utime(old_file, (time.time() - 10, time.time() - 10))

    tool_mod._prune_shots()

    assert not old_file.exists()
