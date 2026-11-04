# Contributing

Thank you for your interest in contributing to `amplifier-bundle-computer-use`!

This project welcomes contributions and suggestions. Most contributions require you to
agree to a Contributor License Agreement (CLA) declaring that you have the right to, and
actually do, grant us the rights to use your contribution. For details, visit
<https://cla.opensource.microsoft.com>.

When you submit a pull request, a CLA bot will automatically determine whether you need
to provide a CLA and decorate the PR appropriately (e.g., status check, comment). Simply
follow the instructions provided by the bot. You will only need to do this once across
all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](CODE_OF_CONDUCT.md).
For more information see the
[Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or contact
[opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or
comments.

## Development setup

This project uses [`uv`](https://docs.astral.sh/uv/) for environment and dependency
management. There is no single root `pyproject.toml` with a lockfile — the two modules
(`modules/tool-computer-use`, `modules/hook-computer-use`) are independent, editable
Python packages, and the test suite imports them by inserting each module's path onto
`sys.path` (see any file under `tests/`).

```bash
# from the repo root
uv venv --python 3.12
uv pip install -e modules/tool-computer-use -e modules/hook-computer-use
uv pip install -r tests/requirements.txt "anthropic==1.13.0" "openai==3.29.0"
uv pip check
```

CI uses the same mandatory `tests/requirements.txt`, including both providers pinned
to merged Git commits, the directly imported HTTP clients, and JSON Schema validation.
The SDK versions are test-only pins, not production dependency changes:

| Python | Anthropic SDK | OpenAI SDK |
|---|---|---|
| 3.11 | 1.0.0 | 2.9.0 |
| 3.12 | 1.13.0 | 3.29.0 |

To test the floor row, create the venv with `uv venv --python 3.11` and use
`uv pip install -r tests/requirements.txt "anthropic==1.0.0" "openai==2.9.0"`.
CI recreates its venv with `uv venv --clear` before installing.

`modules/tool-computer-use`'s own `pyproject.toml` declares `pillow` unconditionally and
`python-xlib` / `pyobjc-framework-Quartz` behind `sys_platform` markers, so the editable
install above pulls in the right platform backend automatically. `amplifier-core` is a
runtime dependency of both modules but is not currently declared in either
`pyproject.toml` (installed separately above); this is a known gap, not something to
route around per-contribution.

### Running the test suite

```bash
.venv/bin/python -m pytest tests/ -q
```

Ordinary PR/main CI checks archived neutral evidence **AS RECORDED** against its
independently established expectations, including deep hashed artifact references.
That integrity check is neither real-time freshness nor a match to today's checkout
or installed packages; it does not grant current release qualification.

**Tests must pass on Linux with no desktop present.** This project targets CI on
`ubuntu-latest` with no X server, no Xvfb, no display of any kind — the suite is
designed for that and stubs/fakes the platform layer rather than driving a real screen.
If you find yourself reaching for a real display, framebuffer, or GUI toolkit to make a
test pass, that test belongs in the ship gate (below), not in `tests/`.

### Linting and formatting

This project uses [Ruff](https://docs.astral.sh/ruff/) for both formatting and linting,
configured in the root `ruff.toml`.

```bash
uvx ruff@0.16.1 format --check .
uvx ruff@0.16.1 check .
```

Run `uvx ruff@0.16.1 format .` (no `--check`) to auto-format, and `uvx ruff@0.16.1 check --fix .` to
apply safe auto-fixes, before committing.

### The ship gate

`scripts/verify_coexistence.py` defaults to **conditional Linux boundary qualification**
under policy `coexistence-boundary-v2` (2026-11-04), not a universal physical-input
safety claim. Read `docs/designs/coexistence.md` §11.1 before interpreting it.
Focused headless tests exercise the maintained classifier/guard and harness logic:

```bash
.venv/bin/python -m pytest tests/test_verify_coexistence.py tests/test_presence.py tests/test_halt_invariant.py -q
```

Native qualification is **not CI**. Its owner must supply an already authorized,
isolated X11 test display, not a person's desktop or an ambient guessed `DISPLAY`.
The script provisions no environment. The owner retains display cleanup. Run once
with a new output filename; an external deadline bounds OS/Xlib stalls:

```bash
timeout 90s .venv/bin/python scripts/verify_coexistence.py --mode boundary \
  --display "$OWNED_TEST_DISPLAY" --output coexistence-boundary-NEW.json
```

Six predeclared controls run at most once: 6 s quiet typing; nominal 2 ms inside;
nominal 8/15/55 ms outside; and a forced post-sample/pre-write barrier. Allow roughly
22 seconds plus native setup. The 60 ms interval is the **O5 reference cadence**,
not built-in production pacing. One independent XTEST child acknowledges readiness
and each event, with matching IDs and send-before/sync-after monotonic intervals.
Each case needs a fresh finite `QUIET`/high-confidence baseline above the quiet floor.
An independent Xlib connection owns a mapped window at (0,0), covering the child
motion coordinates, and keyboard focus. Structural setup emits no warmup input.
The existing keymap must map `a` at unshifted level 0; otherwise qualification
refuses before input. Before each batch the receiver queue must be empty. Each
own acknowledgement requires exactly the matching native key press/release pair;
each child acknowledgement requires exactly its matching motion. Window, detail,
coordinates, `SendEvent=False` and strictly advancing receiver ordinals are checked.
Stale, missing, extra or replayed input invalidates the case. The receiver is also
checked after every attempted backend call, including refusals: ordinary native
input after activity/halt is **FAIL**, even without a logged emission callback.

Delivery alone does not establish **counter registration**. A harness-private
read-only SYNC 3.1 binding discovers `SERVERTIME`. On the backend's same Xlib
connection it queries `S0`, the actual MIT-SCREEN-SAVER idle value, then `S1`.
This brackets last input in server milliseconds as `[S0 − idle, S1 − idle]`,
including the idle query **inside each actual production monitor sample**.
Every acknowledged own unit also needs a separate bracketed raw idle query,
without sampling/updating the monitor. The last delivered event's server timestamp
must lie in this bracket, which must be later than preceding competing input; same-tick
or otherwise ambiguous attribution is `INVALID`. Low-32-bit event timestamps are
unwrapped relative to the 64-bit server counter, never Python time. Regressing,
broken, noninteger, missing or wrap-ambiguous evidence refuses qualification;
there is no clock fallback or extra quantisation allowance.

Send/sync and completed-sample **monotonic intervals** establish placement against
the production injection anchor; sleep requests and server-to-Python inversion do
not. This checks delivery plus idle-counter registration, not typed text, physical
origin or general display semantics. Extra queries add harness overhead, not
production changes. Proven outside-band quiet responses remain **FAIL**, even if
later own registration fails. Unresolvable barrier advancement stays `INVALID`.
No retry, delay adjustment or latency fallback rescues an unplaceable 2 ms control.

Quiet timing starts at the first acknowledged emission, not the first call's
start. Each next unit is scheduled from the preceding acknowledgement: at least
60 ms acknowledgement-to-next-send and at most 80 ms acknowledgement-to-next-
acknowledgement, with at least 6 s from first to last acknowledgement. There are
no catch-up bursts. Early wakes, excessive lateness or shorter spans are `INVALID`.
These fixed scheduling limits and extra raw queries belong only to this harness;
this is not a natural production timing benchmark.

Every recorded `human_active` observation, in every case and follow-up, forbids
that unit's and later ordinary emissions and requires `HaltedError`, a release
callback within that exact guard invocation, and a subsequent refused attempt.
Callbacks carry invocation IDs and timestamps; retry-only release cannot cover
an omitted first release. Invalid/straddling/missing evidence is retained, never
retried or replaced. The first failure stops the run. The barrier is artificial,
not a measurement of the natural race. An inside event may be masked or detected.

The new JSON receipt binds schema `coexistence-qualification-v1`, policy, run ID,
source hashes and bounded case timelines. Existing paths are refused before input.
Exit 0 / `MECHANISM_CONTROLS_PASS` qualifies only these Linux controls, not adoption
or general safety. A generic `PASS` is not a valid successful top-level verdict.
Exit 2 / `UNQUALIFIED` does not qualify them.
An external interruption can leave `INCOMPLETE` or a partial/unreadable final
receipt; neither qualifies or authorizes automatic replay. Native child/connection
cleanup is owned by independent finally paths: child close/reap, restore saved
valid focus, destroy the receiver window, close its connection, and close the
backend. Missing or failed cleanup cannot qualify. The external supervisor must
also reconcile an interrupted process.
Release **callback invocation** is not proof of real held-ledger release or current
mounted permissions/Stop. Main owns native review/current evidence/adoption; Windows
retains the original trial. Do not publish private host details or raw logs.

The **historical** command at source `bf3adece` was
`.venv/bin/python scripts/verify_coexistence.py --trials 100` (with its explicitly
owned display). Its 89/100, 11 misses, zero false positives result remains **FAILED**
under that source's AND rule. Do not rerun until green or reinterpret it under the
new policy. The paired comparison did not establish a diagnostic timing regression;
production diagnostics remain unchanged.

Separately, Main's **single** native attempt at PR37 `c76b98e` remains
**UNQUALIFIED**: quiet unit 49/101 after 5.75 s, before the other five controls.
An idle value of 2 ms returned 0.065–0.292 ms after own acknowledgement made the
old ±1 ms inverse interval end 0.432571 ms before emission. This invalidates
that registration assumption, not production safety. The installed Xvfb clock
choice was not traced. Policy v2 does not relabel this attempt or the 89/100.
See §11.1 for pinned Xorg semantics and the headless numeric regression.

Optional **offline analysis only**, with a new receipt:

```bash
.venv/bin/python scripts/verify_coexistence.py --mode random-model \
  --trials 100 --misses 11 --output coexistence-model-NEW.json
```

This collects no input or random trials. The fixed-α=0.05 exact binomial tail for
`q ≤ 5/60` is conditional; with 11 misses it is about 0.210558, not rejection.
Counts alone cannot establish phases, independence, counter registration, release,
zero false positives or explained misses, so the result stays `UNSUPPORTED` /
`UNQUALIFIED` (exit 2), never safety PASS. No seed is invented for supplied counts.
If every physical event must prevent the next write, that product requirement is
**unmet** by timestamp masking; changing tests does not implement it.

### Neutral boundary and retained wire obligation

Production computer-use exports neutral v1 intent and executes canonical actions.
Providers own models, native declarations/headers, vendor units, safety requirements
and replay. Do not import SDKs, probe private provider methods or infer intent from
tool names in the production tool/hook.

The advertised input schema is a guide. Runtime validation enforces flat-versus-batch
exclusivity and validates the entire batch before any disclosure or desktop input.
Write approval belongs to the validated executing call and its pinned target/policy;
concurrent or later calls must never inherit another call's permission.

`scripts/wire_check.py` is a retained historical instrument for the retired bundle
dialects, **not the neutral-v1 qualification path**. Do not run it to manufacture fresh
acceptance. The credential-bearing command below is historical guidance only:

```bash
ANTHROPIC_API_KEY=... OPENAI_API_KEY=... .venv/bin/python scripts/wire_check.py
```

`tests/fixtures/wire-check.json` and historical captures remain unchanged and are
checked as history, not current qualification. The separate pre-release consumer requires
`tests/fixtures/neutral-qualification.json` and independently established
`neutral-qualification-expected.json`, with safe relative receipts/captures under
that evidence root. Missing, failed, future, older-than-30-day, tampered, wrong-profile
or unbound evidence fails; historical HTTP 200 is never a fallback.
The committed qualification is a bounded observation, not a permanent release grant.
Before making a release claim, this strict command must pass on **both SDK rows**
listed above; it checks current checkout/script manifests, actually loaded packages,
the real-time 30-day window and deep artifact integrity:

```bash
.venv/bin/python -m pytest release_checks/ -q
```

After merge, use **Actions → CI → Run workflow** for that two-row check.
`workflow_dispatch` becomes available when this workflow is on the default branch.
Ordinary PR/main runs execute `tests/` only. Dispatch checks existing evidence offline;
it does not regenerate evidence or enforce release publication, tags or branch
protection. Paid provider calls and an owned display are needed only for actual fresh
live regeneration, not these checks. Refresh genuinely expired or changed-binding
evidence through independent qualification, never by editing dates or prior verdicts.
Native live cases are screenshot-only; the owned Linux
X11 approval-policy case verifies byte-exact alphanumeric input, not general text
fidelity or other platforms. A successful later check never relabels an earlier failure.
Provider owners supply native declaration/result/replay and actual wire settings,
finite generation/count HTTP limits and retry observations independently. The neutral
recorder does not inspect protocol fields or certify truth from hashes/model names.
For each configured provider, current/live Session and independent provider/policy
receipts plus exact current/offline and floor/offline installed SDK/composition
receipts are mandatory. Floor qualification is explicitly offline-only. Each binding
includes actually loaded Core/provider/SDK/tool/hook/loop/context package manifests.
Package identities cover every runtime file, including PowerShell scripts and data;
only generated bytecode (`__pycache__`, `.pyc`, `.pyo`) is excluded. The production
manifest also binds each module's `pyproject.toml`. Producer script digests and
production manifests bind final source without fixture or HEAD hash recursion.
Existing Python-only qualification receipts remain historical evidence and cannot
qualify this expanded binding; fresh independent qualification is required before
a release claim. Providers/models are opaque outside provider-owned checks.
The independent verifier owns normal-installed combined checks and remaining live/hardware decisions.

### Public neutral reproduction (test-only)

`scripts/qualify_neutral_session.py` uses public provider `mount`, Core Session and
ModuleLoader, canonical capture, and the production image hook. It starts exactly one
owned Xvfb using `-displayfd`, refuses ambient DISPLAY, renders fresh undisclosed
markers, and checks two actual adjacent Session cycles retaining image history. Its
native declaration is a **restricted screenshot-only test**, not evidence that a
full-operation tool selects native semantics. Captures come from actual tool results;
no private SDK/client/replay seeding or source-path provider injection is permitted.
Use normal installed reviewed packages, not editable provider/Core/SDK dependencies.
Provide an independently established expected binding, public module name, opaque
model, key environment **name** (the key value remains only in the environment), and
provider-owned count policy/provenance. No credential is accepted as a CLI/config
value. Import and `--help` are offline and never provision a display.

```bash
.venv/bin/python scripts/qualify_neutral_session.py --help
# Run ONLY in a deliberately owned Linux X11 test environment, with no ambient DISPLAY.
.venv/bin/python scripts/qualify_neutral_session.py \
  --expected qualification-expected.json --provider PROVIDER_ID \
  --provider-module PUBLIC_MODULE --model MODEL --keyenv CALLER_KEY_ENV \
  --count-policy provider-owned-count-policy.json --output private-new-run \
  --max-complete 6 --max-budget 12 --max-estimate 12 --deadline-seconds 240
.venv/bin/python scripts/neutral_qualification.py record rows.json \
  qualification-expected.json --output neutral-qualification.json
.venv/bin/python scripts/neutral_qualification.py validate neutral-qualification.json \
  qualification-expected.json --evidence-root qualification-evidence
```

The `neutral_qualification.py validate` CLI checks the supplied expectations, not
their agreement with the current checkout or installed packages. Use the
`release_checks/` command above for those current-binding checks and deep integrity.

The producer records observed public `complete`/`request_budget`/`estimate` invocation
counts (absent public methods have zero observations), **not physical HTTP counts**.
Generation retries zero and auto-continue false are requested through public mount
config; effective wire delivery remains UNKNOWN in this producer. The consumer requires
provider-owned `generation_controls`: outer retries exactly zero, transport retries
bounded to zero or two (two is not disabled), and auto-continue false or `"N/A"`.
Safe hashed provider proof envelopes bind exact packages/source/profile and timestamps
within the provider receipt, with positive public mount/factory/generation-path,
source-provenance, configuration/transport-bound and actual mounted-run checks.
False requires affirmative continuation-disabled proof; `"N/A"` requires both knob
and same-output feature absence, never silence. Source provenance must include a source
ref matching the bound provider Python manifest; mounted-run refs must be observations.
The independent verifier independently admits relevance and truth against public source/configuration and the monitored
public-mounted run; hashes, owner strings and PASS do not authenticate claims, and
an unrelated injected-client run is invalid. Count retries remain separately bounded
provider-owned policy; actual HTTP bounds/outcomes are still mandatory, and observed
zero retries do not prove disabled configuration. Absent proof cannot produce a live
PASS. Sanitize receipt bytes before hashing; only this owned desktop's pixels are allowed.
Published evidence uses consistent service-ID aliases and opaque positive-integer
process-role tokens, not actual host OS PIDs. The accompanying identifier-projection
observation explicitly distinguishes those tokens from privately verified originals,
retains high-entropy original-wire hash commitments and an independent-verifier
attestation for the private session, and omits opaque encrypted reasoning content.
Original session digests are not published: their removed process numbers have low
entropy and a public unsalted digest would permit offline recovery guesses.
Published artifact hashes bind the projected bytes; outcome, capture, package, budget
and timestamp observations are unchanged. Sanitization must never invent an execution
or convert a failed observation into a pass.
Failure produces a failed/partial receipt after setup; no observation is backfilled.
Default finally cleanup uses the public Session/provider lifecycle, ignores hook
mount metadata that is not a cleanup callable, and closes Tk/backend/Xvfb. A separate
CLI supervisor bounds synchronous stalls in an exclusive owned worker process group;
Xvfb/helpers inherit that group. It observes worker exit with WNOWAIT before its final
owned-group signal, then reaps and checks group absence. The consumer requires this
final supervision record, so an interim worker receipt cannot satisfy acceptance.
Generation image observations are distinct from count/budget observations. Caller
purity is observed outside the production hook's lazy public event projection, while
an inner observer checks the projected request. All teardown waits use the overall
deadline; no ambient/human process group is signalled. The private
output retains owned captures/receipts for inspection; the caller owns their eventual
removal. No foreign process sweep, persistent display, or host installation is needed.

Unit controls in `tests/test_neutral_qualification.py` are explicitly synthetic,
created only in temporary test directories, and rejected by the default release
validator. They prove consumer/controller behavior, not actual wire/GUI acceptance.
The ordinary suite may PASS while the separate pre-release check is RED pending
the independent verifier's genuine qualification; do not skip or weaken that release gate.

Focused headless composition requires real qualified provider wheels and SDKs with HTTP
intercepted, never optional SDK skips or private provenance injection:

```bash
.venv/bin/python -m pytest tests/test_neutral_computer.py tests/test_native_screenshot_preflight.py tests/test_provider_neutral_composition.py -q
```

## Commit conventions

This project uses [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <short summary>

<optional body — what changed, why, and impact>
```

Common types used in this history: `feat`, `fix`, `docs`, `chore`. See `CHANGELOG.md` and
`git log` for examples in this repo's own style — most bodies here explain the defect
being fixed and why it happened, not just what changed.

## The evidence standard

**This is the standard this project holds itself to, and the standard your contribution
will be held to.** A claim about platform behavior — timing, detection latency, whether a
permission dialog renders, whether an input event actually reached a window — must be
backed by a real run against real hardware (a real X server, a real macOS session, a real
Windows box), not by reasoning about what "should" happen.

Unmeasured values must be labeled as unmeasured, not backfilled with a plausible-looking
number. `modules/tool-computer-use/amplifier_module_tool_computer_use/presence.py`'s
`GUARD_MEASURED` flag exists for exactly this reason — it is a per-platform record of
whether the guard-band figure it sits next to came from an actual measured run or is
still an inferred placeholder. Flip it to `True` only when you have the run that justifies
it, and keep the run's evidence with the change (see the pull request template's evidence
checkbox).

This standard exists because the failure mode it prevents is expensive: inferred numbers
that look measured get trusted, get built on, and the gap between "we think" and "we
proved" only surfaces once something the mechanism was supposed to prevent has already
happened at production scale.
