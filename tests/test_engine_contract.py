#!/usr/bin/env python3
"""The engine's event stream is typed once (desktop/src/shared/engine.ts) for both UIs: the desktop renderer reads it
through those types, and the dashboard (companion/static/dashboard.js, // @ts-check) is checked against them by
desktop/tsconfig.dashboard.json in npm run build. Here the types are checked against the engine: every top-level key
of a real state (tests/golden/fixtures/dashboard_init.json) is declared and nothing declared is gone, and public
fixture message fields are CoachMessage fields. The localization consumer suite additionally drives App delivery
and checks published keys, history and speech; internal coach delivery envelopes are not public messages."""
import json
import os
import re
import sys
from types import SimpleNamespace

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
assert {"facts_unknown", "web_sources", "spoken"} <= fields, fields
assert sent <= fields, "message fields not in CoachMessage: %s" % sorted(sent - fields)

# Exercise the emitted contract rather than matching a particular dictionary expression.
sys.path.insert(0, os.path.join(ROOT, "companion"))
from voice import HubVoice  # noqa: E402

events = []
speaker = HubVoice({
    "enabled": True,
    "markup_destination": {"provider": "windows", "identity": "test-token", "features": ["rate"]},
}, SimpleNamespace(publish=lambda event, payload: events.append((event, payload))), announce=False)
speaker.say("Literal <tag> & words.")
speaker.say("Slow words.", tone="calm", markup={
    "text": "Slow words.", "spans": [{"start": 0, "end": 4, "rate": "slow"}],
})
speak_fields = declared("SpeakEvent")
required = {key for key, optional in speak_fields.items() if not optional}
assert events[0][1]["text"] == "Literal <tag> & words."
assert events[1][1]["markup"]["spans"][0]["rate"] == "slow"
for event, payload in events:
    assert event == "speak"
    assert required <= set(payload) <= set(speak_fields), payload
assert set().union(*(set(payload) for _, payload in events)) == set(speak_fields)

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
