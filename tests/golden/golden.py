"""Golden masters for the refactor: what the code does today, recorded once, compared after every change.

    python tests/golden/golden.py record     # run every probe, save tests/golden/snapshots/<probe>.json
    python tests/golden/golden.py check      # run every probe, compare with the snapshots (exit 1 on a difference)
    python tests/golden/golden.py check engine knowledge   # only these probes
    python tests/golden/golden.py check --portable          # only the portable tier (record --portable too)

Each probe is a script in this folder (probe_<name>.py OUT.json) run in its own process, so one probe's imports and
monkeypatched clock never leak into another. Each declares its tier (TIER = "..." at its top): "portable" probes
need only the repository (and tools like node or Edge), so their snapshots hold on any machine; "machine" probes read
this machine's data (data/kb.db, glide.db) and pin behaviour for a refactor on this machine only.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "snapshots")
PROBES = sorted(f[len("probe_"):-3] for f in os.listdir(HERE) if f.startswith("probe_") and f.endswith(".py"))


def tier(name):
    """A probe's tier from its TIER line (read, not imported: probes import what they test)."""
    with open(os.path.join(HERE, "probe_%s.py" % name), encoding="utf-8") as fh:
        m = re.search(r'^TIER = "(\w+)"', fh.read(), re.M)
    return m.group(1) if m else "machine"


PORTABLE = [p for p in PROBES if tier(p) == "portable"]
ENV = dict(os.environ, ANTHROPIC_API_KEY="", OPENAI_API_KEY="", GEMINI_API_KEY="", GOOGLE_API_KEY="", OPENROUTER_API_KEY="",
           OPENCODE_API_KEY="", PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8")


TIMES = {}                              # probe -> seconds its last run took (printed by check: the slowest sets the pace)


def run_probe(name):
    """-> the probe's output (parsed JSON), or raises with its stderr."""
    t0 = time.perf_counter()
    out = os.path.join(tempfile.mkdtemp(), name + ".json")
    r = subprocess.run([sys.executable, os.path.join(HERE, "probe_%s.py" % name), out], env=ENV,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    TIMES[name] = time.perf_counter() - t0
    if r.returncode or not os.path.exists(out):
        raise RuntimeError("probe %s failed:\n%s" % (name, (r.stderr or r.stdout)[-3000:]))
    return json.load(open(out, encoding="utf-8"))


def first_difference(a, b, path="$"):
    """The first place two JSON values differ, as 'path: expected != actual' (short)."""
    if type(a) is not type(b):
        return "%s: type %s != %s" % (path, type(a).__name__, type(b).__name__)
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                return "%s: key %r %s" % (path, k, "missing now" if k in a else "new")
            d = first_difference(a[k], b[k], "%s.%s" % (path, k))
            if d:
                return d
        return None
    if isinstance(a, list):
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_difference(x, y, "%s[%d]" % (path, i))
            if d:
                return d
        return None if len(a) == len(b) else "%s: length %d != %d" % (path, len(a), len(b))
    return None if a == b else "%s: %s != %s" % (path, json.dumps(a)[:200], json.dumps(b)[:200])


def main(argv):
    mode = argv[0] if argv else "check"
    names = [a for a in argv[1:] if a != "--portable"] or (PORTABLE if "--portable" in argv else PROBES)
    os.makedirs(SNAP, exist_ok=True)
    bad = 0
    if mode == "record":
        # one at a time, in order, each snapshot written before the next probe runs: the engine probe replays the
        # addon snapshot, so recording both must not read the old one
        results = None
    else:
        with ThreadPoolExecutor(max_workers=min(len(names), 6) or 1) as pool:
            results = dict(zip(names, pool.map(run_probe, names)))
    for name in names:
        got = run_probe(name) if results is None else results[name]
        path = os.path.join(SNAP, name + ".json")
        if mode == "record":
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(got, fh, indent=1, sort_keys=True, ensure_ascii=False)
                fh.write("\n")
            print("recorded", name, "(%d records)" % len(got))
            continue
        want = json.load(open(path, encoding="utf-8"))
        got = json.loads(json.dumps(got, sort_keys=True))
        diff = first_difference(want, got)
        print("%-10s %5.1fs  %s" % (name, TIMES.get(name, 0.0), "same" if not diff else "CHANGED  " + diff))
        bad += bool(diff)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
