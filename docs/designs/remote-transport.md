# Remote Transport & Security Model

**Status:** Design proposal
**Scope:** How `amplifier-bundle-computer-use` drives a desktop on a *different* machine across a private network, and what that is allowed to do.
**Date:** 2026-08-01

---

## 1. Recommendation, up front

**Use SSH as the transport, with a single persistent remote agent process per session. Do not build a network daemon.**

The prior is correct. It survived a genuine attempt to break it. But it is incomplete in four specific ways, and one of those four is load-bearing enough that getting it wrong would produce a system that works in a demo and is unusable in practice.

| # | Correction | Severity |
|---|---|---|
| **C1** | The remote seam **cannot** be raw `Backend.capture()`. Screenshots must be downscaled to model space *on the target*, before they cross the wire. | **Load-bearing** — determines whether this is usable |
| **C2** | SSH transport does **not** fix the Windows latency problem. It stacks on top of it. Two persistent-process layers are needed, not one. | High — sets expectations correctly |
| **C3** | The local path must **not** use the transport. Local stays a direct function call, with zero SSH. | High — protects the non-technical user |
| **C4** | `Backend` is a synchronous protocol called from an `async` tool. A blocking remote call stalls the event loop *including cancellation*. | Medium — 3-line fix, invisible if missed |

Everything else in the prior — SSH for auth/encryption/host identity, one long-lived process reading NDJSON on stdin, `RemoteBackend` implementing the same `Backend` protocol, the far end running the *same* platform backend code — is right and is adopted as-is.

---

## 2. Why the prior is right

Restating the argument in its strongest form, because it is the foundation of everything below.

**SSH adds no new authority.** Anyone who can `ssh` to `user@macos-host` can already run `screencapture` and drive the desktop with `osascript`. A computer-use agent reachable over that same channel is not a new capability — it is a more convenient interface to an existing one. A bespoke daemon is the opposite: it *creates* a new authority (a listening socket that accepts "type this string" from the network) and then requires you to invent an auth model to constrain it. You would be reimplementing, worse, the thing SSH already does correctly.

**The security model fits in one sentence.** *If you can SSH to the box, you can drive its desktop.* That is auditable by a human in ten seconds. No new port, no new credential, no new key rotation story, no new thing to forget to patch.

**The persistent process is the right shape.** Per-action process spawn is the known defect in the existing Windows backend. Repeating it at the network layer would compound it. One long-lived process amortises interpreter startup, X11/Quartz connection setup, and monitor enumeration across an entire session.

**One implementation, two deployment shapes.** The far end runs `select_backend()` and the same `MacOSBackend` / `LinuxX11Backend` / `WindowsBackend` that already pass 83 tests. There is exactly one implementation of "how to click on macOS." This is the property worth protecting above all others, and it is the reason C1 below is a *capability extension* rather than a new seam.

---

## 3. Verified facts this design is built on

Facts supplied with the brief are taken as given. The following were additionally measured during this design pass, against the two real targets, because each one determines a decision the design cannot make honestly without it.

### 3.1 Target inventory

```
user@windows-host   (WSL side of the Win11 box, ~1ms)
  uname   : Linux x86_64
  python3 : /usr/bin/python3  -> Python 3.12.3
  uv      : /home/user/.local/bin/uv        ← present
  pip3    : MISSING                              ← confirms the "venv with no pip" note
  PIL     : not installed
  stdlib tarfile/zlib/json : ok

user@macos-host   (~8ms)
  uname   : Darwin arm64
  python3 : /opt/homebrew/bin/python3 -> Python 3.14.5
  uv      : /opt/homebrew/bin/uv                 ← present
  pip3    : /opt/homebrew/bin/pip3
  PIL     : not installed
  stdlib tarfile/zlib/json : ok
```

Three consequences:

1. **`uv` is present on both targets.** This dissolves the "venv with no pip" problem completely — `uv run --with pillow` provisions dependencies with no pre-existing venv and no pip. It is the deployment answer.
2. **Python versions differ across targets (3.12 vs 3.14).** Agent code must be valid on both. No 3.13+ syntax.
3. **`uv` was found on `PATH` in a non-login shell here, but `/mnt/c` was not** (per the brief). PATH in this context is partially populated and cannot be trusted. Resolve `uv` by absolute path discovered at deploy time — exactly the pattern `windows.py::_which_powershell` already uses for `powershell.exe`.

### 3.2 Screenshot payload — measured, not estimated

Measured on `macos-host` at design time, using built-in `screencapture` + `sips`:

| | bytes | dimensions |
|---|---:|---|
| Native full-screen PNG | **1,341,423** | 3456 × 2234 |
| Downscaled to 1280 long edge | **514,799** | 1280 × 827 |

That is a 2.6× reduction *for this particular frame*. The brief reports a **10.5 MB** native full-screen capture from the same machine under different screen content — a 20× reduction against the same ~515 KB bound.

**The ratio is not the argument. The bound is.** Native payload size is content-dependent and effectively unbounded — it varies 8× between two captures of the same display. Model-space payload is bounded by the pixel budget the tool already enforces (`max_edge=1280`, `max_pixels=1_150_000`). A transport whose worst case you can state is a transport you can reason about. See §5.

### 3.3 Bootstrap and safety mechanics — proven on a real target

The Phase 1 mechanic was built and run end-to-end against `macos-host`. Output, verbatim:

