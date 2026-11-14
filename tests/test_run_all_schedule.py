#!/usr/bin/env python3
"""tests/run_all.py's scheduling and fast loop: the timings it keeps per machine (tests/.timings.json: a time rises at
most a quarter per run and falls at once; tests that are gone are dropped; a bad file is ignored), the start order
(longest first, a test without a time first of all, the hand-kept _HEAVY list while fewer than half have one), which
tests keep normal priority (at least half as long as the longest; none with -j 1), --changed (a change to one engine
file runs only the tests that reach it, and test_types gets the engine files to check), and Ctrl+C on the new main
loop (exit 130 at once, nothing queued starts after it, no timings written).

The end-to-end parts run a copy of the runner over a scratch tree of tiny tests (a git repository for --changed), so
they take seconds and never touch this checkout's timings. The Ctrl+C part is POSIX only (it sends Ctrl+C to a
process group).

Run: python3 tests/test_run_all_schedule.py   (prints OK)
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_all  # noqa: E402

ENV = dict(os.environ, ANTHROPIC_API_KEY="", GEMINI_API_KEY="", GOOGLE_API_KEY="", OPENROUTER_API_KEY="",
           OPENAI_API_KEY="")
ENV.pop("GLIDE_TEST_TIMINGS", None)
ENV.pop("GLIDE_TYPES_FILES", None)


# ---- timings ---------------------------------------------------------------------------------------------------------
old = {"a.py": {"wall": 10.0, "cpu": 8.0}, "b.py": {"wall": 4.0}, "gone.py": {"wall": 3.0}}
ran = {"a.py": {"wall": 20.0, "cpu": 2.0}, "b.py": {"wall": 1.0}, "new.py": {"wall": 5.0, "cpu": 5.0}}
m = run_all.merge_timings(old, ran, {"a.py", "b.py", "c.py", "new.py"})
assert m["a.py"] == {"wall": 12.5, "cpu": 2.0}, m["a.py"]          # rises a quarter at most, falls at once
assert m["b.py"] == {"wall": 1.0}, m["b.py"]
assert m["new.py"] == {"wall": 5.0, "cpu": 5.0}, m["new.py"]       # a first time is taken as it is
assert "gone.py" not in m and "c.py" not in m, m                    # a test that's gone is dropped
assert run_all.merge_timings(old, {}, {"a.py"}) == {"a.py": old["a.py"]}

tmp = tempfile.mkdtemp(prefix="glide-run-all-")
try:
    path = os.path.join(tmp, "timings.json")
    assert run_all.load_timings(path) == {}                         # none yet
    run_all.save_timings(m, path)
    assert run_all.load_timings(path) == m
    assert not [f for f in os.listdir(tmp) if f.endswith(".tmp")]  # written whole, no leftover
    for bad in ("{", "[]", '{"tests": 3}', '{"tests": {"a.py": {"wall": "x"}, "b.py": {"wall": 0}}}'):
        with open(path, "w", encoding="utf-8") as f:
            f.write(bad)
        assert run_all.load_timings(path) == {}, bad
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# ---- order and priority ----------------------------------------------------------------------------------------------
def T(*names):
    return [os.path.join(run_all.HERE, *n.split("/")) for n in names]


tests = T("test_a.py", "test_b.py", "test_c.py", "test_d.py", "kb/test_e.py")
times = {"test_a.py": {"wall": 2.0}, "test_b.py": {"wall": 30.0}, "test_c.py": {"wall": 14.0},
         "kb/test_e.py": {"wall": 16.0}}
names = [run_all._name(t) for t in run_all.order(tests, times)]
assert names == ["test_d.py", "test_b.py", "kb/test_e.py", "test_c.py", "test_a.py"], names   # unknown, then longest
first = {run_all._name(t) for t in run_all.critical(tests, times, 4)}
assert first == {"test_b.py", "kb/test_e.py"}, first                # at least half the longest
assert run_all.critical(tests, times, 1) == set()                    # -j 1: everything at normal priority
few = {"test_b.py": {"wall": 30.0}, "test_c.py": {"wall": 14.0}}    # 2 of 5 timed: the hand-kept list
assert run_all.critical(tests, few, 4) == set()
heavy = T("test_x.py", "test_types.py", "test_navmesh.py", "test_golden.py", "test_rxp_explorer.py")
names = [run_all._name(t) for t in run_all.order(heavy, {})]
assert names == ["test_golden.py", "test_navmesh.py", "test_rxp_explorer.py", "test_types.py", "test_x.py"], names


# ---- arguments -------------------------------------------------------------------------------------------------------
def args(*a):
    o = run_all._parse_args(list(a))
    return o["changed"], o["words"], o["jobs"]


assert args("--changed") == ("origin/main", [], args()[2])
assert args("--changed", "HEAD~3", "kb") == ("HEAD~3", ["kb"], args()[2])
assert args("--changed", "origin/dev") == ("origin/dev", [], args()[2])
assert args("--changed=abc1234", "-j", "2") == ("abc1234", [], 2)
assert args("--changed", "main", "kb") == ("origin/main", ["main", "kb"], args()[2])   # a word, not a ref
assert args("-j3", "trash")[1:] == (["trash"], 3)


# ---- end to end, over a scratch tree ---------------------------------------------------------------------------------
def tree():
    root = tempfile.mkdtemp(prefix="glide-run-all-")
    t = os.path.join(root, "tests")
    os.makedirs(os.path.join(root, "companion"))
    os.makedirs(t)
    for f in ("run_all.py", "affected.py"):
        shutil.copy(os.path.join(HERE, f), t)
    return root, t


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


TEST = ("import os, sys, time\nROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
        "sys.path.insert(0, os.path.join(ROOT, 'companion'))\n%s\nprint('OK')\n")


def run(t, *a, timings=None, **kw):
    env = dict(ENV, GLIDE_TEST_TIMINGS=timings or os.path.join(t, ".timings.json"))
    return subprocess.run([sys.executable, os.path.join(t, "run_all.py"), *a], cwd=os.path.dirname(t), env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, **kw)


root, t = tree()
try:
    # a full run: its summary, and the times it keeps (passes and skips, not failures)
    write(os.path.join(t, "test_slow.py"), TEST % "time.sleep(0.6)")
    write(os.path.join(t, "test_fast.py"), TEST % "pass")
    write(os.path.join(t, "test_skip.py"), "print('SKIP: not here')\nraise SystemExit(77)\n")
    write(os.path.join(t, "test_bad.py"), "raise SystemExit(3)\n")
    r = run(t, "-j", "2")
    assert r.returncode == 1 and "4 tests: 2 ok, 1 skipped, 1 failed" in r.stdout, r.stdout
    got = run_all.load_timings(os.path.join(t, ".timings.json"))
    assert set(got) == {"test_slow.py", "test_fast.py", "test_skip.py"}, got
    assert got["test_slow.py"]["wall"] >= 0.5, got
    if os.name == "posix":
        assert "cpu" in got["test_slow.py"], got                     # measured where it can be
    # Scheduler input must be deterministic: process startup under load can make "fast" exceed half of "slow".
    run_all.save_timings({"test_slow.py": {"wall": 10.0}, "test_fast.py": {"wall": 1.0},
                          "test_skip.py": {"wall": 0.5}}, os.path.join(t, ".timings.json"))
    # The unknown test starts first, then the longest known test at normal priority.
    r = run(t, "--list", "-j", "2")
    rows = [line.split() for line in r.stdout.splitlines() if line.startswith("test_")]
    assert [x[0] for x in rows] == ["test_bad.py", "test_slow.py", "test_fast.py", "test_skip.py"], r.stdout
    pri = {x[0]: x[1] for x in rows}
    assert pri["test_slow.py"] == "normal" and pri["test_fast.py"] == "low" and pri["test_bad.py"] == "low", pri
    serial = [line.split()[0] for line in run(t, "--list", "-j", "1").stdout.splitlines()
              if line.startswith("test_")]   # -j 1: one at a time, in order
    assert serial == ["test_bad.py", "test_fast.py", "test_skip.py", "test_slow.py"], serial
finally:
    shutil.rmtree(root, ignore_errors=True)


# --changed on a fake change: a scratch git repository with two engine modules and a test for each
def git(root, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                    *a], cwd=root, check=True, capture_output=True)


root, t = tree()
try:
    write(os.path.join(root, "companion", "alpha.py"), "X = 1\n")
    write(os.path.join(root, "companion", "beta.py"), "Y = 2\n")
    write(os.path.join(root, "companion", "gamma.py"), "import alpha\nZ = alpha.X\n")
    write(os.path.join(t, "test_alpha.py"), TEST % "import alpha")
    write(os.path.join(t, "test_beta.py"), TEST % "import beta")
    seen = os.path.join(root, "types_files.txt")
    write(os.path.join(t, "test_types.py"), TEST % (   # stands in for test_types: what it was asked to check
        "open(os.path.join(ROOT, 'types_files.txt'), 'w').write(os.environ.get('GLIDE_TYPES_FILES', '<all>'))\n"
        "os.listdir(os.path.join(ROOT, 'companion'))"))
    write(os.path.join(root, ".gitignore"),
          "__pycache__/\ntests/.timings.json\ntests/.affected-cache.json\ntypes_files.txt\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    write(os.path.join(root, "companion", "alpha.py"), "X = 3\n")   # the change
    r = run(t, "--changed", "HEAD", "--list")
    listed = sorted(line.split()[0] for line in r.stdout.splitlines() if line.startswith("test_"))
    assert listed == ["test_alpha.py", "test_types.py"], r.stdout
    assert "fast loop: 2 of 3 tests for 1 files changed since HEAD" in r.stdout, r.stdout
    r = run(t, "--changed=HEAD", "-j", "2")
    assert r.returncode == 0 and "2 tests: 2 ok" in r.stdout, r.stdout
    with open(seen, encoding="utf-8") as f:
        assert sorted(f.read().split()) == ["companion/alpha.py", "companion/gamma.py"]   # alpha and what imports it
    got = run_all.load_timings(os.path.join(t, ".timings.json"))
    assert "test_alpha.py" in got and "test_types.py" not in got, got   # a partial type check's time is no guide
    # without --changed: every test, and test_types checks the whole engine (even with the variable set outside)
    os.remove(seen)
    r = subprocess.run([sys.executable, os.path.join(t, "run_all.py"), "-j", "2"], cwd=root, capture_output=True,
                       text=True, env=dict(ENV, GLIDE_TYPES_FILES="companion/beta.py",
                                           GLIDE_TEST_TIMINGS=os.path.join(t, ".timings.json")), timeout=120)
    assert "3 tests: 3 ok" in r.stdout, r.stdout
    with open(seen, encoding="utf-8") as f:
        assert f.read() == "<all>"
    # code no test reaches (a new tool): the rules can't tell, so everything
    os.makedirs(os.path.join(root, "tools"))
    write(os.path.join(root, "tools", "delta.py"), "W = 4\n")
    r = run(t, "--changed", "HEAD", "--list")
    assert "fast loop: 3 of 3 tests" in r.stdout, r.stdout
finally:
    shutil.rmtree(root, ignore_errors=True)


# Ctrl+C on the new main loop (the long test at normal priority, the rest queued below it): exit 130 within seconds,
# the queued tests never start, and no timings are written for the cut-short run
if os.name == "posix":
    root, t = tree()
    try:
        marks = os.path.join(root, "started")
        os.makedirs(marks)
        start = "open(os.path.join(ROOT, 'started', os.path.basename(__file__)), 'w').close()\n"
        write(os.path.join(t, "test_long.py"), TEST % (start + "time.sleep(30)"))
        for i in range(12):
            write(os.path.join(t, "test_q%02d.py" % i), TEST % (start + "time.sleep(2)"))
        timings = os.path.join(root, "timings.json")
        run_all.save_timings({"test_long.py": {"wall": 30.0}, **{"test_q%02d.py" % i: {"wall": 2.0}
                                                                 for i in range(12)}}, timings)
        before = open(timings, encoding="utf-8").read()
        p = subprocess.Popen([sys.executable, os.path.join(t, "run_all.py"), "-j", "2"], cwd=root,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
                             env=dict(ENV, GLIDE_TEST_TIMINGS=timings))
        deadline = time.time() + 20
        while len(os.listdir(marks)) < 4 and time.time() < deadline:   # long + the 3 that fit beside it
            time.sleep(0.1)
        assert len(os.listdir(marks)) == 4, sorted(os.listdir(marks))  # 2 * -j at once
        t0 = time.time()
        os.killpg(p.pid, signal.SIGINT)
        try:
            out, _ = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            raise AssertionError("run_all.py still running 30 s after Ctrl+C") from None
        took = time.time() - t0
        assert p.returncode == 130 and "interrupted:" in out and "Traceback" not in out, (p.returncode, out[-800:])
        assert took < 5, "took %.1f s to stop" % took
        time.sleep(2.5)                                                   # nothing queued starts afterwards
        assert len(os.listdir(marks)) == 4, sorted(os.listdir(marks))
        assert open(timings, encoding="utf-8").read() == before
    finally:
        shutil.rmtree(root, ignore_errors=True)

print("OK")
