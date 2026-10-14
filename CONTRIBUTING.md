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

`scripts/verify_coexistence.py` is the pre-release gate for the human/agent coexistence
mechanism — a statistical, real-process evidence run (not
a mock, not a unit test) that proves the presence detector still catches a human touching
the input devices mid-injection, at production cadence, across a large sample of trials.
It needs a real (if headless) X server:

```bash
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
XVFB_PID=$!
trap 'kill "$XVFB_PID" 2>/dev/null' EXIT
DISPLAY=:99 .venv/bin/python scripts/verify_coexistence.py
```

**Kill the `Xvfb` you started — the `trap` above does this for you.** A backgrounded
`Xvfb` with no cleanup is easy to forget, and a forgotten one does not just waste a
process slot: `DISPLAY=:99` can leak into a later shell (a sourced rc file, a tmux pane,
an inherited environment), and `LinuxX11Backend.probe()` used to accept *any* reachable,
XTEST-capable X server as "the" desktop — including a stray `Xvfb` left running for days.
`probe()` now checks a blindly-picked-up `DISPLAY` against this user's own login session
(`systemctl --user show-environment`) before trusting it, but there is still no reason to
leave the test server running — kill it when you are done.

This takes several minutes (dozens to ~100+ real trials, each a real subprocess spawn) and
is **not** run in CI — CI runs headless with no display server at all. Run it locally,
against a clean `Xvfb`, before cutting a release that touches the coexistence guard, the
presence detector, or the input backends it depends on. See the script's own docstring
for what evidence it produces and how to read the result. The gate establishes a fresh
`QUIET`/high-confidence baseline before **every** trial, including the first; a baseline
that is unreadable, recent, or otherwise invalid fails the whole run and is never retried
or excluded from its denominator. Do not publish private host details or raw run logs.

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
includes actually loaded Core/provider/SDK/tool/hook/loop/context Python manifests;
producer script digests and production manifests bind final source without fixture
or HEAD hash recursion. Providers/models are opaque outside provider-owned checks.
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