```
{"t": "hello", "py": "3.14.5", "plat": "darwin"}
{"t": "ok", "op": "hold", "held": ["ctrl"]}
{"t": "ok", "op": "click", "held": ["ctrl"]}
sha256=8db769cec67d40b3      (remote)
local sha256=8db769cec67d40b3
RELEASED:ctrl
```

This proves four things that the rest of the design depends on:

1. **One SSH connection carries both code deployment and the protocol session.** The payload tarball is written to the agent's stdin, consumed by byte count, extracted, and the *same* stdin continues as the NDJSON channel. No `scp`, no second connection, no second channel.
2. **Content-addressed integrity works** — remote hash matched local hash.
3. **stdin-EOF → held-input release fires.** When the stream closed, the agent released the held `ctrl`. This is the mechanism that protects the human's desktop from a stuck modifier key, and it is verified, not asserted.
4. **`head -c N` is unsafe here and must not be used.** The first attempt used `head -c $N | tar xz` in the bootstrap. It over-read: `head` buffered past its byte limit and swallowed the two NDJSON requests that followed, which were then silently lost. The working bootstrap is a pure-Python stub that reads exactly `N` bytes with `sys.stdin.buffer.read(n)` and then runs the agent **in-process** via `runpy` — `os.exec*` would discard Python's already-buffered remainder of stdin.

---

## 4. Where the prior is wrong or incomplete

### C1 — The seam cannot be raw `Backend.capture()`

`Backend.capture()` is contractually defined to *"Return PNG bytes at native resolution"* (`backend.py:155`). Downscaling to model space happens on the controller, in `imaging.capture_scaled_b64`, using PIL.

A `RemoteBackend` that implements `capture()` faithfully therefore drags **1.3–10.5 MB across the wire per screenshot**, base64-inflated by 33% if it rides the NDJSON channel, and then spends 200–400 ms of controller CPU LANCZOS-resizing a 3456×2234 image — to throw away 93% of the pixels it just paid to transfer. Computer-use is screenshot-dominated; the recommended agent pattern is a screenshot before every action. This is not a tax, it is the whole cost model.

**Fix: add an optional capability to the `Backend` protocol, not a new seam.**

```python
def capture_scaled(
    self,
    region: tuple[int, int, int, int] | None,
    model_size: tuple[int, int],
    max_edge: int,
    max_pixels: int,
) -> str:            # base64 PNG, already in model space
    ...
```

`imaging.capture_scaled_b64` gains a fast path that checks for the capability **on the class, never by invoking it** — the exact idiom the hook already uses for `native_tool_spec` after the D3 fix (`hook-computer-use/__init__.py:278-281`):

```python
if getattr(type(backend), "capture_scaled", None) is not None:
    return backend.capture_scaled(region, (disp.model_width, disp.model_height), max_edge, max_pixels)
# else: today's path — capture() + PIL, unchanged
```

**Only `RemoteBackend` implements it.** Local backends do not, and should not: locally, `capture()` costs a memory copy, so pushing the resize down would be duplication for zero gain. The capability exists *because there is a wire to protect*, so only the wired backend carries it. Local behaviour is byte-for-byte unchanged.

The far end needs PIL to honour it — provisioned by `uv` (§7).

### C2 — SSH does not fix the Windows latency problem

The Windows target is reached at `user@windows-host`, which is the **WSL side** of the Win11 box. So the remote agent is a Linux Python process that then subprocesses `powershell.exe` per action, across the WSL2→Win32 boundary.

The persistent agent amortises **Python** startup. It does nothing about **PowerShell** startup, which the brief identifies as costing *seconds* per action. Remote Windows will remain slow after this design ships. Saying otherwise would be dishonest.

The real fix is the *same pattern one layer down*: a long-lived `powershell.exe` reading newline-delimited JSON on **its** stdin, replacing `subprocess.run(...)`-per-action in `windows.py::raw`. That is Phase 4. It is deliberately sequenced after the transport, because it is independently valuable (it fixes local WSL→Windows too) and independently riskier.

The useful structural consequence: if the controller↔agent channel is specified as "NDJSON request/response over a pipe pair," then the agent↔PowerShell channel is *the identical framing*, one layer down. One protocol shape, two hops, and the second hop's fix is a drop-in.

```
controller ──NDJSON over SSH──► agent ──NDJSON over pipe──► powershell.exe
   (controller-host)                  (WSL)                       (Win32)
                              └── same framing, one layer down ──┘
```

### C3 — Local must not use the transport

The brief asks whether local and remote should share one mechanism. **They should deliberately differ, at exactly one place.**

Requiring a non-technical user to configure `sshd` on localhost in order to click their own mouse is absurd on its face. Worse, it is a *regression in reliability*: it converts a path with no failure modes into one that fails when sshd is down, when a host key rotates, when a key is passphrase-locked, when `BatchMode` can't authenticate. Adding a network to a local operation only adds ways for it to break.

So:

- `target` absent from config → `select_backend()` returns a platform backend directly, in-process. **Exactly today's behaviour, unchanged.**
- `target: ssh://user@host` present → `RemoteBackend` is used instead, and is the *only* candidate (no probe-fallthrough to a local backend — see §9).

The sharing that matters is not the transport, it is the `Backend` protocol and the platform implementations behind it. The far end runs the same `select_backend()`. There is still exactly one implementation of "how to click on macOS."

One useful side effect: `target: ssh://localhost` becomes a legitimate *test* device — it exercises the entire remote stack on one box — while never being the production local path.

### C4 — Sync protocol, async caller

