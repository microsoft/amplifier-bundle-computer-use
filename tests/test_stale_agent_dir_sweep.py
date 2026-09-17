"""POSIX-only safety tests for v2 remote-agent scratch lease reclamation."""

from __future__ import annotations

import ast
import os
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use import agent_scratch_lease as lease
from amplifier_module_tool_computer_use.remote_agent import sweep_stale_agent_dirs

pytestmark = pytest.mark.skipif(
    lease._secure_flags() is None or lease._flock_module() is None,
    reason="requires POSIX no-follow operations and flock",
)


def _old_valid_dir(base: Path, name: str) -> Path:
    directory = base / f"{lease.AGENT_SCRATCH_DIR_PREFIX}{name}"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    marker = directory / lease.LEASE_FILENAME
    marker.write_bytes(lease.LEASE_MAGIC)
    os.chmod(marker, 0o600)
    stamp = time.time() - 48 * 60 * 60
    os.utime(marker, (stamp, stamp))
    return directory


def test_sweep_removes_only_old_unlocked_v2_leases(tmp_path: Path) -> None:
    stale = _old_valid_dir(tmp_path, "stale")
    fresh = _old_valid_dir(tmp_path, "fresh")
    os.utime(fresh / lease.LEASE_FILENAME, None)

    assert sweep_stale_agent_dirs(temp_dir=str(tmp_path)) == 1
    assert not stale.exists()
    assert fresh.exists()


def test_sweep_missing_explicit_temp_base_is_a_noop(tmp_path: Path) -> None:
    assert sweep_stale_agent_dirs(temp_dir=str(tmp_path / "does-not-exist")) == 0


