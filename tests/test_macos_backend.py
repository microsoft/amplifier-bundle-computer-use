"""Unit tests for `MacOSBackend` - runs on plain Linux CI with no Mac present.

Everything here is pure logic or a faked `Quartz` module - no real Core Graphics call
is ever made. Real end-to-end verification against a live Mac (Retina scale factor,
monitor enumeration, capture pixel content, Accessibility TCC status) lives in the
top-level report, not here - these tests guard the coordinate-conversion and combo-
parsing logic that verification depends on being correct, the same division of labor
`test_geometry.py` and `test_backend_monitors.py` already establish for the other two
backends.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

import pytest
from amplifier_module_tool_computer_use import macos
from amplifier_module_tool_computer_use.backend import BackendError
from amplifier_module_tool_computer_use.macos import (
    _CG_FLAG_ALTERNATE,
    _CG_FLAG_COMMAND,
    _CG_FLAG_CONTROL,
    _CG_FLAG_SHIFT,
    MacOSBackend,
    _combo_flags_and_keycode,
)

# -- probe() -------------------------------------------------------------------
#
# This test suite runs on Linux (no Mac present, no pyobjc installed) - which means
# `MacOSBackend.probe()`'s very first check (platform) and its Quartz-import check
# are exercised for real here, not mocked. That is the point: these are exactly the
# two guards that must fire cleanly on every non-Mac CI/dev machine so this addition
# never breaks a Linux or Windows install (see module docstring's `_IMPORT_ERROR`
# discussion and D1 in `backend.py`).


def test_probe_unavailable_on_non_darwin_platform():
    backend = MacOSBackend({})
    result = backend.probe()
    assert result.available is False
    assert "darwin" in result.reason.lower() or "macos" in result.reason.lower()


def test_probe_unavailable_when_quartz_not_importable(monkeypatch):
    """On this box Quartz genuinely fails to import (no pyobjc installed) - this
    confirms `probe()` reports that specific, actionable reason rather than crashing,
    once the platform check itself is satisfied."""
    monkeypatch.setattr(macos.sys, "platform", "darwin")
    assert macos.Quartz is None  # true on any non-Mac dev/CI box
    backend = MacOSBackend({})
    result = backend.probe()
    assert result.available is False
    assert "quartz" in result.reason.lower()


def test_probe_never_raises_on_arbitrary_platform_string(monkeypatch):
    """`probe()` must never raise - even for an unexpected `sys.platform` value."""
    monkeypatch.setattr(macos.sys, "platform", "some-future-os")
    backend = MacOSBackend({})
    result = backend.probe()
    assert result.available is False


# -- registry probe order -------------------------------------------------------


def test_macos_registered_after_windows_and_linux_by_default():
    from amplifier_module_tool_computer_use.linux_x11 import LinuxX11Backend
    from amplifier_module_tool_computer_use.registry import BACKEND_FACTORIES
    from amplifier_module_tool_computer_use.windows import WindowsBackend

    assert BACKEND_FACTORIES.index(WindowsBackend) < BACKEND_FACTORIES.index(
        MacOSBackend
    )
    assert BACKEND_FACTORIES.index(LinuxX11Backend) < BACKEND_FACTORIES.index(
        MacOSBackend
    )


# -- combo parsing (key/hold_key) - pure logic, zero Quartz dependency ----------


def test_combo_single_key_no_modifiers():
    flags, keycode = _combo_flags_and_keycode("Return")
    assert flags == 0
    assert keycode == 0x24


def test_combo_single_modifier_plus_key():
    flags, keycode = _combo_flags_and_keycode("cmd+s")
    assert flags == _CG_FLAG_COMMAND
    assert keycode == 0x01  # kVK_ANSI_S


def test_combo_multiple_modifiers_combine_via_bitwise_or():
    flags, keycode = _combo_flags_and_keycode("ctrl+shift+a")
    assert flags == (_CG_FLAG_CONTROL | _CG_FLAG_SHIFT)
    assert keycode == 0x00  # kVK_ANSI_A


def test_combo_option_and_alt_are_the_same_modifier():
    f1, _ = _combo_flags_and_keycode("alt+a")
    f2, _ = _combo_flags_and_keycode("option+a")
    assert f1 == f2 == _CG_FLAG_ALTERNATE


def test_combo_case_insensitive():
    flags, keycode = _combo_flags_and_keycode("CMD+S")
    assert flags == _CG_FLAG_COMMAND
    assert keycode == 0x01


def test_combo_rejects_empty_string():
    with pytest.raises(BackendError, match="empty"):
        _combo_flags_and_keycode("")


def test_combo_rejects_unknown_key_name():
    with pytest.raises(BackendError, match="unknown key name"):
        _combo_flags_and_keycode("cmd+notarealkey")


def test_combo_rejects_modifiers_only():
    with pytest.raises(BackendError, match="only modifiers"):
        _combo_flags_and_keycode("cmd+shift")


def test_combo_function_key():
    _, keycode = _combo_flags_and_keycode("F1")
    assert keycode == 0x7A


# -- coordinate conversion: pixel <-> point, with a faked Quartz ----------------
#
# The single-display-Retina scenario below is exactly the configuration this backend
# was verified against for real (see the top-level report): the built-in display of
# a MacBook Pro, no external monitors. Mixed-DPI multi-monitor stitching is a
# documented, un-exercised limitation (see `_monitor_infos`'s docstring) - not tested
# here because pinning a "correct" answer for an inherently approximate case would
# be testing an opinion, not a contract.


class _FakeRect:
    def __init__(self, x: float, y: float, w: float, h: float) -> None:
        self.origin = types.SimpleNamespace(x=x, y=y)
        self.size = types.SimpleNamespace(width=w, height=h)


class _FakeQuartz:
    """Stand-in for the `Quartz` module, exposing only what `MacOSBackend`'s
    geometry/coordinate helpers touch."""

    def __init__(self, displays: list[dict]) -> None:
        self._displays = {d["id"]: d for d in displays}

    def CGGetActiveDisplayList(self, max_displays, _arr, _cnt):
        ids = list(self._displays.keys())
        return (0, ids, len(ids))

    def CGDisplayBounds(self, display_id):
        d = self._displays[display_id]
        return _FakeRect(*d["bounds"])

    def CGDisplayCopyDisplayMode(self, display_id):
        # Real Quartz returns an opaque CGDisplayModeRef; this fake just returns
        # the display_id itself as a token the two accessors below can look up.
        return display_id

    def CGDisplayModeGetPixelWidth(self, mode_token):
        return self._displays[mode_token]["pixel_w"]

    def CGDisplayModeGetPixelHeight(self, mode_token):
        return self._displays[mode_token]["pixel_h"]

    def CGMainDisplayID(self):
        return next(did for did, d in self._displays.items() if d.get("main"))

    def CGPointMake(self, x, y):
        return types.SimpleNamespace(x=x, y=y)


@pytest.fixture
def retina_backend(monkeypatch):
    """One display: 1440x900 points, 2880x1800 physical pixels - a 2x Retina
    backing scale, at the virtual-desktop origin. Mirrors a real MacBook Pro
    built-in display (exact point/pixel numbers vary by model; the 2x ratio and
    zero origin are what matters for this test)."""
    fake = _FakeQuartz(
        [
            {
                "id": 1,
                "bounds": (0, 0, 1440, 900),
                "pixel_w": 2880,
                "pixel_h": 1800,
                "main": True,
            }
        ]
    )
    monkeypatch.setattr(macos, "Quartz", fake)
    return MacOSBackend({})


def test_display_scale_is_measured_not_assumed(retina_backend):
    assert MacOSBackend._display_scale(1) == 2.0


def test_monitor_infos_reports_physical_pixel_dimensions(retina_backend):
    monitors = retina_backend._monitor_infos()
    assert len(monitors) == 1
    m = monitors[0]
    assert m.id == "1"
    assert (m.x, m.y) == (0, 0)
    assert (m.width, m.height) == (2880, 1800)  # physical pixels, not 1440x900 points
    assert m.primary is True


def test_list_monitors_matches_monitor_infos(retina_backend):
    assert retina_backend.list_monitors() == retina_backend._monitor_infos()


def test_covering_monitor_for_pixel_finds_the_only_display(retina_backend):
    m = retina_backend._covering_monitor_for_pixel(100, 100)
    assert m.id == "1"


def test_pixel_to_point_divides_by_backing_scale(retina_backend):
    """The core Retina trap this backend exists to get right: a physical-pixel
    SCREEN coordinate must be *halved* (divided by the 2x backing scale) before
    it is valid input to any `CGEvent*` call, which operates in logical points."""
    point = retina_backend._pixel_to_point(200, 100)
    assert (point.x, point.y) == (100.0, 50.0)


def test_point_to_pixel_multiplies_by_backing_scale():
    """The inverse: a point reported by `CGEventGetLocation` must be *doubled*
    to become a valid physical-pixel SCREEN coordinate (`cursor_position`'s
    contract)."""
    fake = _FakeQuartz(
        [
            {
                "id": 1,
                "bounds": (0, 0, 1440, 900),
                "pixel_w": 2880,
                "pixel_h": 1800,
                "main": True,
            }
        ]
    )
    backend = MacOSBackend({})
    import amplifier_module_tool_computer_use.macos as macos_mod

    macos_mod.Quartz = fake
    x, y = backend._point_to_pixel(100.0, 50.0)
    assert (x, y) == (200, 100)


def test_pixel_to_point_round_trips_through_point_to_pixel(retina_backend):
    for px, py in [(0, 0), (1, 1), (2879, 1799), (1440, 900)]:
        point = retina_backend._pixel_to_point(px, py)
        rx, ry = retina_backend._point_to_pixel(point.x, point.y)
        assert abs(rx - px) <= 1
        assert abs(ry - py) <= 1


# -- _window_rect (real-hardware regression, 2026-08-04) ------------------------
#
# `kCGWindowBounds` is a `CFDictionary`, toll-free-bridged by PyObjC into a real
# Objective-C `NSDictionary` - NOT a Python `dict` subclass. `_ProxyNSDictionary`
# below reproduces exactly that: `.get()`/`.keys()`/`__getitem__` all work (the
# same surface every real caller in this module relies on) but
# `isinstance(x, dict)` is `False`, matching `type(bounds)` printing
# `<objective-c class '__NSDictionaryI'>` when this was verified live against a
# real Mac. A plain `{...}` literal (as every OTHER test in this file uses for
# `kCGWindowBounds`) does NOT reproduce the bug - `isinstance({...}, dict)` is
# `True` - which is exactly why the real defect shipped past 594 passing tests.


class _ProxyNSDictionary:
    """Duck-typed dict-alike that deliberately does NOT subclass `dict` -
    reproducing the real `NSDictionary` bridge type `_window_rect` receives on
    an actual Mac."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()


