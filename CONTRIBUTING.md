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
uv venv
uv pip install -e modules/tool-computer-use -e modules/hook-computer-use
uv pip install amplifier-core pytest pytest-asyncio
```

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

**Tests must pass on Linux with no desktop present.** This project targets CI on
`ubuntu-latest` with no X server, no Xvfb, no display of any kind — the suite is
designed for that and stubs/fakes the platform layer rather than driving a real screen.
If you find yourself reaching for a real display, framebuffer, or GUI toolkit to make a
test pass, that test belongs in the ship gate (below), not in `tests/`.

### Linting and formatting

This project uses [Ruff](https://docs.astral.sh/ruff/) for both formatting and linting,
configured in the root `ruff.toml`.

```bash
uvx ruff format --check .
uvx ruff check .
```

Run `uvx ruff format .` (no `--check`) to auto-format, and `uvx ruff check --fix .` to
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

`scripts/wire_check.py` is the same kind of gate for the multi-provider wire-format
anti-regression scheme (the design notes §11.2, layer 3): it sends
one minimal, real, declaration-only request per provider (the exact shape
`providers.py`'s `Dialect.declare()` emits) and records the result in
`tests/fixtures/wire-check.json`. It needs real credentials and the network, so it is
**not** run in CI either:

```bash
ANTHROPIC_API_KEY=... OPENAI_API_KEY=... .venv/bin/python scripts/wire_check.py
```

Run it periodically (at least every `MAX_AGE_DAYS`, currently 30 —
`tests/test_wire_attestation_freshness.py`) and before cutting a release that touches a
provider dialect. Unlike the coexistence gate, staleness here **is** enforced in the
normal offline test suite: `test_wire_attestation_freshness.py` fails the build if the
attestation this script writes is missing, records a rejection, or has gone stale — see
that module's docstring for why the attestation and its freshness check are deliberately
two different files.

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
