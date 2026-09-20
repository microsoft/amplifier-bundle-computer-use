"""Read-only foreground observations using the existing local macOS backend.

The calling host owns consent, device/session identity and cancellation. This
library neither grants permission nor starts monitoring. Run each observation in
a bounded subprocess when native OS calls must be cancellable. Status never
enumerates windows, captures pixels, requests OS permission or touches input.
"""
from __future__ import annotations

import base64
import io
import sys
import time


class ForegroundUnavailable(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ForegroundObserver:
    def __init__(self, backend):
        self.backend = backend

    def status(self):
        probe = self.backend.probe()
        if not probe.available:
            return {"available": False, "status": "unavailable", "permission": "unknown",
                    "reason": str(probe.reason)[:300], "backend": self.backend.name}
        permission = self.backend.screen_capture_permission()
        return {"available": permission is True,
                "status": "ready" if permission is True else "permission_required" if permission is False else "permission_unknown",
                "permission": "granted" if permission is True else "required" if permission is False else "unknown",
                "backend": self.backend.name}

    def _window(self):
        windows = self.backend.list_windows()
        found = next((row for row in windows.windows if row.handle == windows.foreground and not row.minimized), None)
        if found is None or found.rect is None:
            raise ForegroundUnavailable("foreground_unavailable", "The OS could not identify a visible foreground window and its bounds.")
        rect = found.rect
        if (len(rect) != 4 or any(type(n) is not int for n in rect)
                or not 0 < rect[2]-rect[0] <= 16000 or not 0 < rect[3]-rect[1] <= 16000
                or (rect[2]-rect[0])*(rect[3]-rect[1]) > 40_000_000):
            raise ForegroundUnavailable("foreground_bounds", "The foreground window exceeds capture bounds.")
        return found

    def capture(self):
        status = self.status()
        if not status["available"]:
            raise ForegroundUnavailable(status["status"], "Screen Recording permission and a supported local desktop are required.")
        before = self._window()
        png = self.backend.capture(region=before.rect, allow_utility_fallback=False)
        captured = time.time()
        after = self._window()
        if (before.handle, before.rect, before.app_name) != (after.handle, after.rect, after.app_name):
            raise ForegroundUnavailable("foreground_changed", "The foreground window changed during capture; no evidence was returned.")
        if self.backend.screen_capture_permission() is not True:
            raise ForegroundUnavailable("permission_changed", "Screen Recording permission changed during capture.")
        if not isinstance(png, bytes) or len(png) > 32_000_000:
            raise ForegroundUnavailable("image_bounds", "The native frame exceeds the image limit.")
        from PIL import Image
        with Image.open(io.BytesIO(png)) as frame:
            expected = (before.rect[2]-before.rect[0], before.rect[3]-before.rect[1])
            if frame.format != "PNG" or frame.size != expected:
                raise ForegroundUnavailable("image_bounds", "The native frame does not match the observed foreground bounds.")
            image = frame.convert("RGB")
            image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            for _ in range(12):
                encoded = io.BytesIO()
                image.save(encoded, format="PNG")
                value = encoded.getvalue()
                if len(value) <= 500000:
                    return {"image": base64.b64encode(value).decode("ascii"), "capturedAt": captured,
                            "width": image.width, "height": image.height, "backend": self.backend.name,
                            "window": {"id": str(before.handle)[:120], "title": str(before.title)[:300],
                                       "application": str(before.app_name)[:200] if before.app_name else None,
                                       "bounds": list(before.rect)},
                            "captureScope": "visible foreground window region", "untrustedData": True}
                image.thumbnail((max(1, int(image.width*.75)), max(1, int(image.height*.75))), Image.Resampling.LANCZOS)
        raise ForegroundUnavailable("image_bounds", "The native frame cannot fit the output limit.")


def local_observer():
    """Select only this process's local desktop; never read remote configuration."""
    if sys.platform != "darwin":
        raise ForegroundUnavailable("unsupported", "Native foreground observation currently supports local macOS only.")
    from .macos import MacOSBackend
    return ForegroundObserver(MacOSBackend())