def test_window_rect_accepts_real_nsdictionary_bounds_not_just_plain_dict(
    retina_backend,
):
    """The exact real-hardware defect: `isinstance(bounds, dict)` was `False`
    for the genuine `NSDictionary` PyObjC hands back, so `_window_rect`
    returned `None` for every window, every call. This must now resolve a
    real rect instead of `None` for a non-`dict` but dict-*like* `bounds`."""
    bounds = _ProxyNSDictionary({"X": 100.0, "Y": 50.0, "Width": 200.0, "Height": 80.0})
    assert not isinstance(bounds, dict)  # this IS what real Quartz hands back

    rect = retina_backend._window_rect(bounds)

    assert rect is not None
    left, top, right, bottom = rect
    assert (left, top) == (200, 100)  # 2x Retina scale, per retina_backend fixture
    assert (right - left, bottom - top) == (400, 160)


def test_window_rect_still_accepts_plain_dict_bounds(retina_backend):
    """Existing behavior (a real `dict`, as every other test/mocked caller in
    this file uses) must keep working after dropping the `isinstance` gate."""
    bounds = {"X": 10.0, "Y": 20.0, "Width": 30.0, "Height": 40.0}
    rect = retina_backend._window_rect(bounds)
    assert rect is not None


def test_window_rect_none_for_none_bounds(retina_backend):
    assert retina_backend._window_rect(None) is None


def test_window_rect_none_for_bounds_missing_keys(retina_backend):
    assert retina_backend._window_rect(_ProxyNSDictionary({"X": 1.0})) is None


def test_screen_geometry_single_display(retina_backend):
    geo = retina_backend.screen_geometry()
    assert (geo.width, geo.height) == (2880, 1800)
    assert (geo.origin_x, geo.origin_y) == (0, 0)


def test_list_monitors_fails_loud_on_zero_displays(monkeypatch):
    fake = _FakeQuartz([])
    monkeypatch.setattr(macos, "Quartz", fake)
    backend = MacOSBackend({})
    with pytest.raises(BackendError, match="zero active displays"):
        backend.list_monitors()


def test_covering_monitor_falls_back_to_primary_when_point_outside_all_monitors(
    monkeypatch,
):
    """Defensive fallback only (see `_covering_monitor_for_pixel` docstring): real
    callers always pass coordinates already clamped by `geometry.Display.to_screen`,
    but this must not raise or crash if it somehow receives an out-of-bounds point."""
    fake = _FakeQuartz(
        [
            {
                "id": 1,
                "bounds": (0, 0, 1440, 900),
                "pixel_w": 2880,
                "pixel_h": 1800,
                "main": True,
            }
        ]
    )
    monkeypatch.setattr(macos, "Quartz", fake)
    backend = MacOSBackend({})
    m = backend._covering_monitor_for_pixel(999999, 999999)
    assert m.id == "1"  # falls back to primary, does not raise


