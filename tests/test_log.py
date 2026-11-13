#!/usr/bin/env python3
"""The engine's console lines (companion/log.py): log(tag, msg) keeps the "[tag] msg" text the app shows and keeps
on disk, and log_once(site, e) puts a swallowed error on the record once per site (it used to vanish), with where
it was raised, without raising itself. The modules' status lines and swallowed errors go through it, and no broad
except falls back to a default silently (a short list of expected-quiet sites aside)."""
import ast
import contextlib
import io
import os
import re
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import log  # noqa: E402


def captured(fn):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn()
    return out.getvalue()


def boom(msg):
    raise ValueError(msg)


def swallow(site, msg):
    try:
        boom(msg)
    except Exception as e:  # noqa: BLE001
        log.log_once(site, e)


# the visible text is exactly what the old prints gave
assert captured(lambda: log.log("capture", "WoW window not found / minimised")) == \
    "[capture] WoW window not found / minimised\n"
assert captured(lambda: log.line("GLIDE_READY port=8800 app=1.2.3")) == "GLIDE_READY port=8800 app=1.2.3\n"

# once per site: the first error, with its type, text and where it was raised; repeats (even other errors) are quiet
out = captured(lambda: [swallow("test.save", "disk full %d" % i) for i in range(5)])
lines = out.splitlines()
assert len(lines) == 1, out
assert re.fullmatch(r"\[test\.save\] ignored error: ValueError: disk full 0 \(test_log\.py:\d+\)", lines[0]), lines[0]
# another site logs its own first error
out = captured(lambda: (swallow("test.load", "bad json"), swallow("test.save", "again"), swallow("test.load", "x")))
assert len(out.splitlines()) == 1 and out.startswith("[test.load] ignored error: ValueError: bad json "), out

# an exception never raised (no traceback) still logs, without a place
out = captured(lambda: log.log_once("test.bare", OSError("no tb")))
assert out == "[test.bare] ignored error: OSError: no tb\n", out

# a runaway caller can't grow the set without bound
before = len(log._seen)
captured(lambda: [log.log_once("test.many.%d" % i, ValueError("x")) for i in range(log.MAX_SITES * 2)])
assert len(log._seen) == log.MAX_SITES and before < log.MAX_SITES

# the engine modules no longer print their status lines themselves (calibration and strip_spec are command-line
# tools whose prints are their output), and no broad except in them passes silently any more
COMP = os.path.join(ROOT, "companion")
for name in sorted(os.listdir(COMP)):
    if not name.endswith(".py") or name in ("log.py", "calibration.py", "strip_spec.py"):
        continue
    src = open(os.path.join(COMP, name), encoding="utf-8").read()
    assert not re.search(r"^\s*print\(", src, re.M), "%s prints directly: use log.log / log.line" % name
    silent = re.search(r"except (Exception|BaseException)?\s*:[^\n]*\n\s*pass\b", src)
    assert not silent, "%s swallows an error silently: log_once(site, e)" % name

# nor does one fall back to a default silently: a broad except either re-raises or uses its error (logs it, or
# reports it in what it returns), except at these sites, where the error is the expected, normal case.
# (file, function, the handler's first statement)
QUIET = {
    ("app.py", "version_info", "kb_schema = None"),       # the knowledge package isn't installed
    ("app.py", "version_info", "nav = None"),             # nor wowfiles
    ("capture.py", None, "try:"),                         # Windows before 8.1: the older DPI call, which logs
    ("dashboard.py", "handle", "q = ''"),                 # POST /ask with a bad body asks nothing
    ("dashboard.py", "handle", "d = {}"),                 # POST /announce with a bad body says nothing
}


def broad_fallbacks(name, src):
    """(file, function, first statement) of each broad except that neither raises nor touches its error."""
    out = []

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ExceptHandler) and (child.type is None or (
                    isinstance(child.type, ast.Name) and child.type.id in ("Exception", "BaseException"))):
                body = [n for stmt in child.body for n in ast.walk(stmt)]
                raises = any(isinstance(n, ast.Raise) for n in body)
                used = child.name and any(isinstance(n, ast.Name) and n.id == child.name for n in body)
                if not raises and not used:
                    out.append((name, fn, ast.unparse(child.body[0]).splitlines()[0]))
            visit(child, child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn)
    visit(ast.parse(src), None)
    return out


found = set()
# Speech workers report closed diagnostic events rather than raw exception objects.
# Their failure visibility and privacy are checked at the worker/supervisor seams
# in test_edge_tts_worker.py and test_edge_tts.py, not by this engine AST ratchet.
for name in sorted(os.listdir(COMP)):
    if name.endswith(".py") and name not in ("edge_tts_pool.py", "edge_tts_worker.py"):
        for site in broad_fallbacks(name, open(os.path.join(COMP, name), encoding="utf-8").read()):
            assert site in QUIET, "%s: a broad except falls back silently (log_once(site, e), or add to QUIET): %r" \
                % (name, site)
            found.add(site)
assert found == QUIET, "QUIET lists sites that no longer exist: %r" % (QUIET - found)
# the check sees what it should: a silent fallback, but not a logged one, a re-raise or an error put in the reply
probe = ("def f():\n    try:\n        g()\n    except Exception:\n        return None\n"
         "def h():\n    try:\n        g()\n    except Exception as e:\n        log_once('x', e)\n        return []\n"
         "def i():\n    try:\n        g()\n    except Exception:\n        raise KeyError from None\n"
         "def j():\n    try:\n        g()\n    except Exception as e:\n        return {'error': str(e)}\n")
assert broad_fallbacks("probe.py", probe) == [("probe.py", "f", "return None")], broad_fallbacks("probe.py", probe)
print("ok")
