#!/usr/bin/env python3
"""The engine's event stream is typed once (desktop/src/shared/engine.ts) for both UIs: the desktop renderer reads it
through those types, and the dashboard (companion/static/dashboard.js, // @ts-check) is checked against them by
desktop/tsconfig.dashboard.json in npm run build. Here the types are checked against the engine: every top-level key
of a real state (tests/golden/fixtures/dashboard_init.json) is declared and nothing declared is gone, and every
message field the engine sets (the fixture's, and the ones coach.py adds) is a CoachMessage field."""
import json
import os
import re

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
read = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
engine_ts = read("desktop", "src", "shared", "engine.ts")


def declared(name):
    """The keys of `export interface <name> {`, one member per line, up to the closing brace on its own line."""
    body = re.search(r"^export interface %s \{\n(.*?)^\}" % name, engine_ts, re.S | re.M)
    assert body, "no interface %s in engine.ts" % name
    return {m.group(1): bool(m.group(2)) for m in re.finditer(r"^  (\w+)(\??):", body.group(1), re.M)}


fixture = json.loads(read("tests", "golden", "fixtures", "dashboard_init.json"))
state, optional = declared("EngineState"), {k for k, opt in declared("EngineState").items() if opt}
got = set(fixture["state"])
assert len(state) >= 80, len(state)
assert got - set(state) == set(), "state keys the engine sends but engine.ts doesn't declare: %s" % sorted(got - set(state))
assert set(state) - got <= optional, "declared but not in the state (mark them ? if the engine may omit them): %s" \
    % sorted(set(state) - got - optional)

fields = set(declared("CoachMessage"))
sent = {k for m in fixture["messages"] for k in m}
sent |= set(re.findall(r'msg\["(\w+)"\]\s*=', read("companion", "coach.py")))
sent |= set(re.findall(r'msg\["(\w+)"\], msg\["(\w+)"\]\s*=', read("companion", "coach.py"))[0])
assert {"facts_unknown", "web_sources", "spoken"} <= sent, sent
assert sent <= fields, "message fields not in CoachMessage: %s" % sorted(sent - fields)

speak = re.search(r'publish\("speak", \{(.*?)\}\)', read("companion", "voice.py"), re.S).group(1)
assert set(re.findall(r'"(\w+)":', speak)) == set(declared("SpeakEvent")), speak

# the dashboard stays checked: @ts-check in the page, and the build runs its check
assert "// @ts-check" in read("companion", "static", "dashboard.js")
assert re.search(r"\btsc -b [^&]*\btsconfig\.dashboard\.json\b", json.loads(read("desktop", "package.json"))["scripts"]["build"])
# desktop/src/shared/*.ts is compiled to dist/shared/*.js by both CJS (main) and ESM (renderer) builds;
# runtime exports in .ts files there break main at startup when renderer's ESM build overwrites them (use .mts or src/main/).
shared_dir = os.path.join(ROOT, "desktop", "src", "shared")
for fname in sorted(os.listdir(shared_dir)):
    if fname.endswith(".ts") and not fname.endswith(".d.ts"):
        src = read("desktop", "src", "shared", fname)
        bad = re.findall(r"^\s*export\s+(?:const|let|var|async\s+function|function|class|enum|default)\b", src, re.M)
        assert not bad, "runtime export in type-only desktop/src/shared/%s: %r" % (fname, bad)
print("ok")
