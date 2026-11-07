"""Installed-module freshness proof on an exclusively owned synthetic Xvfb.

Run with an installed module, no ambient DISPLAY, and an empty output directory.
The fixture supplies one known candidate; it does not qualify OCR recognition,
Unified integration, physical input, Windows, or live human coexistence.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import select
import subprocess
import time
from pathlib import Path
from unittest.mock import patch


async def run(args):
    if os.environ.get("DISPLAY"):
        raise RuntimeError("Ambient DISPLAY refused; only an owned Xvfb is allowed")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    private_home = output / "home"
    private_home.mkdir()
    # Isolate this test process's package-default shot/halt paths. No environment
    # or host settings change, and no guard, approval or presence check is mocked.
    with patch.object(Path, "home", return_value=private_home):
        import amplifier_module_tool_computer_use as cu
        from amplifier_module_tool_computer_use import halt_state, observed_control
        from Xlib import X, Xatom, display

    package = Path(cu.__file__).resolve().parent
    if not package.is_relative_to(args.installed_root.resolve()):
        raise RuntimeError("Worker did not import the specified installed package")
    assert cu.SHOT_DIR.is_relative_to(private_home)
    assert halt_state.DEFAULT_STATE_DIR.is_relative_to(private_home)
    report = {
        "workerPid": os.getpid(),
        "package": str(package),
        "sourceSha256": hashlib.sha256(
            Path(observed_control.__file__).read_bytes()
        ).hexdigest(),
        "scope": "installed module; owned Xvfb; known fixture candidate, no OCR",
        "checks": {},
    }
    process, connection, mounted = None, None, None
    stderr_file = (output / "xvfb.stderr").open("wb")
    try:
        process = subprocess.Popen(
            [
                "/usr/bin/Xvfb",
                "-displayfd",
                "1",
                "-screen",
                "0",
                "800x600x24",
                "-nolisten",
                "tcp",
                "-noreset",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
        )
        assert select.select([process.stdout], [], [], 5)[0], "Xvfb startup timeout"
        number = process.stdout.readline().decode("ascii").strip()
        assert number.isdigit() and process.poll() is None, "Xvfb did not start"
        target = ":" + number
        report["display"] = {"name": target, "pid": process.pid}
        (output / "owner.json").write_text(json.dumps(report) + "\n")
        connection = display.Display(target)
        screen = connection.screen()
        window = screen.root.create_window(
            0,
            0,
            800,
            600,
            0,
            screen.root_depth,
            X.InputOutput,
            X.CopyFromParent,
            background_pixel=screen.white_pixel,
            event_mask=X.ButtonPressMask | X.ButtonReleaseMask,
            override_redirect=True,
        )
        window.map()
        window.set_input_focus(X.RevertToParent, X.CurrentTime)
        screen.root.change_property(
            connection.intern_atom("_NET_ACTIVE_WINDOW"), Xatom.WINDOW, 32, [window.id]
        )
        connection.sync()
        await asyncio.sleep(2.1)  # Establish a fresh quiet presence sample.

        class Coordinator:
            tools = {}

            async def mount(self, kind, tool, *, name):
                assert kind == "tools"
                self.tools[name] = tool

        coordinator = Coordinator()
        mounted = await cu.mount(
            coordinator,
            {
                "display": target,
                "observed_control": True,
                "gate_writes": True,
                "coexistence": {"announce": False},
            },
        )
        tool = coordinator.tools["computer"]
        await asyncio.to_thread(tool._ensure_announced)
        assert tool._coexistence_guard is not None
        assert tool._backend.current_target() == str(window.id)
        control = tool._observed_control
        raw, digest, _, _ = control.capture(tool._binding)
        (output / "synthetic-screen.png").write_bytes(raw)
        observed = {
            "revision": "owned-fixture",
            "state": {"elements": [{"id": "target", "rect": [20, 20, 80, 40]}]},
            "candidates": [{"id": "click", "operation": "CLICK", "target": "target"}],
            "created_at": time.monotonic(),
        }
        control.observations["owned-fixture"] = {
            "observation": observed,
            "binding": tool._binding,
            "digest": digest,
        }
        approvals = []
        allowed = True

        async def approve(prepared, binding):
            approvals.append(prepared)
            return allowed

        tool._write_approval = approve

        def received():
            connection.sync()
            events = []
            while connection.pending_events():
                event = connection.next_event()
                if event.type in {X.ButtonPress, X.ButtonRelease}:
                    assert event.window.id == window.id and not event.send_event
                    assert event.detail == 1
                    events.append(event.type)
            return events

        async def act(expected, input_events):
            assert received() == [], "unexpected input before action"
            result = await tool.execute(
                {
                    "action": "act",
                    "revision": "owned-fixture",
                    "candidate": "click",
                }
            )
            assert result.success and result.output["status"] == expected, result
            assert received() == input_events, "native delivery differs from receipt"
            return expected

        await asyncio.sleep(15.1)
        report["selectionAgeSeconds"] = time.monotonic() - observed["created_at"]
        report["checks"]["agedUnchanged"] = await act(
            "completed", [X.ButtonPress, X.ButtonRelease]
        )
        assert approvals == [{"action": "left_click", "coordinate": [60, 40]}]
        window.change_attributes(background_pixel=screen.black_pixel)
        window.clear_area()
        connection.sync()
        report["checks"]["changedPixels"] = await act("stale", [])
        window.change_attributes(background_pixel=screen.white_pixel)
        window.clear_area()
        connection.sync()
        allowed = False
        report["checks"]["revokedApproval"] = await act("denied", [])
        allowed = True
        click = tool._backend.click

        def lost_reply(*positional, **keywords):
            click(*positional, **keywords)
            raise RuntimeError("fixture lost reply after real input")

        tool._backend.click = lost_reply
        report["checks"]["unknownNotReplayed"] = await act(
            "unknown", [X.ButtonPress, X.ButtonRelease]
        )
        tool._backend.click = click
        tool._coexistence_guard.pause.set("overlay_click", reason="fixture Stop")
        report["checks"]["stop"] = await act("unknown", [])
        tool._gate_writes = True  # Replacement binding, identical target/pixels.
        report["checks"]["replacedBinding"] = await act("stale", [])
        report["status"] = "passed"
    finally:
        try:
            if mounted is not None:
                await mounted()
        finally:
            if connection is not None:
                connection.close()
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                process.stdout.close()
                report["ownedDisplayStopped"] = process.poll() is not None
            stderr_file.close()
            (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