def test_sweep_preserves_legacy_unknown_and_unleased_directories(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "amplifier-cu-agent-legacy"
    unknown = tmp_path / "unrelated-agent"
    incomplete = tmp_path / f"{lease.AGENT_SCRATCH_DIR_PREFIX}incomplete"
    for directory in (legacy, unknown, incomplete):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
    (legacy / lease.LEASE_FILENAME).write_bytes(lease.LEASE_MAGIC)

    assert sweep_stale_agent_dirs(temp_dir=str(tmp_path)) == 0
    assert legacy.exists()
    assert unknown.exists()
    assert incomplete.exists()


def test_sweep_preserves_bare_v2_prefix_even_with_valid_old_marker(
    tmp_path: Path,
) -> None:
    bare = tmp_path / lease.AGENT_SCRATCH_DIR_PREFIX
    bare.mkdir(mode=0o700)
    os.chmod(bare, 0o700)
    marker = bare / lease.LEASE_FILENAME
    marker.write_bytes(lease.LEASE_MAGIC)
    os.chmod(marker, 0o600)
    os.utime(marker, (time.time() - 48 * 60 * 60,) * 2)

    assert sweep_stale_agent_dirs(temp_dir=str(tmp_path)) == 0
    assert bare.exists()


@pytest.mark.parametrize(
    "kind",
    [
        "trailing",
        "fifo",
        "bad-mode",
        "directory-mode",
        "hardlink",
        "marker-symlink",
    ],
)
def test_sweep_preserves_invalid_marker_types_and_modes(
    tmp_path: Path, kind: str
) -> None:
    directory = _old_valid_dir(tmp_path, kind)
    marker = directory / lease.LEASE_FILENAME
    if kind == "trailing":
        marker.write_bytes(lease.LEASE_MAGIC + b"extra")
    elif kind == "fifo":
        marker.unlink()
        os.mkfifo(marker, 0o600)
    elif kind == "bad-mode":
        os.chmod(marker, 0o640)
    elif kind == "directory-mode":
        os.chmod(directory, 0o750)
    elif kind == "marker-symlink":
        outside = tmp_path / "outside-marker"
        outside.write_bytes(lease.LEASE_MAGIC)
        marker.unlink()
        marker.symlink_to(outside)
    else:
        linked = tmp_path / "linked-marker"
        linked.write_bytes(lease.LEASE_MAGIC)
        os.chmod(linked, 0o600)
        marker.unlink()
        os.link(linked, marker)
        os.utime(marker, (time.time() - 48 * 60 * 60,) * 2)

    assert sweep_stale_agent_dirs(temp_dir=str(tmp_path)) == 0
    assert directory.exists()


def test_sweep_rejects_explicit_symlink_base_and_candidate(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (outside / "keep").write_text("keep", encoding="utf-8")
    candidate = base / f"{lease.AGENT_SCRATCH_DIR_PREFIX}symlink"
    candidate.symlink_to(outside, target_is_directory=True)
    prefixed_file = base / f"{lease.AGENT_SCRATCH_DIR_PREFIX}ordinary-file"
    prefixed_file.write_text("not a directory", encoding="utf-8")
    base_link = tmp_path / "base-link"
    base_link.symlink_to(base, target_is_directory=True)

    assert sweep_stale_agent_dirs(temp_dir=str(base_link)) == 0
    assert sweep_stale_agent_dirs(temp_dir=str(base)) == 0
    assert candidate.is_symlink()
    assert prefixed_file.exists()
    assert (outside / "keep").read_text(encoding="utf-8") == "keep"


def test_default_temp_base_is_canonicalized_but_explicit_symlink_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    real_base = tmp_path / "real-base"
    real_base.mkdir(mode=0o700)
    link_base = tmp_path / "linked-base"
    link_base.symlink_to(real_base, target_is_directory=True)
    candidate = _old_valid_dir(real_base, "default-canonicalized")
    monkeypatch.setattr(lease.tempfile, "gettempdir", lambda: str(link_base))

    assert sweep_stale_agent_dirs() == 1
    assert not candidate.exists()


def test_requested_max_age_never_lowers_24_hour_retention(tmp_path: Path) -> None:
    directory = _old_valid_dir(tmp_path, "below-floor")
    os.utime(directory / lease.LEASE_FILENAME, (time.time() - 2 * 60 * 60,) * 2)

    assert sweep_stale_agent_dirs(temp_dir=str(tmp_path), max_age_seconds=0) == 0
    assert directory.exists()


def test_locked_48_hour_lease_is_preserved_as_live(tmp_path: Path) -> None:
    directory = tmp_path / f"{lease.AGENT_SCRATCH_DIR_PREFIX}live"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    fd = lease.acquire_agent_lease(directory)
    assert fd is not None
    try:
        marker = directory / lease.LEASE_FILENAME
        os.utime(marker, (time.time() - 48 * 60 * 60,) * 2)
        assert sweep_stale_agent_dirs(temp_dir=str(tmp_path)) == 0
        assert directory.exists()
    finally:
        os.close(fd)


def test_acquire_write_failure_keeps_its_locked_fd(tmp_path: Path, monkeypatch) -> None:
    import fcntl

    directory = tmp_path / f"{lease.AGENT_SCRATCH_DIR_PREFIX}write-failure"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    monkeypatch.setattr(
        lease.os, "write", lambda *_: (_ for _ in ()).throw(OSError("write failed"))
    )
    held_fd = lease.acquire_agent_lease(directory)
    assert held_fd is not None
    probe_fd = os.open(directory / lease.LEASE_FILENAME, os.O_RDWR)
    try:
        with pytest.raises(OSError):
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert (directory / lease.LEASE_FILENAME).read_bytes() == b""
    finally:
        os.close(probe_fd)
        os.close(held_fd)


def test_rmtree_failure_preserves_one_candidate_and_continues(
    tmp_path: Path, monkeypatch
) -> None:
    blocked = _old_valid_dir(tmp_path, "blocked")
    removable = _old_valid_dir(tmp_path, "removable")
    real_rmtree = lease.shutil.rmtree

    def flaky_rmtree(name, *, dir_fd):
        if name == blocked.name:
            raise OSError("simulated removal failure")
        return real_rmtree(name, dir_fd=dir_fd)

    monkeypatch.setattr(lease.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(
        lease.shutil.rmtree, "avoids_symlink_attacks", True, raising=False
    )

    assert sweep_stale_agent_dirs(temp_dir=str(tmp_path)) == 1
    assert blocked.exists()
    assert not removable.exists()


def test_unsupported_locking_preserves_candidate(tmp_path: Path, monkeypatch) -> None:
    directory = _old_valid_dir(tmp_path, "unsupported")
    monkeypatch.setattr(lease, "_flock_module", lambda: None)
    assert sweep_stale_agent_dirs(temp_dir=str(tmp_path)) == 0
    assert directory.exists()
    assert lease.acquire_agent_lease(directory) is None


def test_unsupported_secure_primitives_preserve_scratch_without_marker(
    tmp_path: Path, monkeypatch
) -> None:
    directory = tmp_path / f"{lease.AGENT_SCRATCH_DIR_PREFIX}unsupported-primitives"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    monkeypatch.setattr(lease, "_secure_flags", lambda: None)

    assert lease.acquire_agent_lease(directory) is None
    assert directory.exists()
    assert not (directory / lease.LEASE_FILENAME).exists()


def test_owner_mismatch_preserves_candidate(tmp_path: Path, monkeypatch) -> None:
    directory = _old_valid_dir(tmp_path, "owner-mismatch")
    monkeypatch.setattr(lease.os, "geteuid", lambda: -1)

    assert sweep_stale_agent_dirs(temp_dir=str(tmp_path)) == 0
    assert directory.exists()


def test_existing_marker_does_not_get_overwritten(tmp_path: Path) -> None:
    directory = tmp_path / f"{lease.AGENT_SCRATCH_DIR_PREFIX}existing-marker"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    marker = directory / lease.LEASE_FILENAME
    marker.write_bytes(b"legacy marker")
    os.chmod(marker, 0o600)

    assert lease.acquire_agent_lease(directory) is None
    assert marker.read_bytes() == b"legacy marker"


def test_flock_failure_after_marker_creation_leaves_invalid_marker(
    tmp_path: Path, monkeypatch
) -> None:
    directory = tmp_path / f"{lease.AGENT_SCRATCH_DIR_PREFIX}flock-failure"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)

    class FailingFlock:
        LOCK_EX = 1
        LOCK_NB = 2

        @staticmethod
        def flock(fd: int, flags: int) -> None:
            raise OSError("simulated flock failure")

    monkeypatch.setattr(lease, "_flock_module", lambda: FailingFlock)
    assert lease.acquire_agent_lease(directory) is None
    assert (directory / lease.LEASE_FILENAME).read_bytes() == b""


def test_full_magic_write_then_error_retains_lock_and_prevents_sweep(
    tmp_path: Path, monkeypatch
) -> None:
    real_write = lease.os.write
    directory = tmp_path / f"{lease.AGENT_SCRATCH_DIR_PREFIX}partial-write"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)

    def write_then_error(fd: int, data: bytes) -> int:
        real_write(fd, data)
        raise OSError("simulated post-write error")

    monkeypatch.setattr(lease.os, "write", write_then_error)
    held_fd = lease.acquire_agent_lease(directory)
    assert held_fd is not None
    try:
        marker = directory / lease.LEASE_FILENAME
        assert marker.read_bytes() == lease.LEASE_MAGIC
        os.utime(marker, (time.time() - 48 * 60 * 60,) * 2)
        assert sweep_stale_agent_dirs(temp_dir=str(tmp_path)) == 0
        assert directory.exists()
    finally:
        os.close(held_fd)


def test_helper_has_no_module_level_fcntl_import() -> None:
    tree = ast.parse(Path(lease.__file__).read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        and any(alias.name == "fcntl" for alias in node.names)
        for node in tree.body
    )
    assert stat.S_ISREG(os.stat(lease.__file__).st_mode)
