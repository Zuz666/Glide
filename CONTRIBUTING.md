# Working on Glide

How to run the engine, the tests and the checks, and how a change reaches players. The project's parts and the
interfaces between them are in [docs/REFACTOR-CONTRACTS.md](docs/REFACTOR-CONTRACTS.md); AI agents also follow
[CLAUDE.md](CLAUDE.md).

## Setup

- Python 3.12 or newer (`winget install -e --id Python.Python.3.12`), then
  `py -3 -m pip install -r requirements-dev.txt` (the tests' and linters' packages, pinned; `requirements.txt` holds
  what the engine itself uses: numpy, and faster-whisper for push-to-talk).
- Node 24 and the desktop packages: `cd desktop` then `npm ci`.
- luacheck 1.2.0 for the addon's linter: the Windows `luacheck.exe` from the
  [luacheck releases](https://github.com/lunarmodules/luacheck/releases/tag/v1.2.0) on your PATH (CI downloads the
  same file and checks its SHA-256), or `luarocks install luacheck 1.2.0`.

## Run the engine

The engine is the Python process in `companion/` (`companion/app.py`); the desktop app and `start-glide.cmd` both
run it. For development, run it simulated and without model keys, so nothing reaches a paid API and the coach falls
back to its offline callouts:

```
set ANTHROPIC_API_KEY=
set OPENAI_API_KEY=
set GEMINI_API_KEY=
set GOOGLE_API_KEY=
set OPENROUTER_API_KEY=
set OPENCODE_API_KEY=
py -3 companion\app.py --sim --no-voice --no-overlay --no-ptt --port 8766
```

`GLIDE_SIM_SPEED=10` runs the simulation faster. The dashboard is then at http://127.0.0.1:8766. Pick a free port of
your own: never 8797, where the player's own copy runs. `cd desktop` then `npm run sim` runs the desktop app on the
simulation; a test copy of the app runs muted and hidden (`"muted": true`, `"resumeHidden": true` and
`"closeToTray": true` in its `app-settings.json`) with its own engine home (`GLIDE_ENGINE_HOME`), never on the
player's install. Stop what you started by its process id, never by name.

## Tests

```
py -3 tests/run_all.py              every test, each in its own process (several at once), with a summary
py -3 tests/run_all.py -j 1         one at a time, in order (-j N: N cores' worth); Ctrl+C stops the run at once
py -3 tests/run_all.py trash kb     only the tests whose path contains one of the words
py -3 tests/run_all.py --changed    the fast loop: only the tests your changes since origin/main can reach
                                    (--changed HEAD~3 for another base; tests/affected.py --why FILE says why)
py -3 tests/run_all.py --list       which tests would run, in the order they start (runs none)
py -3 tests/run_all.py --smoke      the report-only checks against your own data (never fail)
```

The runner starts the longest tests first, by this machine's earlier runs (`tests/.timings.json`, not committed; a
fresh checkout uses the list in `run_all.py`). The ones the run's length hangs on keep normal priority and up to
twice -j tests run at once, the rest a step below, so short tests fill the cores the long ones leave idle (WoW and
the app at normal priority stay ahead of them too). `--changed` finds each test's dependencies statically
(`tests/affected.py`: imports, the files and folders the code names, what a command line runs) and runs a test only
when one of them changed; a change to the runner, the requirements or the linters' or TypeScript's settings, or to
code no test reaches, runs everything. In it `test_types` checks only the engine files the change can reach. It is
for iterating: the full run is the check before a commit, and CI always runs everything. After a big change to the
tests, `python3 tests/affected.py --trace` (Linux, strace) checks the map against every file each test really opens.

A test that can't run on this machine (no `data/kb.db`, no game data, no node, no Edge, no linter) prints
`SKIP: <reason>` and exits with 77; the summary lists it apart from passes and failures. A new test that needs data
not in the repository does the same, and tests the rest of what it can. Tests never read this machine's
`config.json` (they pass a temporary root) and never call a model.

On the main development PC several test runs share fixed local ports, so wrap every run in the shared lock (it also
blanks the model keys): `py -3 %USERPROFILE%\Documents\glide-test-lock.py py -3 tests/run_all.py`.