# -- probe() with a non-empty (mocked) display list -----------------------------


def test_probe_available_when_darwin_quartz_and_displays_present(monkeypatch):
    fake = _FakeQuartz(
        [
            {
                "id": 1,
                "bounds": (0, 0, 1440, 900),
                "pixel_w": 2880,
                "pixel_h": 1800,
                "main": True,
            }
        ]
    )
    monkeypatch.setattr(macos.sys, "platform", "darwin")
    monkeypatch.setattr(macos, "Quartz", fake)
    backend = MacOSBackend({})
    result = backend.probe()
    assert result.available is True


def test_probe_unavailable_when_zero_displays(monkeypatch):
    fake = _FakeQuartz([])
    monkeypatch.setattr(macos.sys, "platform", "darwin")
    monkeypatch.setattr(macos, "Quartz", fake)
    backend = MacOSBackend({})
    result = backend.probe()
    assert result.available is False
    assert "zero active displays" in result.reason


# -- coexistence: presence_idle_ms / current_target (TASK 1) -------------------
#
# Everything below is a faked `Quartz` module - no real Core Graphics call is
# ever made, and no macOS hardware is touched (see the accompanying report:
# these behaviors are reasoned from documented Apple behavior and unit-tested
# here, but have not been independently re-verified against a live Mac).


class _FakePresenceQuartz:
    """Stand-in for `Quartz`, exposing only what `presence_idle_ms`/
    `current_target` touch."""

    def __init__(
        self, idle_seconds: float = 0.0, windows: list[dict] | None = None
    ) -> None:
        self.idle_seconds = idle_seconds
        self.windows = windows if windows is not None else []
        self.last_event_source_state = None
        self.last_event_type = None

    def CGEventSourceSecondsSinceLastEventType(self, state, event_type):
        self.last_event_source_state = state
        self.last_event_type = event_type
        return self.idle_seconds

    kCGEventSourceStateHIDSystemState = "HIDSystemState"
    kCGAnyInputEventType = "AnyInputEventType"

    def CGWindowListCopyWindowInfo(self, options, relative_to):
        return list(self.windows)

    kCGWindowListOptionOnScreenOnly = 1
    kCGWindowListExcludeDesktopElements = 16
    kCGNullWindowID = 0


def test_presence_idle_ms_converts_seconds_to_milliseconds(monkeypatch):
    fake = _FakePresenceQuartz(idle_seconds=0.0123)
    monkeypatch.setattr(macos, "Quartz", fake)
    backend = MacOSBackend({})

    idle_ms = backend.presence_idle_ms()

    assert idle_ms == pytest.approx(12.3, abs=0.001)


def test_presence_idle_ms_uses_hid_system_state_and_any_input_event(monkeypatch):
    """The two constants matter: HID system state (not combined-session
    state) is what a real human's hardware AND this backend's own
    `CGEventPost` calls both feed - see `presence_idle_ms`'s docstring."""
    fake = _FakePresenceQuartz(idle_seconds=1.0)
    monkeypatch.setattr(macos, "Quartz", fake)
    backend = MacOSBackend({})

    backend.presence_idle_ms()

    assert fake.last_event_source_state == fake.kCGEventSourceStateHIDSystemState
    assert fake.last_event_type == fake.kCGAnyInputEventType


def test_current_target_returns_frontmost_normal_layer_window(monkeypatch):
    fake = _FakePresenceQuartz(
        windows=[
            {"kCGWindowLayer": 0, "kCGWindowNumber": 4242},
            {"kCGWindowLayer": 0, "kCGWindowNumber": 99},
        ]
    )
    monkeypatch.setattr(macos, "Quartz", fake)
    backend = MacOSBackend({})

    assert backend.current_target() == "4242"


def test_current_target_skips_non_normal_layer_windows():
    """Menu bar / dock / overlay windows (`kCGWindowLayer != 0`) must not be
    reported as the frontmost target - matches `list_windows()`'s own
    filtering."""
    fake = _FakePresenceQuartz(
        windows=[
            {"kCGWindowLayer": 25, "kCGWindowNumber": 1},  # e.g. the dock
            {"kCGWindowLayer": 0, "kCGWindowNumber": 777},
        ]
    )
    import amplifier_module_tool_computer_use.macos as macos_mod

    macos_mod.Quartz = fake
    backend = MacOSBackend({})

    assert backend.current_target() == "777"


def test_current_target_returns_none_when_enumeration_fails():
    """§8.6: a read failure must report `None` (-> `TargetBinding` reports
    "unverified") rather than raising or guessing a handle."""

    class _BoomQuartz:
        def CGWindowListCopyWindowInfo(self, options, relative_to):
            raise RuntimeError("Screen Recording revoked")

        kCGWindowListOptionOnScreenOnly = "OnScreenOnly"
        kCGWindowListExcludeDesktopElements = "ExcludeDesktopElements"
        kCGNullWindowID = 0

    import amplifier_module_tool_computer_use.macos as macos_mod

    macos_mod.Quartz = _BoomQuartz()
    backend = MacOSBackend({})

    assert backend.current_target() is None


def test_current_target_returns_none_when_zero_windows():
    fake = _FakePresenceQuartz(windows=[])
    import amplifier_module_tool_computer_use.macos as macos_mod

    macos_mod.Quartz = fake
    backend = MacOSBackend({})

    assert backend.current_target() is None


# -- coexistence: per-character type_text guard wiring (TASK 1) ---------------


class _FakeTypeQuartz:
    """Stand-in for `Quartz`, exposing only what `type_text` (and the
    `_ensure_input_trusted` gate it calls) touch."""

    kCGHIDEventTap = "HIDEventTap"

    def __init__(self) -> None:
        self.posted_strings: list[str] = []

    def CGEventCreateKeyboardEvent(self, source, keycode, key_down):
        return {"keycode": keycode, "key_down": key_down, "unicode": ""}

    def CGEventSetFlags(self, event, flags):
        pass

    def CGEventKeyboardSetUnicodeString(self, event, length, text):
        event["unicode"] = text

    def CGEventPost(self, tap, event):
        self.posted_strings.append(event["unicode"])


