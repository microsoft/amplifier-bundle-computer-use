"""Native observation contracts using synthetic pixels; no OS capture or prompt."""
import base64
import io

import pytest
from PIL import Image

from amplifier_module_tool_computer_use.backend import ProbeResult, WindowInfo, WindowList
from amplifier_module_tool_computer_use.foreground import ForegroundObserver, ForegroundUnavailable


class Backend:
    name = "synthetic-macos"
    def __init__(self):
        self.permission = True
        self.reads = self.captures = 0
        self.window = WindowInfo("42", "Test document", rect=(10, 20, 14, 23), app_name="Fixture app")
    def probe(self): return ProbeResult(True)
    def screen_capture_permission(self): return self.permission
    def list_windows(self):
        self.reads += 1
        return WindowList([self.window], self.window.handle)
    def capture(self, region, *, allow_utility_fallback=True):
        assert allow_utility_fallback is False
        self.captures += 1
        assert region == (10, 20, 14, 23)
        stream = io.BytesIO()
        Image.new("RGB", (4, 3), "purple").save(stream, format="PNG")
        return stream.getvalue()


@pytest.mark.parametrize("permission,status", [(True,"ready"),(False,"permission_required"),(None,"permission_unknown")])
def test_status_has_no_window_pixels_input_or_permission_prompt(permission, status):
    backend = Backend(); backend.permission = permission
    result = ForegroundObserver(backend).status()
    assert result["status"] == status and result["available"] is (permission is True)
    assert backend.reads == backend.captures == 0


@pytest.mark.parametrize("permission", [False, None])
def test_permission_missing_fails_before_observation(permission):
    backend = Backend(); backend.permission = permission
    with pytest.raises(ForegroundUnavailable): ForegroundObserver(backend).capture()
    assert backend.reads == backend.captures == 0


def test_exact_window_metadata_and_real_bounded_png():
    backend = Backend()
    result = ForegroundObserver(backend).capture()
    assert backend.captures == 1 and backend.reads == 2
    assert result["window"] == {"id":"42","title":"Test document","application":"Fixture app","bounds":[10,20,14,23]}
    with Image.open(io.BytesIO(base64.b64decode(result["image"]))) as image:
        assert image.size == (4,3) and image.getpixel((0,0)) == (128,0,128)


def test_changed_window_drops_pixels():
    backend = Backend()
    original = backend.capture
    def capture(region, **kwargs):
        png = original(region, **kwargs)
        backend.window = WindowInfo("43","Another app",rect=backend.window.rect)
        return png
    backend.capture = capture
    with pytest.raises(ForegroundUnavailable, match="changed") as error: ForegroundObserver(backend).capture()
    assert error.value.code == "foreground_changed" and backend.captures == 1


def test_permission_revoked_during_capture_drops_pixels():
    backend = Backend(); original = backend.capture
    def capture(region, **kwargs):
        png = original(region, **kwargs); backend.permission = False; return png
    backend.capture = capture
    with pytest.raises(ForegroundUnavailable, match="permission changed"): ForegroundObserver(backend).capture()


def test_unknown_bounds_do_not_expand_to_whole_desktop():
    backend = Backend(); backend.window = WindowInfo("42","Unknown geometry")
    with pytest.raises(ForegroundUnavailable, match="bounds"): ForegroundObserver(backend).capture()
    assert backend.captures == 0


def test_native_permission_method_is_read_only(monkeypatch):
    from amplifier_module_tool_computer_use import macos
    seen = []
    monkeypatch.setattr(macos, "_cg_preflight_screen_capture_access", lambda: seen.append("preflight") or False)
    assert macos.MacOSBackend().screen_capture_permission() is False
    assert seen == ["preflight"]