`Backend` is synchronous. `ComputerTool._run` is synchronous. Both are called from `async def execute`. Today that is fine: local backends are microseconds (X11/Quartz) or an already-accepted subprocess (Windows).

A remote `readline()` on a pipe blocks the event loop for the full round trip — and for a screenshot that is hundreds of milliseconds. During that window the kernel's cancellation token cannot be serviced, so a user pressing Ctrl-C waits for a screenshot to finish transferring.

**Fix:** wrap the call at the `execute()` boundary, not in the protocol:

```python
summary, image_b64 = await asyncio.to_thread(self._run, action, input)
```

`Backend` stays synchronous — which keeps the local path and all 83 tests untouched — and the event loop stays live. This also gives cancellation a real story: `execute()` becomes cancellable, and the cleanup path can terminate the SSH process.

---

## 5. Architecture

```
┌─ CONTROLLER (controller-host, headless Linux) ───────────────────────────────────┐
│                                                                          │
│  ComputerTool / DesktopTool          (unchanged)                         │
│         │  Backend protocol                                              │
│         ▼                                                                │
│  RemoteBackend                                                           │
│    • implements Backend + capture_scaled capability                      │
│    • owns request ids, op classification, timeouts                       │
│    • NEVER retries a WRITE op                                            │
│         │                                                                │
│  SshTransport                                                            │
│    • one `ssh -T` subprocess for the session                             │
│    • stdin  ← NDJSON requests    stdout → NDJSON responses               │
│    • stderr → controller log (never parsed as protocol)                  │
└─────────┬────────────────────────────────────────────────────────────────┘
          │  Tailscale (WireGuard) ── native sshd ── no new port
┌─────────▼─ TARGET (windows-host WSL / macos-host) ───────────┐
│                                                                          │
│  cu-agent  (single long-lived process, session-scoped)                   │
│    • held-input ledger  ──► released on stdin EOF / deadman / signal     │
│    • audit log (JSONL, owned by the target's user)                       │
│    • read_only enforced HERE too (defence in depth)                      │
│    • PIL downscale to model space before responding                      │
│         │  select_backend()  ← the SAME registry.py                      │
│         ▼                                                                │
│  MacOSBackend │ LinuxX11Backend │ WindowsBackend ──► powershell.exe      │
└──────────────────────────────────────────────────────────────────────────┘
```

Screenshots still land on the **controller's** disk (`~/.amplifier/computer-use/shots/`), because that is where `hook-computer-use::_image_block` reads them (`Path(path).read_bytes()`). The marker protocol is untouched: the tool still writes a PNG and returns a marker; the hook still inlines it at provider-request time. Remote changes *where the pixels come from*, nothing else.

---

## 6. Wire protocol

Newline-delimited JSON, one object per line, request/response correlated by monotonic integer `id`. UTF-8. No embedded newlines.

### 6.1 Handshake

The agent's **first** line is unsolicited and carries everything needed to decide whether to proceed:

```json
{"id": 0, "ok": true, "result": {
  "protocol": 1,
  "agent_sha256": "8db769cec67d40b3…",
  "python": "3.14.5",
  "platform": "darwin",
  "backend": "macos-quartz",
  "probe": {"available": true, "reason": ""},
  "capabilities": ["capture_scaled", "held_ledger", "audit", "read_only"],
  "permissions": {"accessibility": true, "screen_recording": true},
  "monitors": [{"id": "1", "x": 0, "y": 0, "width": 3456, "height": 2234, "primary": true}]
}}
```

The controller **fails loud and disconnects** if: `protocol` is not exactly what it speaks; `agent_sha256` does not match what it deployed; `probe.available` is false; or any required entry in `permissions` is false. There is no negotiation-down and no partial-capability mode.

`permissions` deserves emphasis. macOS TCC grants attach to the *responsible* process. The brief verifies that `AXIsProcessTrusted()` is true and `screencapture` returns real content in today's SSH context — but that binding can change if the launch chain changes (e.g. going through `uv run` instead of `python3` directly). So the agent **actively probes at handshake**: `AXIsProcessTrusted()` plus a 1×1 `CGDisplayCreateImage`. Discovering a TCC denial at connect time is a clear error message; discovering it at first click is a mystery.

This is the D1 capability-probe principle, extended across the wire: *do not mount what cannot be served.*

### 6.2 Requests and responses

```json
→ {"id": 17, "op": "click", "args": {"x": 1024, "y": 768, "button": "left", "count": 1}}
← {"id": 17, "ok": true, "result": null}

→ {"id": 18, "op": "capture_scaled", "args": {"region": null, "model_w": 1280, "model_h": 827,
                                              "max_edge": 1280, "max_pixels": 1150000}}
← {"id": 18, "ok": true, "result": {"enc": "b64", "png": "iVBORw0…",
                                    "w": 1280, "h": 827,
                                    "native_w": 3456, "native_h": 2234,
                                    "scaled_on": "agent"}}

← {"id": 19, "ok": false, "error": {"type": "BackendError",
                                    "message": "focus_window: no window with handle '0x4400012'"}}
```

`error.type` carries the *class name* so the controller can re-raise a real `BackendError` and preserve the existing error-handling paths in `ComputerTool.execute` verbatim.

`"enc": "b64"` exists from day one so that a length-prefixed binary encoding can be introduced later without a protocol version bump. Base64 is chosen for Phase 1 deliberately: with far-end downscaling the overhead is ~170 KB per screenshot (≈20 ms of wire time on this tailnet), which is not worth optimising before the system is proven.

