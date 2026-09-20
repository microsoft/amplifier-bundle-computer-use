# Read-only foreground observations

`amplifier_module_tool_computer_use.foreground.local_observer()` exposes the existing local macOS backend as a small library. `status()` checks backend availability and Screen Recording permission without enumerating windows, reading pixels, requesting OS permission, changing focus, or sending input. Unsupported platforms and unavailable permissions fail closed.

`capture()` returns one bounded PNG plus the observed foreground window ID, application name when supplied by CoreGraphics, title, physical-pixel bounds and capture time. Names and pixels are untrusted data. The observer checks window identity/geometry and permission before and after capture. It does not promise an atomic OS snapshot. This is the visible foreground window region, which can include overlays or occlusion; it is not accessibility text or a hidden-window rendering. Multi-display windows whose actual captured dimensions differ from the observed bounds are rejected.

The caller owns consent, host identity, active-call or task scope, freshness, rate limiting and cancellation. Invoke each operation in a bounded subprocess when native calls need a hard deadline. `capture()` disables the backend's utility fallback so killing the helper cannot abandon a utility child or temporary screenshot. No pixels are written by this observer. The PNG is at most 1280 pixels per side and 500,000 bytes, and only one native capture is attempted. A failed capture is never answered by another target or a whole-desktop fallback.

This library does not load computer target settings, autodiscover SSH hosts, grant permissions, monitor a desktop, invoke the mutable computer tool, or clear a durable computer-use safety halt. Installing the module alone does not activate observation.

Validation uses synthetic backend/window/PNG fixtures and existing macOS backend mocks. No live desktop capture or permission prompt was performed for this contribution. Real acceptance requires a user-approved source, existing OS Screen Recording permission for the host process, and a supported unlocked local macOS desktop.
