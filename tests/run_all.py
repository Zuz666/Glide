#!/usr/bin/env python3
"""Run every test (tests/**/test_*.py, each in its own process, in parallel) and print a summary; exit 1 if any failed.

    py -3 tests/run_all.py            all of them (in parallel across worker processes)
    py -3 tests/run_all.py -j 4       4 cores' worth (-j 1: one at a time, in order)
    py -3 tests/run_all.py trash kb   only the ones whose path contains one of the words
    py -3 tests/run_all.py --changed  only the tests the changes since origin/main can reach (tests/affected.py): the
                                      fast loop while iterating; --changed REF (or --changed=REF) for the changes
                                      since REF. The full run stays the check before a commit, and CI runs it
    py -3 tests/run_all.py --list     which tests would run, in the order they start, and run none
    py -3 tests/run_all.py --smoke    the report-only checks on the player's real data instead (tests/**/smoke_*.py,
                                      see tests/kb/smoke.py): they print findings and never fail the run

A test passes with exit code 0. One that cannot run here (no data/kb.db, no node, no browser, ...) prints
"SKIP: <reason>" and exits with SKIP_EXIT (77): the summary lists it apart from the passes and the failures.
tests/golden holds the golden-master probes, run by tests/test_golden.py and golden.py (no test_*.py there).

In parallel, the longest tests start first, by how long each took in this machine's earlier runs (tests/.timings.json,
not committed; without it the hand-kept _HEAVY list). The few the run's length hangs on (at least half as long as the
longest) run at normal priority and the rest a step below, with up to 2 * -j tests at once: a short test takes a core
the moment a long one leaves it idle, and never slows the long ones down. (Measured on a 4-core Linux box, -j 4: 33.4
s with the old fixed order and -j tests at once, 24.7 s longest-first with priority, 23.4 s with 2 * -j at once.)

Needs the packages in requirements-dev.txt (lupa for the addon tests): py -3 -m pip install -r requirements-dev.txt"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import glob
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKIP_EXIT = 77
# this machine's wall (and, where it can be measured, CPU) seconds per test, from its earlier runs (gitignored; a
# test of the runner points it elsewhere)
TIMINGS = os.environ.get("GLIDE_TEST_TIMINGS") or os.path.join(HERE, ".timings.json")
# Without timings (a fresh checkout, a CI runner without its cache): the longest-running tests, longest first (by
# the Windows CI runs), so none is left alone at the tail of the run.
_HEAVY = ("test_golden.py", "test_navmesh.py", "test_rxp_explorer.py", "test_rxp_facts.py", "test_dashboard_page.py",
          "test_rxp_routes.py", "kb/test_rxp_updates.py", "test_types.py", "test_desktop_main.py", "test_link.py",
          "test_models.py")
SLOTS = 2            # tests at once per -j while the long ones have priority
RISE = 1.25          # a recorded time rises at most this much per run (and falls at once)
_PAID_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY")
_REF = re.compile(r"^(HEAD|FETCH_HEAD|ORIG_HEAD|[0-9a-f]{7,40})([~^].*)?$|/|[~^]")   # --changed REF, not a word


def collect(pattern):
    """tests/**/<pattern>, sorted by path (tests/ first, then the subfolders)."""
    found = glob.glob(os.path.join(HERE, "**", pattern), recursive=True)
    return sorted((p for p in found if "__pycache__" not in p),
                  key=lambda p: (os.path.dirname(p) != HERE, os.path.relpath(p, HERE).lower()))


def skip_reason(out):
    for line in reversed(out.strip().splitlines()):
        if line.startswith("SKIP:"):
            return line[5:].strip()
    return "(no reason printed)"


def _parse_args(argv):
    """-> dict: smoke, jobs, words, changed (None, or the git ref to compare with), list"""
    o = {"smoke": False, "jobs": min(8, os.cpu_count() or 4), "words": [], "changed": None, "list": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--smoke":
            o["smoke"] = True
        elif a == "--list":
            o["list"] = True
        elif a.startswith("--changed="):
            o["changed"] = a.partition("=")[2] or "origin/main"
        elif a == "--changed":
            o["changed"] = "origin/main"
            if i + 1 < len(argv) and _REF.search(argv[i + 1]):
                o["changed"] = argv[i + 1]
                i += 1
        elif a in ("-j", "--jobs") and i + 1 < len(argv):
            o["jobs"] = max(1, int(argv[i + 1]))
            i += 1
        elif a.startswith("-j") and a[2:].isdigit():
            o["jobs"] = max(1, int(a[2:]))
        elif a.startswith("--jobs=") and a[7:].isdigit():
            o["jobs"] = max(1, int(a[7:]))
        else:
            o["words"].append(a.lower())
        i += 1
    return o


def _name(t):
    return os.path.relpath(t, HERE).replace(os.sep, "/")


# ---- timings ---------------------------------------------------------------------------------------------------------
def load_timings(path=None):
    """{test name: {"wall": s, "cpu": s (POSIX only)}} from earlier runs; {} when there are none or the file is bad"""
    try:
        with open(path or TIMINGS, encoding="utf-8") as f:
            d = json.load(f)
        return {k: v for k, v in d["tests"].items()
                if isinstance(v, dict) and isinstance(v.get("wall"), (int, float)) and v["wall"] > 0}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def merge_timings(old, ran, existing):
    """The times of the tests that passed or skipped in this run, the older ones for the rest (of the tests that still
    exist). A time rises at most a quarter per run, and falls at once: one run slowed by other work on the machine
    doesn't make a test look longer than it is, and a test made faster moves down the order the next run."""
    keep = {k: v for k, v in old.items() if k in existing}
    for name, new in ran.items():
        was = keep.get(name) or {}
        keep[name] = {k: round(min(v, was[k] * RISE) if was.get(k) else v, 2) for k, v in new.items()}
    return keep


