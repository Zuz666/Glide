#!/usr/bin/env python3
"""The app's Settings page says what the engine does. Every engine setting's default, range and "applies live" flag
is written once, in companion/settings_manifest.json, and everything else is checked against it here:
the engine's defaults (app.DEFAULTS, and what a section without the value reads), the dashboard's live settings
(tuning.py), the Settings page (settings-schema.ts takes def/min/max/restart from the generated
settings-manifest.ts; page.ts's live list comes from it).
It used to drift: the briefing delay showed 2.5 s while the coach waited 6, and video_tips said "Saved." but waited
for a restart."""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import app as glide_app  # noqa: E402
import settings  # noqa: E402
import tuning  # noqa: E402

read = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
schema = read("desktop", "src", "renderer", "settings-schema.ts")
page = read("desktop", "src", "renderer", "settings", "page.ts")
M = settings.ENTRIES

# the engine's defaults are the manifest's
assert glide_app.DEFAULTS == settings.defaults()
for key, e in M.items():
    section, name = key.rsplit(".", 1)
    assert settings.Settings(section, {}).get(name) == e["default"], key
    assert e["kind"] in ("int", "number", "bool", "text", "list"), key
    if "min" in e or "max" in e:
        assert e["kind"] in ("int", "number") and e["min"] <= e["default"] <= e["max"], key
    assert isinstance(e["default"], {"int": int, "number": (int, float), "bool": bool, "text": str,
                                     "list": list}[e["kind"]]), key
assert "rxp" not in glide_app.DEFAULTS            # the knowledge layer tells "not set" from a value

# the dashboard's live settings are the manifest's live ones, with its ranges
live = set(settings.live_keys())
assert live == set(tuning.SETTINGS) | set(tuning.TOGGLES) | set(tuning.MODEL_KEYS) | tuning.TEXTS, live
assert tuning.TEXTS == {"persona", "persona_name", "persona_voice"}, tuning.TEXTS
for k, (d, lo, hi) in tuning.SETTINGS.items():
    e = M["llm." + k]
    assert (d, lo, hi) == (e["default"], e["min"], e["max"]), k

# the Settings page: every engine field is in the manifest and carries no default/range/restart of its own
fields = re.findall(r'\{ key: (L \+ "\w+"|"[\w:.]+")(.*?)(?=\n\s*\{ key: |\n\s*\] \})', schema, re.S)
assert len(fields) > 60, len(fields)
engine_keys = set()
for raw, body in fields:
    key = "llm." + raw[len('L + "'):-1] if raw.startswith("L +") else raw.strip('"')
    if key.startswith("companion."):
        key = key[len("companion."):]
    if key.startswith("app:") or key == "wow_dir":
        continue
    assert key in M, "%s is on the Settings page but not in settings_manifest.json" % key
    assert not re.search(r"\b(def|min|max|restart):", body), "%s sets its own def/min/max/restart" % key
    engine_keys.add(key)
assert "llm.briefing_delay_s" in engine_keys and "rules.buffs.ignore" in engine_keys
assert "companion.grid." not in schema, "the strip grid is back in Settings"
assert 'from "./settings-manifest.js"' in schema and "f.def = m.default" in schema
# page.ts sends the manifest's live settings to the running coach; no list of its own
assert 'from "../settings-manifest.js"' in page and "e.live" in page
assert not re.search(r'new Set\(\["voice_lookahead_steps"', page)

# the generated settings-manifest.ts matches the JSON (npm run build regenerates it)
node = shutil.which("node")
if node:
    r = subprocess.run([node, os.path.join(ROOT, "desktop", "scripts", "gen-settings.js"), "--check"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr

print("ok")