class _FakeGuard:
    """Records before_event()/after_event() call order - no real
    `CoexistenceGuard`/`PresenceMonitor` needed to prove the per-character
    wiring shape itself."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def before_event(self) -> None:
        self.calls.append("before")

    def after_event(self) -> None:
        self.calls.append("after")


def test_type_text_posts_one_event_pair_per_character(monkeypatch):
    fake = _FakeTypeQuartz()
    monkeypatch.setattr(macos, "Quartz", fake)
    monkeypatch.setattr(MacOSBackend, "_ensure_ready_for_input", lambda self: None)
    backend = MacOSBackend({})

    backend.type_text("ab")

    # Two characters -> two down events + two up events, one CGEvent PAIR
    # per character (not one CGEvent for the whole string) - this is what
    # makes per-keystroke guard checks meaningful at all (§5.2). Since the
    # 2026-08-04 fix, characters travel as a real keycode, not a
    # `CGEventKeyboardSetUnicodeString` payload - see `_FakeRealKeycodeQuartz`
    # tests below for the keycode/flags assertions; this fake's
    # `posted_strings` stays empty by construction now.
    assert len(fake.posted_strings) == 4


def test_type_text_calls_guard_before_and_after_each_character(monkeypatch):
    fake = _FakeTypeQuartz()
    monkeypatch.setattr(macos, "Quartz", fake)
    monkeypatch.setattr(MacOSBackend, "_ensure_ready_for_input", lambda self: None)
    backend = MacOSBackend({})
    guard = _FakeGuard()

    backend.type_text("xyz", guard=guard)

    assert guard.calls == [
        "before",
        "after",
        "before",
        "after",
        "before",
        "after",
    ]


def test_type_text_with_no_guard_skips_every_guard_call(monkeypatch):
    """Omitting `guard` (the default) must not invoke anything guard-shaped -
    existing callers with no guard are unaffected."""
    fake = _FakeTypeQuartz()
    monkeypatch.setattr(macos, "Quartz", fake)
    monkeypatch.setattr(MacOSBackend, "_ensure_ready_for_input", lambda self: None)
    backend = MacOSBackend({})

    backend.type_text("hi")  # guard=None (default) - must not raise

    assert len(fake.posted_strings) == 4


# -- real-hardware defect: type_text silently no-ops (found 2026-08-04) -------
#
# Live-hardware verification against an idle, unlocked Mac (screenshots
# compared before/after, not just "CGEventPost did not raise") proved that
# the PREVIOUS implementation - a keycode-0 `CGEventCreateKeyboardEvent` with
# only `CGEventKeyboardSetUnicodeString` carrying the character, posted to
# `kCGHIDEventTap` - posts successfully and delivers NOTHING to Spotlight's
# search field: the field was byte-for-byte unchanged after `type_text`, on
# a direct in-process call AND through the full remote wire path, at both
# `kCGHIDEventTap` and `kCGSessionEventTap`. `key()` - which instead posts a
# REAL, non-zero virtual keycode plus `CGEventFlags` for any held modifiers -
# reliably lands (verified: a single `key("a")` correctly triggered
# Spotlight's own autocomplete to "azure VPN Client"). `type_text` now
# routes through that same proven mechanism instead of the unicode-string
# trick - these tests pin the new, verified behavior and FAIL against the
# old keycode-0 implementation.
class _FakeRealKeycodeQuartz:
    """Stand-in for `Quartz` that also records `CGEventSetFlags` calls, so
    tests can assert on the REAL (keycode, flags) pair each character
    resolves to - the old implementation always posted `(0, no-flags-call)`
    with the character living only in `CGEventKeyboardSetUnicodeString`."""

    kCGHIDEventTap = "HIDEventTap"

    def __init__(self) -> None:
        self.posted: list[tuple[int, bool, int]] = []  # (keycode, key_down, flags)
        self.unicode_calls: int = 0

    def CGEventCreateKeyboardEvent(self, source, keycode, key_down):
        return {"keycode": keycode, "key_down": key_down, "flags": 0}

    def CGEventSetFlags(self, event, flags):
        event["flags"] = flags

    def CGEventKeyboardSetUnicodeString(self, event, length, text):
        # The old technique this backend must no longer rely on for
        # resolvable characters - counted so tests can assert it is unused.
        self.unicode_calls += 1

    def CGEventPost(self, tap, event):
        self.posted.append((event["keycode"], event["key_down"], event["flags"]))


def test_type_text_uses_real_keycodes_not_the_unicode_string_technique(monkeypatch):
    fake = _FakeRealKeycodeQuartz()
    monkeypatch.setattr(macos, "Quartz", fake)
    monkeypatch.setattr(MacOSBackend, "_ensure_ready_for_input", lambda self: None)
    backend = MacOSBackend({})
    keycode_a = macos._KEYCODE["a"]

    backend.type_text("a")

    # Old (defective) implementation always posts keycode 0 and relies on
    # CGEventKeyboardSetUnicodeString - proven on real hardware to post
    # successfully while delivering nothing. The fix posts the SAME real,
    # non-zero keycode `key("a")` already uses (proven to land).
    assert fake.posted == [(keycode_a, True, 0), (keycode_a, False, 0)]
    assert fake.unicode_calls == 0


def test_type_text_sets_shift_flag_for_uppercase_and_shifted_symbols(monkeypatch):
    fake = _FakeRealKeycodeQuartz()
    monkeypatch.setattr(macos, "Quartz", fake)
    monkeypatch.setattr(MacOSBackend, "_ensure_ready_for_input", lambda self: None)
    backend = MacOSBackend({})
    keycode_a = macos._KEYCODE["a"]
    keycode_one = macos._KEYCODE["1"]

    backend.type_text("A!")

    # 'A' = shift + the same keycode as 'a'; '!' = shift + the '1' keycode -
    # both real, verified keys, never keycode 0.
    keycodes_and_flags = [(kc, flags) for kc, _down, flags in fake.posted]
    assert (keycode_a, _CG_FLAG_SHIFT) in keycodes_and_flags
    assert (keycode_one, _CG_FLAG_SHIFT) in keycodes_and_flags
    assert fake.unicode_calls == 0


def test_type_text_refuses_unsupported_characters_before_typing_anything(monkeypatch):
    """No US-ANSI keycode exists for these characters - per the no-fallback
    rule, `type_text` must not silently retry them through the unverified
    unicode-string technique and call it success. It must fail loud, and it
    must not have posted ANY event for the supported characters that
    preceded the unsupported one either (atomic: no partial, silent typing)."""
    fake = _FakeRealKeycodeQuartz()
    monkeypatch.setattr(macos, "Quartz", fake)
    monkeypatch.setattr(MacOSBackend, "_ensure_ready_for_input", lambda self: None)
    backend = MacOSBackend({})

    with pytest.raises(BackendError, match="\u00e9"):
        backend.type_text("caf\u00e9")  # 'é' has no US ANSI keycode

    assert fake.posted == []
    assert fake.unicode_calls == 0


def test_type_text_guard_wiring_still_works_with_real_keycodes(monkeypatch):
    """The per-character guard contract (already covered above) must survive
    the switch away from the unicode-string technique."""
    fake = _FakeRealKeycodeQuartz()
    monkeypatch.setattr(macos, "Quartz", fake)
    monkeypatch.setattr(MacOSBackend, "_ensure_ready_for_input", lambda self: None)
    backend = MacOSBackend({})
    guard = _FakeGuard()

    backend.type_text("ab", guard=guard)

    assert guard.calls == ["before", "after", "before", "after"]
    assert len(fake.posted) == 4  # 2 chars * (down + up)


# -- session/lock detection (D-locked-screen): _macos_session_state ------------
#
# Real ioreg output captured live against `macos-host` (unlocked,
# see the accompanying report) is the "unlocked" fixture below - byte-for-byte
# the shape actually returned by `ioreg -n Root -d1 -a` on a real Mac, not a
# guessed structure. `plistlib` is used to parse it, exactly as production code
# does - no fake plist parser, only a faked `subprocess.run`.

_IOREG_UNLOCKED_PLIST = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>IOConsoleLocked</key>
\t<false/>
\t<key>IOConsoleUsers</key>
\t<array>
\t\t<dict>
\t\t\t<key>kCGSSessionUserNameKey</key>
\t\t\t<string>user</string>
\t\t\t<key>kCGSessionLoginDoneKey</key>
\t\t\t<true/>
\t\t</dict>
\t</array>
</dict>
</plist>
"""

