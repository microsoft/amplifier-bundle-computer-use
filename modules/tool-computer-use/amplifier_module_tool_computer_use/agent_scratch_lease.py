"""POSIX lease markers for safely reclaiming remote-agent scratch directories.

The marker is deliberately a liveness lease, not a heartbeat. A directory is
eligible only when its valid marker has aged past the retention floor and the
marker can be locked exclusively. Thus age is never mistaken for idleness: an
old, live cooperating agent holds the lock and is retained.

This is best-effort hygiene, not a security boundary against a malicious
same-UID process that can race filesystem operations. The no-follow,
fd-relative operations protect ordinary cooperating sessions and avoid
following accidental symlinks. Remote agents are POSIX-only; unsupported
locking or filesystem primitives conservatively leave directories untouched.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
import time

AGENT_SCRATCH_DIR_PREFIX = "amplifier-cu-v2-agent-"
LEASE_FILENAME = ".amplifier-cu-agent-lease"
LEASE_MAGIC = b"amplifier-cu-agent-lease-v2\n"
STALE_AGENT_DIR_MIN_AGE_SECONDS = 24 * 60 * 60

logger = logging.getLogger(__name__)


def _secure_flags() -> int | None:
    """Return flags needed for no-follow directory-relative POSIX operations."""
    if os.name != "posix" or not all(
        hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW")
    ):
        return None
    return os.O_DIRECTORY | os.O_NOFOLLOW


def _flock_module():
    """Import fcntl lazily so importing the controller remains Windows-safe."""
    try:
        import fcntl
    except ImportError:
        return None
    if not all(hasattr(fcntl, name) for name in ("LOCK_EX", "LOCK_NB", "flock")):
        return None
    return fcntl


def _open_directory(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> int:
    flags = _secure_flags()
    if flags is None:
        raise OSError("secure POSIX directory operations unavailable")
    return os.open(
        path,
        os.O_RDONLY | flags | getattr(os, "O_CLOEXEC", 0),
        dir_fd=dir_fd,
    )


def _is_owned_private_directory(info: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o700
    )


def _is_valid_marker(info: os.stat_result, fd: int) -> bool:
    if not (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_size == len(LEASE_MAGIC)
    ):
        return False
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, len(LEASE_MAGIC) + 1) == LEASE_MAGIC
    except OSError:
        return False


def acquire_agent_lease(scratch_dir: str | os.PathLike[str]) -> int | None:
    """Create and hold a new lease marker, returning its open fd on success.

    A caller must retain the returned descriptor for its whole process
    lifetime. Once the exclusive lock is held, the descriptor is returned even
    when writing the marker is interrupted: closing it and continuing would
    leave a possibly valid but unlocked marker.
    """
    if _secure_flags() is None:
        return None
    fcntl = _flock_module()
    if fcntl is None:
        return None

    directory_fd: int | None = None
    marker_fd: int | None = None
    try:
        directory_fd = _open_directory(scratch_dir)
        if not _is_owned_private_directory(os.fstat(directory_fd)):
            return None
        marker_fd = os.open(
            LEASE_FILENAME,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            fcntl.flock(marker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        try:
            os.write(marker_fd, LEASE_MAGIC)
        except OSError:
            pass
        result = marker_fd
        marker_fd = None
        return result
    except OSError:
        return None
    finally:
        if marker_fd is not None:
            os.close(marker_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _current_is_same(
    parent_fd: int,
    name: str,
    directory_info: os.stat_result,
    marker_info: os.stat_result,
) -> bool:
    """Re-check path identities after taking the marker lock."""
    try:
        current_directory = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_inode(current_directory, directory_info):
            return False
        directory_fd = _open_directory(name, dir_fd=parent_fd)
        try:
            current_marker = os.stat(
                LEASE_FILENAME, dir_fd=directory_fd, follow_symlinks=False
            )
        finally:
            os.close(directory_fd)
        return _same_inode(current_marker, marker_info)
    except OSError:
        return False


def _remove_if_unlocked_stale(
    base_fd: int, name: str, now: float, minimum_age: float, fcntl
) -> bool:
    """Remove one validated old directory only while its marker lock is held."""
    directory_fd: int | None = None
    marker_fd: int | None = None
    try:
        entry_info = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
        if not _is_owned_private_directory(entry_info):
            return False
        directory_fd = _open_directory(name, dir_fd=base_fd)
        directory_info = os.fstat(directory_fd)
        if not _same_inode(
            entry_info, directory_info
        ) or not _is_owned_private_directory(directory_info):
            return False

        marker_fd = os.open(
            LEASE_FILENAME,
            os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
        marker_info = os.fstat(marker_fd)
        if not _is_valid_marker(marker_info, marker_fd):
            return False
        if now - marker_info.st_mtime < minimum_age:
            return False
        try:
            fcntl.flock(marker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False

        if not _is_valid_marker(os.fstat(marker_fd), marker_fd) or not _current_is_same(
            base_fd, name, directory_info, marker_info
        ):
            return False
        if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
            return False
        try:
            shutil.rmtree(name, dir_fd=base_fd)
        except OSError:
            return False
        try:
            os.stat(name, dir_fd=base_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    except OSError:
        return False
    finally:
        if marker_fd is not None:
            os.close(marker_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def sweep_stale_agent_dirs(
    *,
    temp_dir: str | None = None,
    max_age_seconds: float = STALE_AGENT_DIR_MIN_AGE_SECONDS,
) -> int:
    """Best-effort reclaim of stale, unlocked v2 scratch directories.

    ``max_age_seconds`` is retained for tests and callers, but never lowers
    the mandatory 24-hour marker retention floor. Legacy, unknown, malformed,
    unleased, or actively locked directories are all preserved.
    """
    if _secure_flags() is None:
        return 0
    fcntl = _flock_module()
    if fcntl is None:
        return 0

    base_fd: int | None = None
    try:
        base_path = (
            os.path.realpath(tempfile.gettempdir()) if temp_dir is None else temp_dir
        )
        base_fd = _open_directory(base_path)
        minimum_age = max(STALE_AGENT_DIR_MIN_AGE_SECONDS, max_age_seconds)
        now = time.time()
        removed = 0
        for name in os.listdir(base_fd):
            if name == AGENT_SCRATCH_DIR_PREFIX or not name.startswith(
                AGENT_SCRATCH_DIR_PREFIX
            ):
                continue
            if _remove_if_unlocked_stale(base_fd, name, now, minimum_age, fcntl):
                removed += 1
        if removed:
            logger.info("stale-dir sweep: removed %d orphaned agent dir(s)", removed)
        return removed
    except OSError as exc:
        logger.debug("stale-dir sweep skipped: %s", exc)
        return 0
    finally:
        if base_fd is not None:
            os.close(base_fd)
