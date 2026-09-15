# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project uses [Semantic Versioning](https://semver.org/) once it reaches a
tagged release. Everything below is currently unreleased, tracked against the `0.1.x`
line declared in `modules/tool-computer-use/pyproject.toml` and
`modules/hook-computer-use/pyproject.toml`.

## [Unreleased]

### Added

- Human/agent coexistence mechanism: presence detection, an unconditional halt that
  stops the agent's writes the moment a human is detected at the machine (not
  configurable off, on any platform), and target binding that aborts a multi-event
  operation if focus changes mid-op. Design record in the design notes.
- SSH-based remote transport: drive a desktop on a different machine across a private
  network (e.g. Tailscale), with a single persistent remote agent process per session
  and screenshots downscaled to model space on the target before crossing the wire.
  Design record in the design notes.
- macOS backend for the `computer` and `desktop` tools, alongside the existing Windows
  and Linux X11 backends.
- Per-monitor display targeting, fixing coordinate scaling on multi-monitor setups.
- Linux X11 backend and a `Backend` protocol/registry seam so platform support can be
  added without touching the tool's core logic.

### Fixed

- Generalised that fallback from "the sole display" to "one display at a time", which is
  what the default configuration actually needs: `target_monitor` defaults to `"primary"`,
  so per-monitor capture routes through the per-display path, not the whole-virtual-desktop
  branch - and on macOS 26 a second attached display therefore broke *every* ordinary
  screenshot with an error blaming a display that was awake and capturable. Same guards,
  `-D <1-based ordinal>` instead of `-m`, plus an active-display-list reorder check that the
  sole-display form did not need. `-D`'s mapping to `CGGetActiveDisplayList` order is
  verified by image CONTENT on macOS 26.6.2, not by size.
- Whole-virtual-desktop capture on multi-display Macs running macOS 26, which hit a flat
  30.0s `CGWindowListCreateImage` cost - also the remote transport's per-op timeout, so it
  dropped the connection instead of returning an image. Composites per-display captures into
  the identical canvas (point-space bounding box at the largest backing scale). Verified on a
  mixed-DPI rig (2x built-in beside a 1x ultrawide): same 13696x2880 output, 30.04s -> 0.47s.
  Falls back to the original call if the composite cannot be built.
- Added a narrow, offline-logic-tested-only macOS fallback after native per-display capture
  returns `None`: one bounded `screencapture -m` attempt for an unchanged single active main
  display, with fresh preflight, lock/topology checks, private temporary storage, and
  in-memory PNG decoding. Real-macOS verification is still required. Adapted from the
  capture-alternative lead reported by [@colombod in PR #11](https://github.com/microsoft/amplifier-bundle-computer-use/pull/11).
- Two silent failures blocking end-to-end remote desktop control.
- Missing `python-xlib` now reported as a missing dependency rather than surfacing as
  an X server connection failure.
- Provider request hook now fails closed if the provider gains a `stream()` method
  (rather than silently no-op'ing and leaving computer-use blind); removed a dead
  `_default_headers` mutation; PowerShell is now resolved without depending on `PATH`
  (needed for non-login SSH shells, where `PATH` does not include `/mnt/c/...`).
- Non-ASCII window titles in `desktop.list_windows` (missing `CharSet.Unicode` on the
  `GetWindowText` P/Invoke declaration).

### Changed

- Repository takeover: sources repointed, scope corrected, original attribution
  preserved (see `LICENSE` and the Credits section of `README.md`).

## Attribution note

This project began as a Windows-from-WSL2 bundle by
[@ckrabach617](https://github.com/ckrabach617); that original contribution predates the
"Unreleased" entries above and is preserved in this repository's git history. See
`README.md`'s Credits section and `LICENSE`.
