#!/usr/bin/env python3
"""Settings as the engine reads them (companion/settings.py over settings_manifest.json): a hand edit out of range
reads as the nearest end of it, so the coach does what the dashboard shows (lookahead_steps 500 used to send 500
steps while the dashboard said 80); a value of the wrong kind reads as the default; load_config clamps what
config.json sets. And config.json writes: the dashboard's save (tuning.save) used to fail on a file Notepad saved
with a BOM; it reads it now, and leaves a file it can't read alone. jsonfile.write_atomic's temporary file has a
name of its own (two writers used to share "<file>.tmp")."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import app as glide_app  # noqa: E402
import jsonfile  # noqa: E402
import settings  # noqa: E402
import tuning  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from rules import Rules  # noqa: E402

S = settings.Settings
# ranges
assert S("llm", {"lookahead_steps": 500}).get("lookahead_steps") == 80
assert S("llm", {"lookahead_steps": -3}).get("lookahead_steps") == 0
assert S("llm", {"lookahead_steps": 12.6}).get("lookahead_steps") == 13
assert S("llm", {"lookahead_steps": "25"}).get("lookahead_steps") == 25
assert S("llm", {"briefing_delay_s": 2.5}).get("briefing_delay_s") == 2.5        # numbers keep their type
assert S("llm", {"timeout": 1000}).get("timeout") == 120
assert S("rules.buffs", {"warn_before": 9999}).get("warn_before") == 120
# the wrong kind, or unset: the default
assert S("llm", {"lookahead_steps": "lots"}).get("lookahead_steps") == 40
assert S("llm", {"lookahead_steps": None}).get("lookahead_steps") == 40
assert S("llm", {"lookahead_steps": float("nan")}).get("lookahead_steps") == 40
assert S("rules.buffs", {"ignore": "Thorns"}).get("ignore") == []
assert S("llm", {}).get("briefing_delay_s") == 6
assert S("voice", {}).get("markup_mode") == "ai"
assert S("voice", {}).get("markup_profile") == "standard"
assert S("voice", {"markup_voices": []}).get("markup_voices") == {}
permissions = {"token:one": ["rate"], "token:two": ["pitch", "pauses"]}
assert S("voice", {"markup_voices": permissions}).get("markup_voices") == permissions
fresh = settings.defaults()
fresh["voice"]["markup_voices"]["token:one"] = ["rate"]
assert settings.defaults()["voice"]["markup_voices"] == {}
fresh_permissions = S("voice", {}).get("markup_voices")
fresh_permissions["token:one"] = ["rate"]
assert S("voice", {}).get("markup_voices") == {}
# in range: the value as it is
assert S("llm", {"conversation_turns": 3}).get("conversation_turns") == 3
assert S("ptt", {"min_seconds": 0.2}).get("min_seconds") == 0.2
# the dashboard shows what the coach reads
cfg = {"lookahead_steps": 500, "voice_lookahead_steps": 99}
snap = tuning.snapshot(type("C", (), {"cfg": cfg})())
assert snap["settings"]["lookahead_steps"]["value"] == 80 == LLMClient(cfg).opt.get("lookahead_steps")
assert snap["settings"]["voice_lookahead_steps"]["value"] == 30
# a section read live: a dashboard change shows at once
c = LLMClient({})
c.cfg["lookahead_steps"] = 12
assert c.opt.get("lookahead_steps") == 12
# rules read their (clamped) settings too
assert Rules({"stuck_minutes": 0}).opt.get("stuck_minutes") == 1

tmp = tempfile.mkdtemp(prefix="glide-settings-")
try:
    # load_config clamps hand edits
    glide_app.ROOT = tmp
    p = os.path.join(tmp, "config.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"companion": {"llm": {"lookahead_steps": 500, "fast_model": "x-model"},
                                 "capture": {"fps": 0}, "rules": {"buffs": {"min_duration": -5}}}}, fh)
    with contextlib.redirect_stdout(io.StringIO()):
        loaded = glide_app.load_config()
    assert loaded["llm"]["lookahead_steps"] == 80 and loaded["llm"]["fast_model"] == "x-model"
    assert loaded["capture"]["fps"] == 1 and loaded["rules"]["buffs"]["min_duration"] == 0
    # (a section merges one level deep: buffs is the file's, its missing values read as their defaults)
    assert S("rules.buffs", loaded["rules"]["buffs"]).get("warn_before") == 15

    # tuning.save: a BOM is fine, everything else in the file is kept, the file ends with a newline
    with open(p, "w", encoding="utf-8-sig") as fh:
        json.dump({"wow_dir": "C:/WoW", "companion": {"llm": {"fast_model": "a"}}}, fh)
    tuning.save(p, {"lookahead_steps": 20})
    text = open(p, encoding="utf-8").read()
    assert not text.startswith("\ufeff") and text.endswith("}\n"), text[:20]
    data = json.loads(text)
    assert data == {"wow_dir": "C:/WoW", "companion": {"llm": {"fast_model": "a", "lookahead_steps": 20}}}, data
    # ... and a file it can't read raises and stays as it is
    bad = '{"wow_dir": "C:/WoW",, }'
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(bad)
    try:
        tuning.save(p, {"lookahead_steps": 20})
        raise SystemExit("saved over an unreadable config.json")
    except ValueError:
        pass
    assert open(p, encoding="utf-8").read() == bad
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("[1, 2]")
    try:
        tuning.save(p, {"lookahead_steps": 20})
        raise SystemExit("saved over a config.json that isn't an object")
    except ValueError:
        pass
    # no file yet: made
    os.remove(p)
    tuning.save(p, {"web_search": False})
    assert jsonfile.read(p) == {"companion": {"llm": {"web_search": False}}}

    # jsonfile.read takes a BOM
    with open(p, "w", encoding="utf-8-sig") as fh:
        fh.write('{"wow_dir": "D:/Games"}')
    assert jsonfile.read(p) == {"wow_dir": "D:/Games"}
    import health
    assert health.wow_dir(tmp) == "D:/Games"

    # write_atomic: many writers at once, every result whole, no temporary files left
    target = os.path.join(tmp, "sub", "many.json")
    errors = []

    def writer(n):
        for i in range(30):
            try:
                jsonfile.write_atomic(target, {"n": n, "i": i, "pad": "x" * 2000})
            except PermissionError:
                pass                     # Windows: a rename can lose to another writer's; never a torn file
            except Exception as e:      # noqa: BLE001
                errors.append(e)
    ts = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors
    assert jsonfile.read(target)["pad"] == "x" * 2000
    assert os.listdir(os.path.dirname(target)) == ["many.json"], os.listdir(os.path.dirname(target))
    # a failed dump leaves neither a temporary file nor a changed target
    try:
        jsonfile.write_atomic(target, {"bad": object()})
        raise SystemExit("dumped an object")
    except TypeError:
        pass
    assert os.listdir(os.path.dirname(target)) == ["many.json"] and "pad" in jsonfile.read(target)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print("ok")