def save_timings(timings, path=None):
    """Best effort (another run may hold the file; the times are only a hint): written whole, or not at all."""
    path = path or TIMINGS
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tests": timings}, f, indent=0, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---- order and priority ----------------------------------------------------------------------------------------------
def order(tests, timings):
    """Longest first, by the recorded wall time (a test without one first of all: its length is unknown); the
    hand-kept _HEAVY order when fewer than half the tests have a time. Ties keep the collection order."""
    names = [_name(t) for t in tests]
    if 2 * sum(n in timings for n in names) < len(names):
        heavy = {h: i for i, h in enumerate(_HEAVY)}
        return sorted(tests, key=lambda t: heavy.get(_name(t), len(heavy)))
    return sorted(tests, key=lambda t: -timings[_name(t)]["wall"] if _name(t) in timings else -float("inf"))


def critical(tests, timings, jobs):
    """The tests the run's length hangs on: those at least half as long as the longest. They keep normal priority and
    the others run a step below, so a short test squeezed in beside them never takes their core. None (everything at
    normal priority) for -j 1, or when fewer than half the tests have a time."""
    known = [timings[_name(t)]["wall"] for t in tests if _name(t) in timings]
    if jobs == 1 or not known or 2 * len(known) < len(tests):
        return set()
    longest = max(known)
    return {t for t in tests if _name(t) in timings and timings[_name(t)]["wall"] >= longest / 2}


# ---- one test --------------------------------------------------------------------------------------------------------
def _run_one(t, low=False, env=None):
    """-> name, exit code, output, wall seconds, CPU seconds of the test and everything it started (None where that
    can't be measured: Windows). low: one step below normal priority (what the test starts inherits it)."""
    s = time.time()
    flags = subprocess.BELOW_NORMAL_PRIORITY_CLASS if low and os.name == "nt" else 0
    p = subprocess.Popen([sys.executable, t], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env,
                         creationflags=flags)
    if low and hasattr(os, "setpriority"):
        try:
            os.setpriority(os.PRIO_PROCESS, p.pid, 10)
        except OSError:
            pass
    cpu = None
    if hasattr(os, "wait4"):
        with p.stdout:
            out = p.stdout.read()
        _, status, ru = os.wait4(p.pid, 0)
        p.returncode = os.waitstatus_to_exitcode(status)
        cpu = ru.ru_utime + ru.ru_stime
    else:
        out, _ = p.communicate()
    return _name(t), p.returncode, out, time.time() - s, cpu


def _fast_loop(tests, base):
    """--changed: (the tests the changes since `base` can reach, the environment for them, a note to print)"""
    import affected
    files = affected.changed_files(base)
    deps, repo = affected.dependency_map()
    pick = set(affected.affected(files, deps, repo)[0])
    rel = {t: os.path.relpath(t, ROOT).replace(os.sep, "/") for t in tests}
    chosen = [t for t in tests if rel[t] in pick]
    env = {k: ("" if k in _PAID_KEYS else v) for k, v in os.environ.items() if k != "GLIDE_TYPES_FILES"}
    sub = affected.pyright_files(files, repo)
    if sub is not None:   # test_types checks only the engine files the change can reach
        env["GLIDE_TYPES_FILES"] = "\n".join(sub)
    note = ("fast loop: %d of %d tests for %d files changed since %s (the full run is the check before a commit)"
            % (len(chosen), len(tests), len(files), base))
    return chosen, env, note