_IOREG_LOCKED_PLIST = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>CGSSessionScreenIsLocked</key>
\t<true/>
\t<key>IOConsoleLocked</key>
\t<true/>
\t<key>IOConsoleUsers</key>
\t<array>
\t\t<dict>
\t\t\t<key>kCGSSessionUserNameKey</key>
\t\t\t<string>user</string>
\t\t\t<key>kCGSessionLoginDoneKey</key>
\t\t\t<true/>
\t\t</dict>
\t</array>
</dict>
</plist>
"""

_IOREG_NO_CONSOLE_USER_PLIST = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>IOConsoleLocked</key>
\t<false/>
\t<key>IOConsoleUsers</key>
\t<array/>
</dict>
</plist>
"""


class _FakeIoregProc:
    def __init__(self, stdout: bytes, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout.decode("utf-8")
        self.returncode = returncode
        self.stderr = stderr


def test_session_state_unlocked_from_real_captured_ioreg_output(monkeypatch):
    monkeypatch.setattr(
        macos.subprocess,
        "run",
        lambda *a, **k: _FakeIoregProc(_IOREG_UNLOCKED_PLIST),
    )
    state, detail = macos._macos_session_state()
    assert state == "unlocked"
    assert detail


def test_session_state_locked_when_cgs_key_present_true(monkeypatch):
    """The proven signature: CGSSessionScreenIsLocked present and True - this
    is the exact key/value the real locked-vs-unlocked measurement against
    macos-host distinguished (see the report)."""
    monkeypatch.setattr(
        macos.subprocess,
        "run",
        lambda *a, **k: _FakeIoregProc(_IOREG_LOCKED_PLIST),
    )
    state, detail = macos._macos_session_state()
    assert state == "locked"
    assert "CGSSessionScreenIsLocked" in detail


def test_session_state_no_gui_session_when_no_console_user(monkeypatch):
    monkeypatch.setattr(
        macos.subprocess,
        "run",
        lambda *a, **k: _FakeIoregProc(_IOREG_NO_CONSOLE_USER_PLIST),
    )
    state, detail = macos._macos_session_state()
    assert state == "no_gui_session"
    assert detail


def test_session_state_unknown_when_ioreg_fails_to_exec(monkeypatch):
    def _boom(*a, **k):
        raise OSError("ioreg: command not found")

    monkeypatch.setattr(macos.subprocess, "run", _boom)
    state, detail = macos._macos_session_state()
    assert state == "unknown"
    assert "ioreg" in detail.lower()


def test_session_state_unknown_when_ioreg_exits_nonzero(monkeypatch):
    monkeypatch.setattr(
        macos.subprocess,
        "run",
        lambda *a, **k: _FakeIoregProc(b"", returncode=1, stderr="permission denied"),
    )
    state, detail = macos._macos_session_state()
    assert state == "unknown"
    assert "permission denied" in detail


def test_session_state_unknown_when_plist_unparseable(monkeypatch):
    monkeypatch.setattr(
        macos.subprocess,
        "run",
        lambda *a, **k: _FakeIoregProc(b"not a plist at all"),
    )
    state, detail = macos._macos_session_state()
    assert state == "unknown"


# -- capture() refuses a locked/no-GUI/unknown session (FAILS WITHOUT THE FIX) --
#
# Before this pass, capture() never consulted session state at all - it went
# straight to _active_display_ids()/CGDisplayCreateImage, which succeed
# perfectly well against a LOCKED screen (a real, plausible-looking image is
# returned). These tests fail on the pre-fix code because no BackendError is
# raised at all before Quartz is ever touched.


def test_capture_refuses_when_locked(monkeypatch):
    monkeypatch.setattr(
        macos,
        "_macos_session_state",
        lambda: ("locked", "CGSSessionScreenIsLocked=True"),
    )
    backend = MacOSBackend({})

    with pytest.raises(BackendError, match="LOCKED"):
        backend.capture()


def test_capture_refuses_when_no_gui_session(monkeypatch):
    monkeypatch.setattr(
        macos, "_macos_session_state", lambda: ("no_gui_session", "no console user")
    )
    backend = MacOSBackend({})

    with pytest.raises(BackendError, match="no GUI session"):
        backend.capture()


def test_capture_refuses_when_session_state_unknown(monkeypatch):
    monkeypatch.setattr(
        macos, "_macos_session_state", lambda: ("unknown", "ioreg timed out")
    )
    backend = MacOSBackend({})

    with pytest.raises(BackendError, match="could not determine"):
        backend.capture()


def test_capture_proceeds_when_unlocked(monkeypatch):
    """Sanity check: an "unlocked" state must not block capture - the
    pre-existing Quartz path still runs exactly as before."""
    fake = _FakeQuartz(
        [
            {
                "id": 1,
                "bounds": (0, 0, 1440, 900),
                "pixel_w": 2880,
                "pixel_h": 1800,
                "main": True,
            }
        ]
    )
    monkeypatch.setattr(macos, "Quartz", fake)
    monkeypatch.setattr(
        macos, "_macos_session_state", lambda: ("unlocked", "console user logged in")
    )
    backend = MacOSBackend({})

    # Reaches real Quartz capture machinery - CGDisplayCreateImage isn't faked
    # on _FakeQuartz, so this call itself would raise AttributeError if the
    # lock check were somehow still blocking; getting past that line proves
    # the "unlocked" path is unaffected.
    with pytest.raises(AttributeError):
        backend.capture()


# -- discrete input refuses a locked/no-GUI session (FAILS WITHOUT THE FIX) ----
#
# Before this pass, every discrete-input method (move, click, key, ...) checked
# ONLY _ensure_input_trusted() (Accessibility TCC) - a locked session silently
# swallows CGEventPost calls exactly the same way a missing TCC grant does,
# with no error either way. These tests fail on the pre-fix code because
# click()/key() proceed all the way to Quartz.CGEventPost (which the fakes
# below do not implement) instead of raising BackendError first.


def test_click_refuses_when_locked(monkeypatch):
    monkeypatch.setattr(
        macos,
        "_macos_session_state",
        lambda: ("locked", "CGSSessionScreenIsLocked=True"),
    )
    backend = MacOSBackend({})

    with pytest.raises(BackendError, match="LOCKED"):
        backend.click(10, 10)


def test_key_refuses_when_locked(monkeypatch):
    monkeypatch.setattr(
        macos,
        "_macos_session_state",
        lambda: ("locked", "CGSSessionScreenIsLocked=True"),
    )
    backend = MacOSBackend({})

    with pytest.raises(BackendError, match="LOCKED"):
        backend.key("Return")


def test_type_text_refuses_when_locked(monkeypatch):
    monkeypatch.setattr(
        macos,
        "_macos_session_state",
        lambda: ("locked", "CGSSessionScreenIsLocked=True"),
    )
    backend = MacOSBackend({})

    with pytest.raises(BackendError, match="LOCKED"):
        backend.type_text("hello")


def test_input_refuses_when_no_gui_session(monkeypatch):
    monkeypatch.setattr(
        macos, "_macos_session_state", lambda: ("no_gui_session", "no console user")
    )
    backend = MacOSBackend({})

    with pytest.raises(BackendError, match="no GUI session"):
        backend.move(0, 0)


def test_input_still_gated_by_accessibility_when_unlocked(monkeypatch):
    """The lock check must not bypass the existing Accessibility gate - both
    still apply, lock-state first (see _ensure_ready_for_input's docstring)."""
    monkeypatch.setattr(
        macos, "_macos_session_state", lambda: ("unlocked", "console user logged in")
    )
    monkeypatch.setattr(macos, "_ax_is_process_trusted", lambda: False)
    backend = MacOSBackend({})

    with pytest.raises(BackendError, match="Accessibility"):
        backend.click(10, 10)


def test_lock_check_runs_before_accessibility_check(monkeypatch):
    """Order matters: a locked-AND-untrusted session must be diagnosed as
    LOCKED, not as a missing Accessibility grant - this is the exact
    ambiguity that produced the wrong diagnosis in the real incident."""
    monkeypatch.setattr(
        macos,
        "_macos_session_state",
        lambda: ("locked", "CGSSessionScreenIsLocked=True"),
    )
    monkeypatch.setattr(macos, "_ax_is_process_trusted", lambda: False)
    backend = MacOSBackend({})

    with pytest.raises(BackendError, match="LOCKED"):
        backend.click(10, 10)


# -- narrow single-active-display screencapture fallback -----------------------


@pytest.fixture(autouse=True)
def _private_fallback_test_storage(monkeypatch, tmp_path):
    import tempfile

    real_mkdtemp = tempfile.mkdtemp

    def private_dir(**kwargs):
        return real_mkdtemp(**{**kwargs, "dir": str(tmp_path)})

    monkeypatch.setattr(tempfile, "mkdtemp", private_dir)
    yield
    assert not list(tmp_path.glob("amplifier-cu-capture-*")), "capture storage leaked"


class _FallbackImage:
    def __init__(self, width, height, raw=b"") -> None:
        self.width = width
        self.height = height
        self.raw = raw


class _SingleFallbackQuartz(_FakeQuartz):
    """A 2x Retina display whose native per-display capture returns ``None``."""

    def __init__(self) -> None:
        super().__init__(
            [
                {
                    "id": 7,
                    "bounds": (0, 0, 2, 1),
                    "pixel_w": 4,
                    "pixel_h": 2,
                    "main": True,
                }
            ]
        )
        self.decoded_image = _FallbackImage(4, 2)

    def CGDisplayCreateImage(self, _display_id):
        return None

    def CGImageSourceCreateWithData(self, data, _options):
        return data

    def CGImageSourceCreateImageAtIndex(self, source, _index, _options):
        self.decoded_image.raw = source
        return self.decoded_image

    def CGImageGetWidth(self, image):
        return image.width

    def CGImageGetHeight(self, image):
        return image.height

    def CGRectMake(self, x, y, width, height):
        return x, y, width, height

    def CGImageCreateWithImageInRect(self, image, rect):
        import io

        from PIL import Image

        x, y, width, height = rect
        with Image.open(io.BytesIO(image.raw)) as decoded:
            cropped = decoded.crop((x, y, x + width, y + height))
            data = io.BytesIO()
            cropped.save(data, format="PNG")
        return _FallbackImage(width, height, data.getvalue())


def _valid_png(width=4, height=2):
    """Use Pillow only to create test data; ImageIO is faked below."""
    import io

    from PIL import Image

    output = io.BytesIO()
    image = Image.new("RGBA", (width, height))
    image.putdata(
        [
            (x * 20, y * 40, (x + y) * 10, 255)
            for y in range(height)
            for x in range(width)
        ]
    )
    image.save(output, format="PNG")
    return output.getvalue()


def _fallback_backend(monkeypatch):
    fake = _SingleFallbackQuartz()
    monkeypatch.setattr(macos, "Quartz", fake)
    monkeypatch.setattr(
        macos, "_macos_session_state", lambda: ("unlocked", "test session")
    )
    monkeypatch.setattr(macos, "_cg_preflight_screen_capture_access", lambda: True)
    monkeypatch.setitem(
        sys.modules,
        "CoreFoundation",
        types.SimpleNamespace(CFDataCreate=lambda _alloc, raw, length: raw[:length]),
    )
    return MacOSBackend({}), fake


def _png_process(data, seen, after_write=None):
    def fake_run(argv, **kwargs):
        import os
        import stat

        image_path = Path(argv[-1])
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        seen["directory_mode"] = stat.S_IMODE(os.stat(image_path.parent).st_mode)
        seen["file_mode"] = stat.S_IMODE(os.stat(image_path).st_mode)
        if data is None:
            image_path.unlink()
        else:
            image_path.write_bytes(data)
        if after_write is not None:
            after_write()
        return types.SimpleNamespace(returncode=0)

    return fake_run


@pytest.mark.parametrize(
    ("region", "expected_size"),
    [
        (None, (4, 2)),
        ((0, 0, 2, 2), (2, 2)),
        ((2, 0, 4, 2), (2, 2)),
    ],
)
def test_capture_single_display_fallback_preserves_retina_capture_and_crop(
    monkeypatch, region, expected_size
):
    backend, fake = _fallback_backend(monkeypatch)
    seen = {}
    raw = _valid_png()
    monkeypatch.setattr(macos.subprocess, "run", _png_process(raw, seen))

    def encode(image):
        import io

        from PIL import Image

        assert not Path(seen["argv"][-1]).exists(), "utility file must be removed"
        with Image.open(io.BytesIO(raw)) as original:
            expected = original.crop(region) if region is not None else original
            with Image.open(io.BytesIO(image.raw)) as decoded:
                assert decoded.size == expected_size
                assert decoded.tobytes() == expected.tobytes()
        return f"encoded:{image.width}x{image.height}".encode()

    monkeypatch.setattr(MacOSBackend, "_encode_png", staticmethod(encode))

    assert backend.capture(region) == (
        f"encoded:{expected_size[0]}x{expected_size[1]}".encode()
    )
    assert len(seen["argv"]) == 6
    assert seen["argv"][:-1] == ["/usr/sbin/screencapture", "-x", "-m", "-t", "png"]
    assert seen["kwargs"]["stdin"] is macos.subprocess.DEVNULL
    assert seen["kwargs"]["stdout"] is macos.subprocess.DEVNULL
    assert seen["kwargs"]["stderr"] is macos.subprocess.DEVNULL
    assert "capture_output" not in seen["kwargs"]
    assert seen["kwargs"]["shell"] is False
    assert seen["directory_mode"] == 0o700
    assert seen["file_mode"] == 0o600
    assert (fake.decoded_image.width, fake.decoded_image.height) == (4, 2)


def test_capture_native_success_never_consults_single_display_fallback(monkeypatch):
    backend, fake = _fallback_backend(monkeypatch)
    native = _FallbackImage(4, 2)
    fake.CGDisplayCreateImage = lambda _display_id: native
    monkeypatch.setattr(
        macos,
        "_cg_preflight_screen_capture_access",
        lambda: pytest.fail("fallback preflight called after native success"),
    )
    monkeypatch.setattr(
        MacOSBackend,
        "_screencapture_single_display",
        lambda *_args: pytest.fail("fallback called after native success"),
    )
    monkeypatch.setattr(
        MacOSBackend, "_encode_png", staticmethod(lambda _image: b"native")
    )

    assert backend.capture() == b"native"


def test_capture_multi_display_whole_path_is_unchanged(monkeypatch):
    fake = _FakeQuartz(
        [
            {"id": 7, "bounds": (0, 0, 2, 1), "pixel_w": 2, "pixel_h": 1, "main": True},
            {
                "id": 9,
                "bounds": (2, 0, 2, 1),
                "pixel_w": 2,
                "pixel_h": 1,
                "main": False,
            },
        ]
    )
    fake.CGWindowListCreateImage = lambda *_args: "virtual"
    fake.CGRectInfinite = "infinite"
    fake.kCGWindowListOptionOnScreenOnly = 1
    fake.kCGNullWindowID = 0
    fake.kCGWindowImageDefault = 0
    monkeypatch.setattr(macos, "Quartz", fake)
    monkeypatch.setattr(macos, "_macos_session_state", lambda: ("unlocked", "test"))
    monkeypatch.setattr(
        MacOSBackend,
        "_screencapture_single_display",
        lambda *_args: pytest.fail("multi-display whole capture must not use fallback"),
    )
    monkeypatch.setattr(
        MacOSBackend, "_encode_png", staticmethod(lambda image: image.encode())
    )

    assert MacOSBackend({}).capture() == b"virtual"


def test_capture_multi_display_region_native_none_keeps_existing_error(monkeypatch):
    backend, _fake = _fallback_backend(monkeypatch)
    _fake._displays[9] = {
        "id": 9,
        "bounds": (2, 0, 2, 1),
        "pixel_w": 2,
        "pixel_h": 1,
        "main": False,
    }
    monkeypatch.setattr(
        MacOSBackend,
        "_screencapture_single_display",
        lambda *_args: pytest.fail("multi-display region must not use fallback"),
    )
    monkeypatch.setattr(
        MacOSBackend, "_capture_none_error", lambda *_args: "native none"
    )

    with pytest.raises(BackendError, match="native none"):
        backend.capture((0, 0, 1, 1))


@pytest.mark.parametrize("state", ["locked", "no_gui_session", "unknown"])
def test_capture_initial_non_unlocked_state_never_starts_fallback(monkeypatch, state):
    backend, _fake = _fallback_backend(monkeypatch)
    monkeypatch.setattr(macos, "_macos_session_state", lambda: (state, "raw detail"))
    monkeypatch.setattr(
        macos.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("child process started"),
    )

    with pytest.raises(BackendError):
        backend.capture()


@pytest.mark.parametrize("preflight", [False, None])
def test_capture_fallback_refuses_nonpositive_preflight_without_child(
    monkeypatch, preflight
):
    backend, _fake = _fallback_backend(monkeypatch)
    monkeypatch.setattr(macos, "_cg_preflight_screen_capture_access", lambda: preflight)
    monkeypatch.setattr(
        macos.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("child process started"),
    )

    with pytest.raises(BackendError, match="preflight was not positive"):
        backend.capture()


def test_capture_fallback_refuses_locked_recheck_without_child(monkeypatch):
    backend, _fake = _fallback_backend(monkeypatch)
    states = iter([("unlocked", "initial"), ("locked", "private")])
    monkeypatch.setattr(macos, "_macos_session_state", lambda: next(states))
    monkeypatch.setattr(
        macos.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("child process started"),
    )

    with pytest.raises(BackendError, match="session is not unlocked") as error:
        backend.capture()
    assert "private" not in str(error.value)


def test_capture_fallback_refuses_topology_change_before_child(monkeypatch):
    backend, fake = _fallback_backend(monkeypatch)
    active_lists = iter([[7], [7], [9]])
    monkeypatch.setattr(backend, "_active_display_ids", lambda: next(active_lists))
    monkeypatch.setattr(
        macos.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("child process started"),
    )

    with pytest.raises(BackendError, match="target display changed"):
        backend.capture()
    assert 7 in fake._displays


def test_capture_fallback_refuses_main_display_change_before_child(monkeypatch):
    backend, fake = _fallback_backend(monkeypatch)
    main_ids = iter([7, 8])
    fake.CGMainDisplayID = lambda: next(main_ids)
    monkeypatch.setattr(
        macos.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("child process started"),
    )

    with pytest.raises(BackendError, match="target display changed"):
        backend.capture()


def test_capture_fallback_refuses_geometry_change_before_child(monkeypatch):
    backend, fake = _fallback_backend(monkeypatch)

    def native_none(_display_id):
        fake._displays[7]["pixel_w"] = 5
        return None

    fake.CGDisplayCreateImage = native_none
    monkeypatch.setattr(
        macos.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("child process started"),
    )

    with pytest.raises(BackendError, match="target display changed"):
        backend.capture()


@pytest.mark.parametrize("change", ["topology", "main", "geometry"])
def test_capture_fallback_discards_child_output_after_target_change(
    monkeypatch, change
):
    backend, fake = _fallback_backend(monkeypatch)
    seen = {}

    def change_target():
        if change == "topology":
            fake._displays[9] = {
                "id": 9,
                "bounds": (2, 0, 2, 1),
                "pixel_w": 2,
                "pixel_h": 1,
                "main": False,
            }
        elif change == "main":
            fake.CGMainDisplayID = lambda: 9
        else:
            fake._displays[7]["pixel_h"] = 3

    monkeypatch.setattr(
        macos.subprocess, "run", _png_process(_valid_png(), seen, change_target)
    )

    with pytest.raises(BackendError, match="target display changed"):
        backend.capture()
    assert "argv" in seen


def test_capture_fallback_discards_child_output_after_session_locks(monkeypatch):
    backend, _fake = _fallback_backend(monkeypatch)
    states = iter(
        [
            ("unlocked", "initial"),
            ("unlocked", "before child"),
            ("locked", "after child"),
        ]
    )
    monkeypatch.setattr(macos, "_macos_session_state", lambda: next(states))
    monkeypatch.setattr(macos.subprocess, "run", _png_process(_valid_png(), {}))

    with pytest.raises(BackendError, match="session is not unlocked"):
        backend.capture()


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        ("timeout", "timed out"),
        ("nonzero", "failed"),
        ("missing", "produced no PNG file"),
        ("empty", "produced no PNG data"),
        ("malformed", "produced invalid PNG data"),
        ("read", "could not read PNG data"),
    ],
)
def test_capture_fallback_child_failures_are_fixed_and_private(
    monkeypatch, outcome, message
):
    backend, _fake = _fallback_backend(monkeypatch)
    poison = "do-not-leak-private-capture-path"
    if outcome == "timeout":
        monkeypatch.setattr(
            macos.subprocess,
            "run",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                macos.subprocess.TimeoutExpired(["ignored"], 1, stderr=poison)
            ),
        )
    elif outcome == "nonzero":
        monkeypatch.setattr(
            macos.subprocess,
            "run",
            lambda *_args, **_kwargs: types.SimpleNamespace(returncode=1),
        )
    else:
        data = {
            "missing": None,
            "empty": b"",
            "malformed": poison.encode(),
            "read": _valid_png(),
        }[outcome]
        monkeypatch.setattr(macos.subprocess, "run", _png_process(data, {}))
        if outcome == "read":
            monkeypatch.setattr(
                macos.Path,
                "read_bytes",
                lambda _path: (_ for _ in ()).throw(OSError(poison)),
            )

    with pytest.raises(BackendError, match=message) as error:
        backend.capture()
    assert poison not in str(error.value)


