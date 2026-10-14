"""Test-only public Session reproduction on one owned Linux X11 fixture.

Provider/module/model and provider-owned count policy are opaque caller inputs.
This records public calls, NOT HTTP attempts or effective delivered settings.
Native protocol qualification must be supplied independently by the provider owner.
Import/--help never starts a display, reads a key or calls a model.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import hashlib
import importlib
import inspect
import json
import os
import secrets
import select
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from neutral_qualification import (
    SCENARIOS,
    artifact,
    binding_check,
    load_json,
    package_identity,
    production_manifest,
    receipt,
    require,
    script_manifest,
)


class PublicCalls:
    """Observe/wrap only public methods, with per-method invocation quotas."""

    def __init__(self, limits, deadline):
        require(
            set(limits) == {"complete", "request_budget", "estimate"}, "invalid quotas"
        )
        require(
            all(type(v) is int and v > 0 for v in limits.values()), "nonpositive quotas"
        )
        self.limits = dict(limits)
        self.calls = dict.fromkeys(limits, 0)
        self.deadline = deadline
        self.projected = {name: [] for name in limits}
        self.purity = True
        self.restores = []

    def admit(self, name):
        require(time.monotonic() < self.deadline, "execution deadline")
        require(self.calls[name] < self.limits[name], "public invocation quota")
        self.calls[name] += 1

    def observe(self, request, name=None):
        value = (
            request.model_dump()
            if hasattr(request, "model_dump")
            else copy.deepcopy(request)
        )
        if name is not None:
            self.projected[name].extend(image_hashes(value))
        return value

    def check(self, request, before):
        after = request.model_dump() if hasattr(request, "model_dump") else request
        self.purity = self.purity and after == before
        require(self.purity, "caller request changed")

    def wrap(self, provider, *, outer_purity=False):
        for name in self.limits:
            method = getattr(provider, name, None)
            if callable(method):
                self.restores.append((provider, name, method))
                setattr(
                    provider,
                    name,
                    self.wrapper(name, method, outer_purity=outer_purity),
                )

    def wrapper(self, name, method, *, outer_purity=False):
        # request_budget may be synchronous but return an awaitable. Preserve both.
        def observed(request, **kwargs):
            if not outer_purity:
                self.admit(name)
            before = self.observe(request, None if outer_purity else name)
            try:
                result = method(request, **kwargs)
            finally:
                self.check(request, before)
            if inspect.isawaitable(result):

                async def finish():
                    try:
                        return await asyncio.wait_for(
                            result, max(0, self.deadline - time.monotonic())
                        )
                    finally:
                        self.check(request, before)

                return finish()
            return result

        return observed

    def restore(self):
        for provider, name, method in reversed(self.restores):
            setattr(provider, name, method)
        self.restores.clear()


def outer_purity_handler(calls, provider):
    installed = False

    async def handler(event, data):
        from amplifier_core.models import HookResult

        nonlocal installed
        if not installed:
            calls.wrap(provider, outer_purity=True)
            installed = True
        return HookResult(action="continue")

    return handler


def image_hashes(value):
    """Inspect generic public request values, never vendor protocol structures."""
    found = []
    if isinstance(value, dict):
        for item in value.values():
            found.extend(image_hashes(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(image_hashes(item))
    elif isinstance(value, str):
        encoded = value.partition(",")[2] if value.startswith("data:image/") else value
        try:
            data = base64.b64decode(encoded, validate=True)
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                found.append(hashlib.sha256(data).hexdigest())
        except ValueError:
            pass
    return found


def identities(binding):
    return {
        role: package_identity(v["distribution"], v["module"])
        for role, v in binding["identities"].items()
    }


def verify_identity(binding, expected, root):
    binding_check(binding)
    require(identities(binding) == binding["identities"], "installed identity mismatch")
    require(
        production_manifest(root) == binding["source"] == expected["source"],
        "production source mismatch",
    )
    require(
        script_manifest(root) == expected["producer"]["scripts"],
        "producer script mismatch",
    )


def prompt():
    # The fresh visible marker is intentionally not an argument or part of the prompt.
    return "On this isolated owned test desktop, take exactly one screenshot using the desktop tool. Use no other operation. Read and report the alphanumeric receipt code visible in that screenshot."


def capture_only(value):
    if value == {"action": "screenshot"}:
        return True
    return (
        isinstance(value, dict)
        and type(value.get("version")) is int
        and value["version"] == 1
        and value.get("actions") == [{"action": "screenshot"}]
        and set(value) <= {"version", "actions", "capture_after"}
        and type(value.get("capture_after", False)) is bool
    )


def cleanup_handle(mounted):
    return mounted if callable(mounted) else None


def stop_owned(process):
    if process is None:
        return "absent"
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    if process.stdout:
        process.stdout.close()
    try:
        os.kill(process.pid, 0)
    except ProcessLookupError:
        return "absent"
    raise RuntimeError("owned Xvfb still present")


async def run(args, *, output_fd=None):
    output = args.output.absolute()
    if output_fd is None:
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
    else:
        # Only the supervisor's inherited directory descriptor permits reuse.
        # An existing path, environment flag or symlink is not ownership.
        require(
            os.path.samestat(os.fstat(output_fd), output.stat(follow_symlinks=False))
            and output.is_dir(),
            "output does not match inherited directory",
        )
    output = output.resolve()
    require(not os.environ.get("DISPLAY"), "ambient DISPLAY refused")
    require(bool(os.environ.get(args.keyenv)), "caller key environment missing")
    require(30 <= args.deadline_seconds <= 900, "deadline must include cleanup reserve")
    expected = load_json(args.expected)
    bindings = [
        b
        for b in expected["bindings"]
        if (b["provider"], b["profile"], b["mode"])
        == (args.provider, "current", "live")
    ]
    require(len(bindings) == 1, "missing exact live/current binding")
    binding = bindings[0]
    policy = load_json(args.count_policy)
    require(
        isinstance(policy, dict) and bool(policy),
        "missing opaque provider count policy",
    )
    root = Path(__file__).resolve().parents[1]
    started = time.monotonic()
    calls = PublicCalls(
        {
            "complete": args.max_complete,
            "request_budget": args.max_budget,
            "estimate": args.max_estimate,
        },
        started + args.deadline_seconds - 15,
    )
    require(
        args.provider_module == binding["identities"]["provider"]["module"],
        "provider module mismatch",
    )
    # An entirely private process-local home contains all default screenshot paths.
    # No SHOT_DIR override, private provider method, SDK or replay injection.
    previous = {k: os.environ.get(k) for k in ("HOME", "TMPDIR", "DISPLAY")}
    for variable, directory in (("HOME", "home"), ("TMPDIR", "tmp")):
        path = output / directory
        path.mkdir(mode=0o700)
        os.environ[variable] = str(path)
    began = datetime.now(timezone.utc).isoformat()
    process = desktop = session = backend = None
    cleanup_provider = cleanup_hook = None
    acquired_backends = []
    display_record = None
    captures, cycles = [], []
    cleanup = dict.fromkeys(("session", "backend", "tk", "xvfb"), "UNKNOWN")
    scenarios = dict.fromkeys(SCENARIOS["session"], "UNKNOWN")
    status, failure = "FAIL", None
    configured = None
    pump_task = None
    try:
        verify_identity(binding, expected, root)
        # Normal imports only, after establishing the private home.
        import tkinter as tk

        from amplifier_core import AmplifierSession, ModuleLoader
        from amplifier_core.models import HookResult

        tool_module = importlib.import_module(binding["identities"]["tool"]["module"])
        hook_module = importlib.import_module(binding["identities"]["hook"]["module"])
        provider_module = importlib.import_module(args.provider_module)
        backend_module = importlib.import_module(
            binding["identities"]["tool"]["module"] + ".linux_x11"
        )
        # verify_identity imports tool code too. Ensure its default home resolved to
        # this owned output, rather than silently changing a module global.
        require(
            tool_module.SHOT_DIR.resolve().is_relative_to(output),
            "tool capture root is not private owned output",
        )
        process = subprocess.Popen(
            [
                args.xvfb,
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
            stderr=subprocess.DEVNULL,
            # Inherit the supervisor's exclusive worker group, including helpers.
            env={
                k: os.environ[k] for k in ("HOME", "TMPDIR", "PATH") if k in os.environ
            },
        )
        display_record = {
            "pid": process.pid,
            "process_group": os.getpgid(process.pid),
            "display": None,
            "scope": "owned-X11-only",
        }
        (output / "owned-process.json").write_text(json.dumps(display_record) + "\n")
        require(
            bool(select.select([process.stdout], [], [], 5)[0]), "Xvfb startup deadline"
        )
        number = process.stdout.readline().decode("ascii").strip()
        require(number.isdigit() and process.poll() is None, "owned Xvfb unavailable")
        display_record["display"] = ":" + number
        os.environ["DISPLAY"] = display_record["display"]
        (output / "owned-process.json").write_text(json.dumps(display_record) + "\n")
        desktop = tk.Tk()
        desktop.overrideredirect(True)
        desktop.geometry("800x600+0+0")
        desktop.configure(bg="white")
        marker = secrets.token_hex(4).upper()
        label = tk.Label(
            desktop,
            text="OWNED TEST DESKTOP\n" + marker,
            bg="white",
            fg="black",
            font=("DejaVu Sans", 30),
        )
        label.pack(pady=150)
        desktop.update()

        async def pump():
            while True:
                desktop.update()
                await asyncio.sleep(0.01)

        pump_task = asyncio.create_task(pump())
        backend = backend_module.LinuxX11Backend({"display": display_record["display"]})
        acquired_backends.append(backend)
        require(backend.probe().available, "owned X11 backend unavailable")

        class RestrictedCapture(tool_module.ComputerTool):
            @property
            def name(self):
                return "desktop"

            @property
            def native_tool_spec(self):
                value = super().native_tool_spec
                value["computer_use"]["operations"] = ["screenshot"]
                return value

            async def execute(self, value):
                require(
                    capture_only(value),
                    "restricted test declaration only admits screenshot",
                )
                return await super().execute(value)

        tool = RestrictedCapture(backend, {"read_only": True, "max_edge": 800})
        tool.resolve_display()
        session = AmplifierSession(
            {
                "session": {
                    "orchestrator": {
                        "module": "loop-streaming",
                        "config": {
                            "max_iterations": args.max_complete,
                            "auto_continue": False,
                        },
                    },
                    "context": {"module": "context-simple"},
                },
                "providers": [],
                "tools": [],
                "hooks": [],
            }
        )
        coordinator = session.coordinator
        coordinator.loader = ModuleLoader(coordinator=coordinator)
        await session.initialize()
        config = {
            "default_model": args.model,
            "use_streaming": False,
            "max_retries": 0,
            "auto_continue": False,
            "max_tokens": 1200,
        }
        cleanup_provider = await provider_module.mount(coordinator, config)
        providers = coordinator.get("providers")
        require(
            isinstance(providers, dict) and len(providers) == 1,
            "provider did not mount",
        )
        provider = next(iter(providers.values()))
        configured = {"max_retries": 0, "auto_continue": False}
        calls.wrap(provider)
        await coordinator.mount("tools", tool, name="desktop")
        hook_mount = await hook_module.mount(coordinator, {"max_inline_screenshots": 3})
        cleanup_hook = cleanup_handle(hook_mount)
        # Production projection wraps lazily at provider:request priority 50.
        coordinator.hooks.register(
            "provider:request",
            outer_purity_handler(calls, provider),
            priority=60,
            name="neutral-caller-purity",
        )

        async def post(event, data):
            if data.get("tool_name") == "desktop":
                require(data["result"]["success"], "canonical capture failed")
                result = data["result"]["output"]
                result = json.loads(result) if isinstance(result, str) else result
                require(
                    result.get(tool_module.MARKER) == 1
                    and len(result.get("images", [])) == 1,
                    "missing canonical capture",
                )
                path = Path(result["images"][0]).resolve()
                require(
                    path.is_relative_to(output) and path.is_file(),
                    "foreign capture path",
                )
                captures.append(
                    artifact(output, path.relative_to(output).as_posix(), "capture")
                )
                require(len(captures) <= 2, "capture quota exceeded")
            return HookResult(action="continue")

        coordinator.hooks.register(
            "tool:post", post, priority=100, name="neutral-capture-observer"
        )
        for cycle in range(2):
            if cycle:
                marker = secrets.token_hex(4).upper()
                label.configure(text="OWNED TEST DESKTOP\n" + marker)
                desktop.update()
            count_before = len(captures)
            projection_before = len(calls.projected["complete"])
            answer = await asyncio.wait_for(
                session.execute(prompt()), max(0, calls.deadline - time.monotonic())
            )
            require(
                len(captures) == count_before + 1 and marker in answer,
                "capture/visible marker not observed",
            )
            projected = calls.projected["complete"][projection_before:]
            current = [c["sha256"] for c in captures[count_before:]]
            require(
                set(current) <= set(projected)
                and {c["sha256"] for c in captures[:count_before]} <= set(projected),
                "image projection/retained history missing",
            )
            cycles.append(
                {
                    "captures": current,
                    "projected": sorted(set(projected)),
                    "recognized": True,
                    "prior_history_retained": True,
                }
            )
        for name in (
            "canonical_capture",
            "marker_recognition",
            "image_projection",
            "adjacent_history",
        ):
            scenarios[name] = "PASS"
        require(calls.purity, "caller request changed")
        scenarios["caller_purity"] = "PASS"
        verify_identity(binding, expected, root)
        scenarios["identity_unchanged"] = "PASS"
        status = "PASS"
    except BaseException as exc:
        failure = type(exc).__name__  # never persist provider exception text/keys
    finally:
        if pump_task:
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
            except BaseException:
                status = "FAIL"
        operations = []
        if session:
            operations.append(("session", session.cleanup))
        if cleanup_hook:
            operations.append(("hook", cleanup_hook))
        if cleanup_provider:
            operations.append(("provider", cleanup_provider))
        for name, operation in operations:
            try:
                value = operation()
                if inspect.isawaitable(value):
                    remaining = started + args.deadline_seconds - time.monotonic()
                    await asyncio.wait_for(value, max(0, min(3, remaining)))
                if name in cleanup:
                    cleanup[name] = "closed"
            except BaseException:
                status = "FAIL"
        try:
            for connection in acquired_backends:
                connection.close()
            cleanup["backend"] = "closed"
        except BaseException:
            status = "FAIL"
        try:
            if desktop:
                desktop.destroy()
            cleanup["tk"] = "closed"
        except BaseException:
            status = "FAIL"
        try:
            cleanup["xvfb"] = stop_owned(process)
        except BaseException:
            status = "FAIL"
        calls.restore()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        elapsed = time.monotonic() - started
        if (
            cleanup
            == {
                "session": "closed",
                "backend": "closed",
                "tk": "closed",
                "xvfb": "absent",
            }
            and elapsed <= args.deadline_seconds
        ):
            scenarios["cleanup"] = "PASS"
        else:
            status = "FAIL"
        observations = {
            "public_bounds": {
                "limits": calls.limits,
                "calls": calls.calls,
                "deadline_seconds": args.deadline_seconds,
                "elapsed_seconds": elapsed,
                "configured": configured,
                "effective_wire": "UNKNOWN",
                "count_policy": policy,
            },
            "cycles": cycles,
            "owned_display": display_record,
            "cleanup": cleanup,
        }
        result = receipt(
            binding,
            "session",
            began,
            datetime.now(timezone.utc).isoformat(),
            status,
            scenarios,
            captures,
            observations=observations,
        )
        (output / "session.json").write_text(json.dumps(result, indent=2) + "\n")
        (output / "run-summary.json").write_text(
            json.dumps(
                {
                    "status": status,
                    "failure_type": failure,
                    "declaration": "restricted screenshot-only test; not full-operation native qualification",
                    "effective_wire": "UNKNOWN",
                    "provider_protocol_gate": "UNMET: independent provider-owned receipt required",
                },
                indent=2,
            )
            + "\n"
        )
    return status


def wait_owned_exit(process, deadline):
    # WNOWAIT keeps the leader's PID allocated until our final group signal.
    while time.monotonic() < deadline:
        if (
            os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            is not None
        ):
            return True
        time.sleep(min(0.02, max(0, deadline - time.monotonic())))
    return False


def supervise(args):
    """Bound synchronous worker stalls and reap only an exclusive owned group."""
    require(30 <= args.deadline_seconds <= 900, "invalid overall deadline")
    # Claim before spawning or reading any inputs. Do not resolve a preexisting
    # symlink into its target, or write failure receipts into an old directory.
    output = args.output.absolute()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    output = output.resolve()
    output_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    command = [
        sys.executable,
        "-B",
        str(Path(__file__).resolve()),
        "--worker",
        "--output-fd",
        str(output_fd),
    ]
    for name in (
        "expected",
        "provider",
        "provider_module",
        "model",
        "keyenv",
        "count_policy",
        "output",
        "xvfb",
        "deadline_seconds",
        "max_complete",
        "max_budget",
        "max_estimate",
    ):
        command.extend(["--" + name.replace("_", "-"), str(getattr(args, name))])
    started = time.monotonic()
    deadline = started + args.deadline_seconds
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=(output_fd,),
        )
    finally:
        os.close(output_fd)
    supervision = {
        "worker_pid": process.pid,
        "process_group": process.pid,
        "deadline_seconds": args.deadline_seconds,
        "elapsed_seconds": None,
        "timed_out": False,
        "group_absent": False,
    }
    print(f"owned worker PID/group: {process.pid}", flush=True)
    try:
        supervision["timed_out"] = not wait_owned_exit(process, deadline - 6)
    finally:
        # This group was created exclusively for this worker. Xvfb and backend
        # helper children inherit it; no ambient/human process can be swept.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=max(0, deadline - time.monotonic()))
        while time.monotonic() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                supervision["group_absent"] = True
                break
            time.sleep(0.02)
    supervision["elapsed_seconds"] = time.monotonic() - started
    (output / "supervisor.json").write_text(json.dumps(supervision, indent=2) + "\n")
    passed = (
        process.returncode == 0
        and not supervision["timed_out"]
        and supervision["group_absent"]
        and supervision["elapsed_seconds"] <= args.deadline_seconds
    )
    path = output / "session.json"
    if path.is_file():
        result = load_json(path)
        result["observations"]["supervision"] = supervision
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["status"] = "PASS" if passed and result["status"] == "PASS" else "FAIL"
        path.write_text(json.dumps(result, indent=2) + "\n")
        passed = passed and result["status"] == "PASS"
    else:
        passed = False
    return "PASS" if passed else "FAIL"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--provider", required=True, help="opaque binding id")
    parser.add_argument("--provider-module", required=True)
    parser.add_argument("--model", required=True, help="opaque model name")
    parser.add_argument(
        "--keyenv", required=True, help="environment name only; never a key value"
    )
    parser.add_argument(
        "--count-policy",
        type=Path,
        required=True,
        help="opaque provider-owned bounded policy/provenance",
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="new private evidence directory"
    )
    parser.add_argument("--xvfb", default="/usr/bin/Xvfb")
    parser.add_argument("--deadline-seconds", type=int, default=240)
    parser.add_argument("--max-complete", type=int, default=6)
    parser.add_argument("--max-budget", type=int, default=12)
    parser.add_argument("--max-estimate", type=int, default=12)
    args = parser.parse_args(argv)
    try:

        async def bounded():
            # Cancellation reaches run's finally with an explicit cleanup reserve.
            try:
                return await asyncio.wait_for(
                    run(args, output_fd=args.output_fd), args.deadline_seconds - 15
                )
            finally:
                os.close(args.output_fd)

        require(
            args.worker or args.output_fd is None, "output descriptor is worker-only"
        )
        if args.worker:
            require(os.getpgid(0) == os.getpid(), "worker must own an exclusive group")
            require(args.output_fd is not None, "worker requires inherited output")
            status = asyncio.run(bounded())
        else:
            status = supervise(args)
    except Exception as exc:
        print("FAIL: " + type(exc).__name__)
        return 1
    print(status + ": public receipt only; independent live gate not implied")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