The tests also hold the project's ratchets: `tests/test_lint.py` (`ruff check .` and `luacheck addon/Glide` stay
clean), `tests/test_types.py` (pyright over the engine: a file may not gain type errors; lower its cap when it loses
some), `tests/test_strip_spec.py` (`docs/STRIP.md` is generated from `companion/strip_spec.py`:
`py -3 companion/strip_spec.py --write`), the settings manifest and the desktop's types of the engine state and IPC.

## Golden masters

`tests/golden/` records what each part does today and compares after every change:

```
py -3 tests/golden/golden.py check              all six probes (addon, desktop, dashboard, engine, knowledge, parser)
py -3 tests/golden/golden.py check --portable   only addon, desktop and dashboard: they need just the repository
                                                (plus node and Edge), so their snapshots hold on any machine
```

The other three ("machine" tier) read this PC's `data/glide.db` and `data/kb.db`. The check also builds the desktop
app. A difference is either a bug (fix it) or an intended change: record it in `docs/REFACTOR-CONTRACTS.md` under
"Recorded changes" (what, why, which recommendation), then re-record only that probe:
`py -3 tests/golden/golden.py record <probe>`. Never re-record to make an unexplained difference go away.

`tests/test_golden_addon_replay.py` always checks the database-free addon → engine half of the engine golden,
including on CI without game data. After an intended addon payload or scheduling change, update the dependent
`engine.json` `addon` expectations as well as the addon snapshot. Preserve the machine-dependent `sim` section
when the original databases are unavailable; never replace it with this machine's reduced simulator output.

On the development PC (it has the data), `tests/test_golden.py` inside `run_all` runs `golden.py check` over every
probe, so a green `run_all` with `test_golden.py ok` is the golden check: don't run it a second time. `golden.py`
stays for `record`, a single probe and `--portable` on a machine without the data.

## Linters and builds

- `ruff check .` (settings in `pyproject.toml`), `luacheck addon/Glide` (`.luacheckrc`).
- `cd desktop` then `npm run build` (generates the Settings schema from `companion/settings_manifest.json`, then
  compiles).
- On the development PC, `run_all` covers these too when `test_lint.py`, `test_types.py` and `test_golden.py` pass
  (not SKIP): `test_lint` runs ruff and luacheck, and the golden desktop probe runs `npm run build`. The routine check
  is then one command: `run_all` through the test lock. Elsewhere add what skipped (`golden.py check --portable`,
  the linters you have, `npm run build`).
- CI (`.github/workflows/ci.yml`, on every push and pull request) runs all of these, `tests/run_all.py` and the
  portable golden masters (alongside the tests) on a Windows runner with no game data.

## Releases

Every push to `main` that changes what the installer ships (the engine, knowledge, tools, `wowfiles/`, the addon,
`data/seed/`, `requirements.txt` or `desktop/`) is a release: `.github/workflows/release.yml` runs the CI checks
and, alongside them, builds the installer as the next patch version and uploads it to a draft release on
`Fermin7c1/Glide-releases`; once the checks pass it publishes that draft (only if its notes name this run's commit),
and installed apps update from there. The version is never committed back; `desktop/scripts/next-release.js` picks it. Raise `version`
in `desktop/package.json` by hand only for a new minor or major version. What's new gets an entry from the commit
subjects since the last release unless `desktop/changelog.json` already has one for that version, so write commit
subjects a player can read. Those entries aren't committed back either: each build also fills in every published
release newer than `changelog.json`'s newest entry, from the commits between its source and the release before it
(a release's notes end with `source: <sha>`).

The installer ships only what the engine runs (`build.extraResources` in `desktop/package.json`): a new
development-only script in `tools/` or `knowledge/` belongs in its exclusions.

An addon change bumps the addon's version once, in `addon/Glide/Glide.toc` (the engine reads the expected version
from there).

## Knowledge refresh

The daily community scan commits its results as `Knowledge refresh <YYYY-MM-DD>`: the seed snapshot
(`data/seed/`), the day's intel and digest (`knowledge/community/`), `sources.json`, and
`addon/Glide/Data/QuestIDs.lua` when the quest ID list moved. When QuestIDs.lua changes, the same commit bumps the
addon version in `Glide.toc`, so installed addons pick up the new list.