@pytest.mark.parametrize(
    ("source", "image", "message"),
    [
        (None, _FallbackImage(4, 2), "could not decode PNG data"),
        (b"source", None, "could not decode PNG image"),
        (b"source", _FallbackImage(3, 2), "unexpected image dimensions"),
    ],
)
def test_capture_fallback_rejects_invalid_decoded_image(
    monkeypatch, source, image, message
):
    backend, fake = _fallback_backend(monkeypatch)
    fake.CGImageSourceCreateWithData = lambda _data, _options: source
    fake.CGImageSourceCreateImageAtIndex = lambda _source, _index, _options: image
    monkeypatch.setattr(macos.subprocess, "run", _png_process(_valid_png(), {}))

    with pytest.raises(BackendError, match=message):
        backend.capture()


def test_capture_fallback_temp_creation_error_is_fixed_and_private(monkeypatch):
    backend, _fake = _fallback_backend(monkeypatch)
    poison = "do-not-leak-temp-path"
    monkeypatch.setattr(
        macos.tempfile,
        "mkdtemp",
        lambda **_kwargs: (_ for _ in ()).throw(OSError(poison)),
    )

    with pytest.raises(
        BackendError, match="could not create private temporary storage"
    ) as error:
        backend.capture()
    assert poison not in str(error.value)


