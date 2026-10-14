"""Regression tests for the pydantic serializer-warning suppression filter.

`hook-computer-use` installs a `warnings.filterwarnings(...)` ignore filter at
mount time to silence pydantic's own "Pydantic serializer warnings:" aggregate
`UserWarning` - noise inherent to writing image blocks as plain dicts (see
`_with_content`'s docstring), not a real defect. The filter's `message`
pattern was anchored (`re.match`) at the message's first line, but the token
it keyed on lives on the message's *second* line, and `.` does not cross a
newline without `re.DOTALL` - so the filter never matched anything, for the
module's entire lifetime, and every screenshot dumped the full warning wall
into ordinary interactive sessions.

These tests prove:

1. the real repro fires without a filter (the bug is real, not a test
   artifact);
2. the exact pattern this repo shipped never actually suppressed the real
   warning shape (a permanent characterization of the defect);
3. the fix - `_install_pydantic_serializer_warning_filter` - actually
   suppresses the real warning, driven through a real `model_dump()`, not a
   regex string match that would pass for the wrong reason;
4. the `category=`/`module=` narrowing does not over-suppress unrelated
   warnings, since `warnings.filterwarnings` mutates a process-global list;
5. the mount-time self-check detects both the historically-broken pattern
   and the current, fixed one;
6. `mount()` logs loudly (ERROR), never raises, when the self-check fails -
   an unsuppressed warning is cosmetic noise, not a functional break.

Every test that installs a real filter wraps it in `warnings.catch_warnings()`
so the process-global filter list this module mutates at mount time never
leaks between tests - the same mutation-scope concern the fix itself exists
to narrow.
"""

from __future__ import annotations

import asyncio
import sys
import warnings
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "hook-computer-use"))

import amplifier_module_hook_computer_use as hook_mod  # noqa: E402
from amplifier_core.message_models import Message  # noqa: E402


def _raw_screenshot_content() -> Any:
    """The exact raw-dict shape `_with_content` writes for a screenshot
    result - a plain dict standing in for `ImageBlock` so no `visibility:
    null` reaches the API. This is what actually triggers pydantic's real
    warning; a hand-picked string would prove nothing about the real code."""
    return [
        {"type": "text", "text": "screenshot captured (1280x720)"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "AAA",
            },
        },
    ]


class FakeHooks:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def register(self, event, handler, priority=100, name=None):
        self.handlers[event] = handler


class FakeCoordinator:
    """Minimal stand-in - `mount()` only calls `.hooks.register(...)` and
    `.get(...)` at mount time; provider/orchestrator lookups happen inside
    the returned handlers, which these tests never invoke."""

    def __init__(self) -> None:
        self.hooks = FakeHooks()

    def get(self, mount_point, name=None):
        return None


def _model_dump_warning_count(*, install_filter) -> int:
    """Real repro: construct a `Message`, overwrite `.content` with the raw
    dict shape, `model_dump()` it, and count the warnings that escape."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        if install_filter is not None:
            install_filter()
        message = Message(role="user", content="x")
        message.content = _raw_screenshot_content()  # type: ignore[assignment]
        message.model_dump()
    return len(caught)


def test_repro_warning_fires_with_no_filter_installed():
    """Baseline: without any filter, pydantic really does warn for this
    shape - confirms the repro is real, not an artifact of test setup."""
    assert _model_dump_warning_count(install_filter=None) == 1


def test_current_broken_pattern_never_matched_the_real_warning():
    """Characterizes the bug directly: the exact pattern this repo shipped
    (anchored `re.match`, no `DOTALL`) against the exact real warning shape.
    A permanent record of the defect, independent of the fix below - if this
    ever starts passing (count == 0), the historical bug has been
    reintroduced by someone reverting the DOTALL fix."""

    def install_historically_broken_filter() -> None:
        warnings.filterwarnings(
            "ignore", message=".*PydanticSerializationUnexpectedValue.*"
        )

    assert (
        _model_dump_warning_count(install_filter=install_historically_broken_filter)
        == 1
    )


def test_fixed_filter_suppresses_the_real_warning():
    """The actual fix: installs the real, current
    `_install_pydantic_serializer_warning_filter` and proves zero warnings
    escape for the real raw-dict content shape - not a regex string match."""
    assert (
        _model_dump_warning_count(
            install_filter=hook_mod._install_pydantic_serializer_warning_filter
        )
        == 0
    )


def test_scoping_does_not_suppress_unrelated_user_warnings():
    """The `category=UserWarning, module=pydantic...` narrowing exists so
    this filter's blast radius stays "pydantic's own serializer warning" -
    proves an unrelated `UserWarning`, raised from this test module (not
    pydantic), whose text happens to contain the same class-name substring,
    is NOT swallowed."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        hook_mod._install_pydantic_serializer_warning_filter()
        warnings.warn(
            "PydanticSerializationUnexpectedValue-shaped but unrelated warning",
            UserWarning,
            stacklevel=2,
        )
    assert len(caught) == 1


def test_self_check_passes_for_the_current_fixed_filter():
    assert hook_mod._pydantic_serializer_warning_is_suppressed() is True


def test_self_check_detects_the_historically_broken_pattern():
    """Proves the self-check is a real detector, not a tautology: force it
    to install the exact broken pattern this repo shipped and confirm it
    reports False - not just that it reports True today with the fix."""
    original = hook_mod._PYDANTIC_SERIALIZER_WARNING_MESSAGE_PATTERN
    hook_mod._PYDANTIC_SERIALIZER_WARNING_MESSAGE_PATTERN = (
        r".*PydanticSerializationUnexpectedValue.*"
    )
    try:
        assert hook_mod._pydantic_serializer_warning_is_suppressed() is False
    finally:
        hook_mod._PYDANTIC_SERIALIZER_WARNING_MESSAGE_PATTERN = original


def test_mount_logs_error_when_self_check_fails(monkeypatch, caplog):
    """Mount must fail LOUD (ERROR log), never raise: an unsuppressed
    warning is cosmetic noise, not a functional break, so mounting must
    still succeed. Forces the self-check to report broken without needing
    to actually corrupt the real filter."""
    monkeypatch.setattr(
        hook_mod, "_pydantic_serializer_warning_is_suppressed", lambda: False
    )
    coord = FakeCoordinator()
    with (
        warnings.catch_warnings(),
        caplog.at_level("ERROR", logger=hook_mod.__name__),
    ):
        # Own this invocation's loop; earlier tests may have cleared the
        # thread's default loop with asyncio.run().
        result = asyncio.run(hook_mod.mount(coord, {}))
    assert result["name"] == "hook-computer-use"  # mount still succeeded
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "self-check failure must be logged at ERROR"
    assert "did NOT suppress" in errors[0].getMessage()


def test_mount_does_not_log_error_when_self_check_passes(caplog):
    """No false alarms: mounting with the real, working filter must not
    emit the failure log."""
    coord = FakeCoordinator()
    with (
        warnings.catch_warnings(),
        caplog.at_level("ERROR", logger=hook_mod.__name__),
    ):
        asyncio.run(hook_mod.mount(coord, {}))
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert not errors