def main():
    o = _parse_args(sys.argv[1:])
    if not o["list"]:
        try:
            import lupa  # noqa: F401
        except ImportError:
            print("lupa is missing (the addon tests need it): py -3 -m pip install -r requirements-dev.txt")
            return 1
    smoke, jobs = o["smoke"], o["jobs"]
    tests = collect("smoke_*.py" if smoke else "test_*.py")
    if o["words"]:
        tests = [t for t in tests if any(w in os.path.relpath(t, HERE).lower() for w in o["words"])]
    env = {k: ("" if not smoke and k in _PAID_KEYS else v)
           for k, v in os.environ.items() if k != "GLIDE_TYPES_FILES"}   # a full run checks the whole engine
    if o["changed"] and not smoke:
        tests, env, note = _fast_loop(tests, o["changed"])
        print(note, flush=True)
    failed, skipped = [], []
    t0 = time.time()
    if smoke:
        for t in tests:
            name, _, stdout, dt, _ = _run_one(t)
            print("%s  %5.1fs" % (name, dt))
            print("\n".join("  " + line for line in stdout.rstrip().splitlines()))
        print("\n%d smoke files run (report only: findings never fail the run), %.0fs" % (len(tests), time.time() - t0))
        return 0

    timings = load_timings()
    queue = order(tests, timings) if jobs > 1 else list(tests)
    first = critical(queue, timings, jobs)
    if o["list"]:
        for t in queue:
            w = timings.get(_name(t), {}).get("wall")
            print("%-38s %s  %s" % (_name(t), "normal" if not first or t in first else "low   ",
                                    "%5.1fs" % w if w else "    ?"))
        print("\n%d tests, -j %d" % (len(queue), jobs))
        return 0
    # with the long tests at normal priority, as many again can wait a step below them: a short test then takes a core
    # the moment a long one leaves it idle (a test waiting on a server, a browser or a timer leaves its core idle)
    pool = ThreadPoolExecutor(max_workers=max(1, min(SLOTS * jobs if first else jobs, len(queue))))
    ran = {}
    reported = 0
    try:
        # submitted in order: the pool starts them in that order as workers free up
        futs = {pool.submit(_run_one, t, bool(first) and t not in first, env): i for i, t in enumerate(queue)}
        pending = set(futs)
        while pending:
            # a timeout, so Ctrl+C reaches this thread on Windows too (a wait without one can't be interrupted there)
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for fut in sorted(done, key=futs.get):
                name, code, stdout, dt, cpu = fut.result()
                reported += 1
                status = "ok  " if code == 0 else "SKIP" if code == SKIP_EXIT else "FAIL"
                print("%-38s %s  %5.1fs" % (name, status, dt), flush=True)
                # (in the fast loop test_types checks only part of the engine: its time there is no guide)
                if code in (0, SKIP_EXIT) and not (env.get("GLIDE_TYPES_FILES") is not None
                                                   and name == "test_types.py"):
                    ran[name] = {"wall": max(round(dt, 2), 0.01)}
                    if cpu is not None:
                        ran[name]["cpu"] = max(round(cpu, 2), 0.01)
                if code == SKIP_EXIT:
                    skipped.append((name, skip_reason(stdout)))
                elif code != 0:
                    failed.append(name)
                    print("    " + "\n    ".join(stdout.strip().splitlines()[-6:]), flush=True)
    except KeyboardInterrupt:
        # Ctrl+C: the running tests got it too (same console); the queued ones never start
        pool.shutdown(wait=False, cancel_futures=True)
        print("\ninterrupted: %d of %d tests reported, %s failed so far" % (
            reported, len(tests), ", ".join(sorted(failed)) or "none"), flush=True)
        return 130
    pool.shutdown()
    save_timings(merge_timings(timings, ran, {_name(t) for t in collect("test_*.py")}))
    print("\n%d tests: %d ok, %d skipped, %d failed, %.0fs" % (
        len(tests), len(tests) - len(failed) - len(skipped), len(skipped), len(failed), time.time() - t0))
    if skipped:
        print("SKIP:")
        for name, why in sorted(skipped):
            print("  %-36s %s" % (name, why))
    if failed:
        print("FAILED: " + ", ".join(sorted(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