@pytest.mark.parametrize("returncode", [0, 1])
def test_capture_fallback_cleanup_failure_never_reports_success(
    monkeypatch, returncode
):
    import shutil

    backend, _fake = _fallback_backend(monkeypatch)
    real_rmtree = shutil.rmtree
    seen = {}
    write_png = _png_process(_valid_png(), seen)

    def run(*args, **kwargs):
        write_png(*args, **kwargs)
        return types.SimpleNamespace(returncode=returncode)

    def fail_cleanup(_path):
        raise OSError("PRIVATE_CAPTURE_PATH")

    monkeypatch.setattr(macos.subprocess, "run", run)
    monkeypatch.setattr(macos.shutil, "rmtree", fail_cleanup)
    try:
        with pytest.raises(
            BackendError, match="private capture data may remain"
        ) as exc:
            backend.capture()
        assert "PRIVATE_CAPTURE_PATH" not in str(exc.value)
    finally:
        if seen:
            real_rmtree(Path(seen["argv"][-1]).parent)


def test_capture_fallback_uses_remaining_deadline_budget(monkeypatch):
    backend, fake = _fallback_backend(monkeypatch)
    clock = types.SimpleNamespace(value=0.0)
    monkeypatch.setattr(macos.time, "monotonic", lambda: clock.value)

    def native_none(_display_id):
        clock.value = 10.0
        return None

    def preflight():
        clock.value = 12.0
        return True

    seen = {}
    fake.CGDisplayCreateImage = native_none
    monkeypatch.setattr(macos, "_cg_preflight_screen_capture_access", preflight)
    monkeypatch.setattr(macos.subprocess, "run", _png_process(_valid_png(), seen))
    monkeypatch.setattr(MacOSBackend, "_encode_png", staticmethod(lambda _image: b"ok"))

    assert backend.capture() == b"ok"
    assert seen["kwargs"]["timeout"] == pytest.approx(8.0)


def test_capture_fallback_expired_budget_skips_child(monkeypatch):
    backend, fake = _fallback_backend(monkeypatch)
    clock = types.SimpleNamespace(value=0.0)
    monkeypatch.setattr(macos.time, "monotonic", lambda: clock.value)

    def native_none(_display_id):
        clock.value = 20.0
        return None

    fake.CGDisplayCreateImage = native_none
    monkeypatch.setattr(
        macos.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("expired budget launched child"),
    )

    with pytest.raises(BackendError, match="exceeded capture budget"):
        backend.capture()


def test_capture_fallback_discards_late_child_output(monkeypatch):
    backend, _fake = _fallback_backend(monkeypatch)
    clock = types.SimpleNamespace(value=0.0)
    monkeypatch.setattr(macos.time, "monotonic", lambda: clock.value)
    seen = {}

    def late():
        clock.value = 20.1

    monkeypatch.setattr(macos.subprocess, "run", _png_process(_valid_png(), seen, late))

    with pytest.raises(BackendError, match="exceeded capture budget"):
        backend.capture()
    assert "argv" in seen