### 6.3 Operation classes

This table is the retry policy. It is not advisory.

| Class | Ops | Retryable? |
|---|---|---|
| **READ** | `probe`, `screen_geometry`, `list_monitors`, `capture`, `capture_scaled`, `cursor_position`, `list_windows`, `get_clipboard`, `ping` | Yes — idempotent |
| **WRITE** | `move`, `click`, `mouse_down`, `mouse_up`, `drag`, `scroll`, `key`, `hold_key`, `type_text`, `focus_window`, `set_clipboard` | **Never** |
| **CONTROL** | `hello`, `release_all`, `bye` | Yes — idempotent by construction |

A `left_click` whose response was lost may or may not have landed. Replaying it is how you get two clicks on "Confirm Purchase." **WRITE ops get zero automatic retries, ever.** The failure surfaces to the model as a tool error, and the model — which can take a screenshot and look — decides what to do. That decision belongs to something that can see the screen, not to a transport layer.

### 6.4 Stdout hygiene

Anything the far end prints to stdout corrupts the protocol stream. PyObjC in particular is chatty. The agent's first act, before importing any backend, is:

```python
_proto = os.fdopen(os.dup(1), "w", encoding="utf-8")   # protocol channel
os.dup2(2, 1)                                          # everything else → stderr
```

`ssh -T` (no TTY) so there is no line-ending translation. stderr is a separate SSH stream and is used for agent logs and diagnostics.

---

## 7. Code deployment and version skew

**The deployment problem and the version-skew problem have the same answer: content addressing over the session's own connection.**

At connect time the controller:

1. Builds a manifest-restricted `tar.gz` from `ssh_transport.PAYLOAD_MODULES`, including
   `remote_agent.py`, `agent_scratch_lease.py`, and the platform and support modules the
   remote agent imports.
2. Computes the SHA-256 of the exact payload bytes.
3. Opens **one** SSH connection whose remote command is a small pure-Python stub, and writes: `[payload bytes][NDJSON requests…]` to its stdin.

The stub reads exactly `N` bytes, computes and exports the received-byte hash, validates the
fixed manifest before extraction, then runs the agent **in-process** (`runpy.run_module`) from
a private session scratch directory so the buffered remainder of stdin survives as the protocol
channel. It registers its own cleanup immediately after creating that directory, before
extraction. The controller verifies the received-byte hash during the handshake; the stub does
not have an expected hash to compare.

Why this shape:

- **No `scp`, no second connection, no second channel.** Deployment and session are the same stream, so they cannot disagree about which code is running.
- **Version skew is structurally impossible**, not merely detected. The agent reports the hash
  of the received payload bytes; the controller compares it with the exact bytes it sent.
  Mismatch → fail loud, disconnect. Deployment happens every connection, so there is no
  persistent agent-code cache or stale-cache path to reason about.
- **Scratch cleanup is lease-based, not age-based.** After extraction the deployed helper
  creates an exact v2 marker and holds an advisory lock for the agent process lifetime.
  Normal shutdown removes the agent's own directory. A later agent may reclaim only a
  valid, unlocked v2 marker whose marker mtime has passed a 24-hour *minimum retention*
  floor. An old process can be live, so age is not evidence of idleness. Legacy prefixes,
  unknown names, malformed or incomplete crash residue, and platforms without the needed
  POSIX no-follow and locking primitives are conservatively retained. This protects
  ordinary cooperating sessions, not a malicious same-UID process racing filesystem swaps.

**Dependencies.** The agent needs PIL (for `capture_scaled`) and, on X11 targets, `python-xlib`. Neither is installed on either target; `pip3` is missing on the WSL box. **`uv` is present on both and is the answer.** The stub locates `uv` by absolute path — `~/.local/bin/uv`, `/opt/homebrew/bin/uv`, `/usr/local/bin/uv`, then `shutil.which` — never trusting `PATH`, mirroring `windows.py::_which_powershell`. It then re-execs under `uv run --with pillow --with python-xlib`. If `uv` cannot be found, **fail loud with the list of paths tried** — the same diagnostic shape `_which_powershell` already produces. Do not attempt a pip fallback; on the WSL target there is no pip to fall back to.

---

## 8. Security model and trust boundary

### 8.1 The boundary

> **Trust boundary: the `authorized_keys` entry on the target.**
> Anyone holding a private key that authenticates as that user can drive that desktop.

Everything else is a consequence. There is no second credential, no listening port, no token, no session secret.

### 8.2 What this adds — and what it honestly does not

**Adds no new authority.** An SSH user on `macos-host` can already run `screencapture` and `osascript`. This bundle is a more ergonomic interface to authority that already exists.

**Can be made *less* powerful than plain SSH.** Use a dedicated key pinned to the agent:

```
command="/usr/local/bin/cu-agent-boot",restrict ssh-ed25519 AAAA… amplifier-computer-use
```

