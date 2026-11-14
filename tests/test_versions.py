#!/usr/bin/env python3
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "companion"))
import versions
exp = versions.expected()
assert versions.status(None, 3)["ok"] is None                              # no data yet
assert versions.status(exp, 50, exp, exp)["ok"] is True
r = versions.status(None, 50, exp, exp)                                    # old addon still loaded
assert r["ok"] is False and "restart WoW" in r["fix"], r
r = versions.status("0.4.0", 50, exp, "0.4.0")                             # files never copied
assert r["ok"] is False and "AddOns" in r["fix"], r
d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, "Interface", "AddOns", "Glide"))
open(os.path.join(d, "Interface", "AddOns", "Glide", "Glide.toc"), "w").write("## Interface: 16001\n## Version: 0.5.0\n")
assert versions.installed(d) == "0.5.0"
# run by the desktop app: the fix points at the app's Addon page, never at the project's folders
r = versions.status("0.4.0", 50, exp, "0.4.0", app=True)
assert r["kind"] == "installed" and "Addon page" in r["fix"] and "project" not in r["fix"] + r["problem"], r
r = versions.status(None, 50, exp, exp, app=True)
assert r["kind"] == "running" and "restart WoW" in r["fix"], r
assert versions.status(exp, 50, exp, exp, app=True)["kind"] is None
# rules + offline coach line
from rules import Rules
from state import GameState
from coach import Coach
from glidedb import GlideDB
rl = Rules({}); rl.addon_status = versions.status(None, 50, exp, exp)
tr = [t for t in rl.check(GameState(), []) if t.kind == "addon_outdated"]
assert tr, tr
os.environ.pop("ANTHROPIC_API_KEY", None)
c = Coach({"enabled": False}, GlideDB("/x.db"), lambda m: None)
print(c.offline(tr, {"player": {}, "rxp_steps": [], "quests": {}}, None))
print("OK")