`restrict` disables port/agent/X11 forwarding and PTY allocation; `command=` forces the agent regardless of what the client asks for (the client's request lands in `SSH_ORIGINAL_COMMAND` and is ignored). The computer-use key becomes strictly weaker than a login key. **Recommended, and worth the setup step.**

**Host identity is not optional.** `StrictHostKeyChecking` must be `yes` or `accept-new`. Never `no`. A host-key mismatch on a machine that types your passwords is a MITM signal, and the design's response is to refuse to connect. Tailscale's WireGuard layer makes this unlikely; that is a reason to keep the check, not to skip it.

**Tailscale ACLs are an independent second layer.** Restrict which nodes may reach port 22 on the targets. Good practice, but the design does not depend on it.

**The novel risk is not the transport.** It is that the model reads pixels off a remote screen and those pixels are attacker-influenced content. A crafted web page or email rendered on the target desktop can attempt to steer subsequent actions — indirect prompt injection with a mouse attached. **No transport choice mitigates this.** It is mitigated, partially, by contention policy (§11) and gating (§10), and honestly by scoping: see §10.4.

### 8.3 Upgrade path: Tailscale SSH

The brief notes Tailscale SSH is not enabled on either target; both use native `sshd`. Worth flagging as a strict improvement available later at zero architectural cost:

- Removes key distribution entirely — access is granted by tailnet ACL, identity-checked per connection.
- Provides centralised, revocable, auditable access policy.
- Optional session recording.

It is the *same architecture* with a better identity model — `RemoteBackend` and the wire protocol do not change at all. Recommend adopting it once the transport is proven, not before (it adds a variable to the first thing that has to work).

---

## 9. Failure semantics

House rule: **no fallbacks, no synthetics, no degraded modes.** Applied concretely:

| Failure | Behaviour |
|---|---|
| `target` configured, host unreachable | **Fail loud at mount.** `RemoteBackend` is the only candidate — no fall-through to a local backend. Silently driving *the controller's own desktop* because the remote was down would be the single worst outcome this design can produce. |
| `target` absent (local mode) | Today's behaviour: probe local backends, mount nothing if none available, log the reason, do not raise. |
| Handshake mismatch (protocol / hash / probe / permissions) | Fail loud, disconnect, do not mount. |
| SSH drops mid-action | Pending request resolves to `BackendError`. WRITE ops are **not** retried. Tool returns `success=False`. |
| Reconnect | Permitted **only between actions**, never within one. After reconnect the controller re-runs the handshake, re-resolves geometry, and **injects a note to the model** that the link dropped and all held inputs were released. A model reasoning about a desktop whose modifier state silently changed is a correctness bug, not a cosmetic one. |
| Agent crash | stdout closes → controller sees EOF → same path as SSH drop. |
| Target desktop locked / no GUI session | Backend `probe()` fails → handshake reports unavailable → fail loud. No transport fixes a missing session. |
| Response timeout | Per-op-class deadline. Expiry is a hard failure; the agent is asked to `release_all` on the next successful contact and the session is torn down if that fails. |

The `mount()` distinction between *explicitly configured* and *defaulted* mirrors the precedent already established in this codebase for `target_monitor` (`__init__.py:162-194`): an explicit ask always fails loud; only an unconfigured default is allowed to take a quieter path. Same principle, applied to `target`.

### Where SSH genuinely cannot work

Named honestly, because "SSH everywhere" is not true:

1. **Native Windows target with no WSL and no OpenSSH Server.** Out of scope per the brief, but this is the real gap. It is where a daemon would be justified — and even then, prefer enabling Windows' built-in OpenSSH Server over shipping a bespoke listener.
2. **Locked or logged-out desktop.** No GUI session exists to drive. Not a transport problem.
3. **Wayland Linux targets.** XTEST does not apply; input injection requires compositor-specific portals. Transport-independent; a backend problem.
4. **Users who will not run sshd.** That is the non-technical local user — and §C3 already gives them a path that requires no network at all.

---

## 10. Process lifecycle and safety

### 10.1 Who starts, supervises, reaps

The **controller** does, for the duration of the session. No systemd unit, no launchd plist, no daemon, no autostart. The agent's lifetime is the SSH subprocess's lifetime, which is bounded by the module's mount/cleanup cycle — `mount()` returns a cleanup callable, which the kernel awaits at teardown (`CONTRACTS.md` § Module Lifecycle).

Shutdown order: `release_all` → `bye` → close stdin → wait 2 s → `SIGTERM` the ssh process → wait 2 s → `SIGKILL`.

The bootstrap also registers its scratch-directory cleanup immediately on creation. A raw
crash before it can acquire a v2 lease leaves an unknown/incomplete directory intentionally
untouched. On POSIX targets with secure no-follow opens and advisory locks, a future agent
reclaims only an unlocked, valid v2 lease after the marker has been retained for at least
24 hours; it never removes legacy or unknown names. Where those primitives are unavailable,
automatic stale cleanup is disabled rather than risking a live agent's imports.

### 10.2 The held-input ledger — the most important safety property here

A synthetic `keydown` with no matching `keyup` leaves a real human's desktop broken. The agent must guarantee release, and **it must guarantee it locally**, because by definition the controller cannot reach a machine whose link just died.

The agent maintains a ledger of every currently-held input — mouse buttons from `mouse_down`, keys from `hold_key`, modifiers mid-`key` sequence. Release is triggered by **all** of:

1. **stdin EOF** — verified in §3.3 (`RELEASED:ctrl`). sshd closes the pipe when the client goes away, including on controller `SIGKILL`. This is the primary path and it is reliable.
2. **Deadman timer** — no request for `N` seconds (default 60) → release, exit. This covers the half-open TCP case where EOF may not arrive for minutes. Controller-side `ServerAliveInterval=5, ServerAliveCountMax=2` shortens detection from the other direction, but the agent-side timer is the one that actually protects the desktop.
3. **`SIGTERM` / `SIGHUP` handler** and a `finally` block around the read loop.
4. Explicit `release_all` from the controller.

Every release is written to the audit log so the human can see it happened.

**`drag` stays atomic on the agent.** Never decompose it into `mouse_down` + `move` + `mouse_up` across the wire — a link failure between frames would strand a held button. But note that Anthropic's action set exposes `left_mouse_down` and `left_mouse_up` independently, so the agent *can* legitimately be left mid-drag by the model. The ledger is what covers that case; atomic `drag` merely avoids manufacturing the problem.

### 10.3 Audit trail — mechanism, always on, no toggle

Every action the agent executes appends one JSONL line to `~/.local/state/amplifier-computer-use/audit-<session>.jsonl` **on the target**, owned by the target's user:

```json
{"ts":"2026-08-01T06:11:44Z","session":"a3126f2f","op":"type_text","args_digest":"sha256:…",
 "len":24,"ok":true,"ms":41,"held_after":[]}
```

This is the machine owner's record of what was done to *their* desktop — a different artifact from the controller's transcript, and it belongs on the machine that was acted upon. It survives the controller, and it exists even if the controller is the thing that misbehaved.

**No configuration toggle.** The overhead is a line of JSON next to an LLM round trip; the data cannot be reconstructed after the fact; and an audit log with an off switch is not an audit log. A toggle here would be dead complexity that contradicts its own rationale.

Note `args_digest` rather than `args` for `type_text`: the audit log records *that* text was typed and its length and hash, not the plaintext. Typed content frequently includes credentials.

### 10.4 Safety boundary — what is mechanism, what is policy

**Mechanisms this design provides** (the bundle builds these):

| Mechanism | Notes |
|---|---|
| `read_only` enforced at **both** ends | Currently controller-only (`__init__.py:609`). The agent must enforce it too, so a buggy or compromised controller cannot bypass it. Cheap defence in depth. |
| Audit log on the target | §10.3. Always on. |
| Held-input ledger + guaranteed release | §10.2. Always on. |
| Op classification (READ/WRITE) | §6.3. Enables gating and retry policy to be expressed precisely. |
| Contention signal | §11. Always computed and reported. |
| `type_text` marked sensitive in the wire envelope | Lets a redaction hook act without inspecting payloads. |

**Policy the user chooses** (existing kernel mechanisms, not new code):

Confirmation gates already exist in the kernel. `tool:pre` supports `ask_user` and `deny` (`HOOKS_API.md`). The bundle should **not** build a bespoke confirmation system — it should ship an optional hook module with a default policy and let the user replace it. Choices that are genuinely the user's:

- Gate every WRITE, or only some? (Note: "destructive" is undecidable from a click — a click on **Delete** is indistinguishable from any other click. Any gate finer than "all WRITEs" is guesswork.)
- Gate `type_text` always, or only when the model believes it is a password field?
- `read_only` default on or off for remote targets?

**What is honestly not solvable in code**, and belongs in the README rather than pretending otherwise: this tool can read anything visible on the screen, and screenshots go to the model provider. Screenshot redaction is not tractable — you cannot reliably identify a password field in a bitmap, and a false negative is a leaked credential.

> **Do not point this at a desktop displaying secrets you would not paste into the model's context window.**

That sentence is the real guardrail. Shipping a redaction feature that works most of the time would be worse than shipping this sentence, because it would encourage exactly the usage it cannot actually protect.

---

## 11. Human/agent contention

The user explicitly raised this and it needs their judgment. Framing it precisely first:

There are **four** contended resources, and they do not partition the same way:

| Resource | Partitionable per monitor? |
|---|---|
| Screen pixels (vision) | **Yes** — `capture(region)` already scopes to one monitor |
| Pointer | **Yes** — coordinates are monitor-scoped |
| Keyboard focus | **No** — global. Keystrokes go to the focused window, wherever it is |
| Clipboard | **No** — global, single-slot, and the agent overwrites it |

This is the crux, and it is why per-monitor partitioning is a genuine but **partial** answer.

### The options

**A. Exclusive takeover** — agent drives, human is told to stay off.
Zero mechanism, zero enforcement, and it is the de facto behaviour today. Honest and simple. Fails silently the moment the human forgets.

**B. Observe-only** — `read_only=true`. Agent sees, reasons, never acts.
Already implemented. No contention by construction. Genuinely the right mode for "watch my screen and tell me what's wrong," which is a large fraction of the real use cases. **Should be the recommended default for a desktop the human is actively using.**

**C. Explicit handoff / lease** — agent acquires a visible lease before acting; an always-on-top indicator on the target says "AGENT DRIVING"; a global hotkey revokes.
Strongest UX. But it requires a persistent UI component and a global hotkey listener **on the target**, which contradicts this design's central "no daemon, no resident component" property. That tension is real and should not be glossed over. Build only if D+E proves insufficient in practice.

**D. Per-monitor partitioning** — agent gets `\\.\DISPLAY4`, human keeps 1–3.
The per-monitor work (`monitors.py`, `MonitorInfo`, `select_monitor`) makes this genuinely available *today* for vision and pointer. **But keyboard focus is global**, so the agent can still type into the human's foreground window. Partial mitigation: require `focus_window` before every keyboard action, and constrain the agent to focusing only windows whose bounds lie on its assigned monitor. That converts "silently types into the wrong window" into "focus, then type, with a ~50 ms race." Better, not solved. Clipboard remains fully shared and unfixable by partitioning.

**E. Contention detection with fail-loud** — the agent records cursor position after each of its own input actions; before the next one it re-reads. Moved, and the agent did not move it → a human is active.
Cheap, needs no daemon, no UI, and matches the house rule far better than any prevention scheme: it converts silent interference into a loud stop.

### Recommendation, and the choice that is genuinely yours

**Split E into mechanism and policy.** Computing and reporting the contention signal is free and its absence is unrecoverable — so it is **always on**, always attached to the tool result, and always audited. Whether it *blocks* is the policy knob, because the false-positive rate is real (screensavers, window animations, and apps that warp the cursor all move the pointer without a human).

Recommended defaults:

- **Human actively at the machine → B (observe-only).** Safest, already built, covers a large share of real use.
- **Human present but sharing → D + E-blocking**, with the keyboard hole disclosed.
- **Machine dedicated to the agent → A + E-reporting.** Nothing to contend with; keep the signal for diagnostics.
- **C: do not build yet.**

**This needs your decision, and here is exactly what you are deciding:** whether D's keyboard hole is acceptable for your Windows box. Screenshots and clicks will be correctly confined to `DISPLAY4`. Keystrokes will not be, unless the agent wins a ~50 ms focus race every time. If that is not acceptable, the honest answer is B or A — not a more elaborate D.

---

## 12. Alternatives considered

### Alternative 1 — Custom network daemon (steelmanned, then rejected)

The strongest version: a small service on each target, listening on the Tailscale interface only, mTLS with Tailscale-issued identity, or `tsnet` so it is only reachable inside the tailnet with ACL enforcement. It survives controller restarts, supports multiple concurrent controllers, and needs no code deployment per session.

**Rejected**, because:

- It creates a new authority — a socket that accepts "type this string" — where none existed. That is an RCE primitive with a friendly face.
- It requires a bespoke auth model. SSH's is twenty-five years old and correct.
- It needs installation, autostart, upgrade, and patching on every target. SSH is already running.
- "Survives controller restarts" is a benefit for a *service*. This is a *session-scoped agent*; surviving the session is a liability, because a resident process holding input state with no controller is precisely the stuck-modifier scenario §10.2 exists to prevent.

The one case where it wins is a native Windows target with no WSL and no OpenSSH Server — out of scope, and even there, enabling Windows' built-in OpenSSH Server dominates writing a listener.

### Alternative 2 — VNC/RFB as the transport (the most serious challenger)

Drive the desktop through a VNC client library instead of a custom agent. This is intellectually honest competition: RFB was designed for exactly this shape — pixels down, input up — and its **incremental framebuffer updates** mean a mostly-static desktop costs *kilobytes* per refresh instead of ~515 KB. macOS has a built-in server (Screen Sharing). That is a genuine 10–100× win on the dominant payload, on the dominant hop.

**Rejected**, with the win acknowledged:

- The win is on the target→controller hop only. The model needs a *full frame* to reason about — it cannot consume deltas — so the controller must maintain a framebuffer and serialise a full PNG anyway. The saving is real but narrower than it first appears.
- It requires a VNC server: a new listening port and a new credential — precisely the thing §8 is built to avoid. Windows has no native server.
- RFB's input model is lower-level than what already exists here. No window enumeration, no monitor metadata, no permissioned clipboard. `list_windows` / `focus_window` / `get_clipboard` would all have to be rebuilt or lost.
- It destroys the "one implementation, two deployment shapes" property. Remote would be a wholly separate code path from local, and the 83 tests would cover only half of it.

**Worth stealing later:** framebuffer deltas are a legitimate optimisation *inside* our own protocol — the agent could send a changed-region delta and let the controller composite. That is a Phase 5+ idea, sequenced after correctness.

### Comparison

| Dimension | **SSH + persistent agent** (recommended) | Custom daemon | VNC/RFB transport |
|---|---|---|---|
| **Latency** | Good — 1–8 ms RTT, one round trip per action; Windows still gated by PowerShell spawn (C2) | Good — same, minus ~300 ms one-time connect | Best for pixels (deltas); comparable for input |
| **Complexity** | Low — one subprocess, one protocol, no install | High — service, autostart, upgrade, TLS, auth on every target | Medium-high — new dependency, but mature |
| **Reliability** | Good — one failure domain; stdin-EOF release verified | Fair — resident process can outlive its controller holding input state | Fair — depends on a third-party server's health |
| **Cost** | ~100 KB deploy per connect; ~515 KB per screenshot | ~0 per action after install; ongoing operational cost | Lowest bytes on target→controller |
| **Security** | **Best** — no new authority, no new port, no new credential; `command=` narrows below plain SSH | **Worst** — new RCE-shaped surface, bespoke auth | Poor — new port, new credential, weak legacy auth in some servers |
| **Scalability** | Good to ~dozens of targets; one process each | Better for many controllers per target — not a requirement here | Good |
| **Reversibility** | **High** — `RemoteBackend` is one class behind an existing protocol; delete it and local is untouched | Low — installed software on every target | Low — different code path for remote, permanently |
| **Org fit** | **Excellent** — SSH already works on both targets today; zero new ops | Poor — someone must own a fleet of services | Fair — VNC is familiar but the integration is not |
| **Optimises for** | Security, reversibility, and code unification | Multi-controller access and zero per-session deploy | Pixel bandwidth |
| **Sacrifices** | ~100 KB per connect; needs sshd; Windows latency unaddressed (C2) | A defensible security story | The unified backend and the clean security boundary |

---

## 13. Phased implementation

### Phase 1 — Thinnest end-to-end proof (the only phase with a hard gate)

Prove the full path against **both** real machines. Nothing else ships until this does.

**Build:**
- `agent.py` — NDJSON loop; stdout hygiene (§6.4); handshake with probe + permissions; **held-input ledger with stdin-EOF release, deadman timer, and signal handlers from day one**; ops: `probe`, `screen_geometry`, `list_monitors`, `capture_scaled`, `cursor_position`, `move`, `click`, `type_text`, `key`, `release_all`, `bye`.
- `SshTransport` — one `ssh -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=5 -o ServerAliveCountMax=2` subprocess.
- `RemoteBackend(Backend)` + the `capture_scaled` capability (C1), with `imaging.capture_scaled_b64` gaining the class-descriptor fast path.
- Bootstrap stub with content-addressed deploy over the session's own stdin (§7, mechanics already proven).
- `asyncio.to_thread` at the `execute()` boundary (C4).
- Config: `target: ssh://user@host`, absent ⇒ local (C3).

**The safety ledger is not deferred to a later phase.** It is the property that protects a real person's desktop, and Phase 1 is the phase most likely to crash.

**Evidence required to call Phase 1 done:**

| # | Requirement | How verified |
|---|---|---|
| 1 | Handshake against both targets reports correct platform, backend, monitors, and `permissions.accessibility=true` on macOS | Captured handshake JSON from each host |
| 2 | Screenshot from each target lands on **controller-host's** disk as a valid PNG at exactly `model_width × model_height` | `file` + PIL dimension assert on the controller |
| 3 | Wire payload per screenshot is bounded, and materially below native | Byte count logged; compare against the 1.34 MB / 10.5 MB native baselines |
| 4 | `move` then `cursor_position` round-trips the same coordinates on each target | Assert equality |
| 5 | Deployed hash == agent-reported hash; a deliberately corrupted payload **fails loud** | Both cases exercised |
| 6 | **Kill the SSH client mid-session with a modifier held; the target desktop has no stuck key** | Agent stderr shows `RELEASED:…`; confirm on the target by typing |
| 7 | Unreachable `target` **fails loud at mount** and does *not* fall back to the controller's local desktop | Point `target` at a down host; assert mount raises and no `computer` tool is mounted |
| 8 | Local mode (no `target`) is byte-for-byte unchanged | 83 existing tests still pass |
| 9 | A WRITE op whose response is lost is **not** retried | Fault injection |

Requirements 6 and 7 are the two that matter most. 6 protects the human; 7 prevents the worst possible confusion — the agent driving the wrong machine.

### Phase 2 — Complete the surface
Remaining ops (`drag`, `scroll`, `hold_key`, `mouse_down/up`, `list_windows`, `focus_window`, `get_clipboard`, `set_clipboard`); agent-side `read_only`; audit log on the target; op-class retry policy; reconnect-between-actions with model notification; `uv`-provisioned dependencies with absolute-path discovery.

### Phase 3 — Contention
Contention signal always computed and attached to results (mechanism); `contention: exclusive | observe | partition | detect-and-halt` policy knob; monitor-constrained `focus_window` for partition mode.

### Phase 4 — Persistent PowerShell (the real Windows latency fix)
Replace per-action `subprocess.run` in `windows.py::raw` with a long-lived `powershell.exe` reading NDJSON on its stdin — same framing, one layer down (C2). Independently valuable: it fixes local WSL→Windows too. Sequenced late because it is the riskiest change to the most-verified backend.

### Phase 5 — P1 controller portability, and optimisation
CLI from macOS / Linux / WSL driving local or remote. Mostly free: the controller side is platform-agnostic already. Then, if measurement justifies it: binary framing (`enc` field already reserved) and framebuffer deltas.

---

## 14. Open questions requiring your judgment

Everything else in this document is a recommendation I will stand behind. These three are genuinely yours.

1. **Contention policy for the Windows box (§11).** Per-monitor partitioning gives you correct screenshots and clicks on `DISPLAY4` but *cannot* confine keystrokes — focus is global. Is a ~50 ms focus race acceptable, or do you want observe-only when you are at the machine?

2. **Gate granularity (§10.4).** "Destructive" is undecidable from a click. The honest options are *gate every WRITE* (safe, slow, ~1 approval per action) or *gate none* (fast, trusting). Anything in between is guesswork wearing a confidence costume. Which?

3. **`read_only` default for remote targets.** I lean **on** — a remote target is by definition a machine you are not looking at, and observe-only is the mode that cannot damage it. But it makes the P0 use case ("drive my Windows desktop") require an explicit opt-in, which may be exactly the friction you do not want.

---

## 15. Success metrics

| Metric | Target |
|---|---|
| Input action round trip (macOS, 8 ms RTT) | < 50 ms p50 |
| Input action round trip (Linux X11 target) | < 30 ms p50 |
| Screenshot round trip, end to end | < 500 ms p50; < 1.5 s p99 |
| Screenshot wire payload | Bounded by model-space budget; p99 < 1 MB |
| Stuck-input incidents after abnormal disconnect | **Zero.** Any occurrence is a P0 defect. |
| Silent wrong-target actions | **Zero.** Any occurrence is a P0 defect. |
| Automatic retries of WRITE ops | **Zero, by construction.** |
| Windows action round trip | Tracked but **not** targeted until Phase 4 — gated by PowerShell spawn, not by transport (C2) |
