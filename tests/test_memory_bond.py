#!/usr/bin/env python3
"""The bond's storage (memory.py): coach_bond.json beside coach_memory.json, so an older engine (which keeps only its
note kinds and keys) never loses it; "rapport" notes saved as facts flagged "rapport" (older engines keep them);
unknown note kinds and keys written back as they were; every part of a hand-edited or broken file checked (a bad
entry dropped, never a crash at start or in a coach call); zones remembered apart from the capped diary; "forget
everything" forgets the history too; character names in any script; a Character edited in Settings never takes
another character's lore.

Run: python3 tests/test_memory_bond.py   (prints OK)
"""
import json
import os
import sys
import tempfile

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
from glidedb import GlideDB  # noqa: E402
from coach_deps import CoachDeps  # noqa: E402
import coach  # noqa: E402
import memory as memory_mod  # noqa: E402


def check(cond, what):
    if not cond:
        raise SystemExit("FAIL: " + str(what))


def read(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


d = tempfile.mkdtemp()
path = os.path.join(d, "coach_memory.json")
with open(path, "w", encoding="utf-8") as fh:        # a file with a kind and a key from some other version
    json.dump({"notes": [{"id": 4, "kind": "wish", "text": "From the future."},
                         {"id": 2, "kind": "fact", "text": "Plays a dwarf.", "updated": 5}],
               "next_id": 5, "learning": True, "last_reflect": 0, "future_key": {"x": 1}}, fh)
m = memory_mod.Memory(path)
m.add("Laughs at gnome jokes; calls the coach Sarge.", "rapport")
m.bond_add(sessions=1, asides=2, replied=1, activity="flying")
m.diary_add("level_up", "Reached level 12 in Westfall")
m.character_save("Sarge", "A grizzled dwarf drill sergeant.", "Gruff.")
m.character_lore("Sarge", "Lost his beard to a gnome's rocket.")

main, side = read(path), read(os.path.join(d, "coach_bond.json"))
kinds = {n["text"]: (n["kind"], n.get("rapport")) for n in main["notes"]}
check(kinds["Laughs at gnome jokes; calls the coach Sarge."] == ("fact", True), "rapport saved as a flagged fact")
check(kinds["From the future."] == ("wish", None) and main["future_key"] == {"x": 1}, "unknown kinds and keys kept")
check(not {"bond", "diary", "characters"} & set(main), "the bond is not in coach_memory.json")
check(side["bond"]["asides"] == 2 and side["diary"][0]["text"].startswith("Reached level 12") and
      side["characters"]["sarge"]["lore"] == ["Lost his beard to a gnome's rocket."], side)
check(m.add("Another note.")[0]["id"] == 6, "ids continue past the unknown kind's (the rapport note took 5)")

# an older engine: keeps its kinds (the flagged fact among them) and its own keys only, then saves
old = read(path)
old_kinds = ("preference", "request", "goal", "feedback", "fact")
with open(path, "w", encoding="utf-8") as fh:
    json.dump({"notes": [n for n in old["notes"] if n["kind"] in old_kinds], "next_id": old["next_id"],
               "learning": True, "last_reflect": 0}, fh)
again = memory_mod.Memory(path)
check(any(n["kind"] == "rapport" and "gnome jokes" in n["text"] for n in again.list()), "rapport survives a downgrade")
check(again.bond["asides"] == 2 and again.character("sarge")["lore"], "and so does the bond (its own file)")

# broken files: every bad part dropped, never a crash, and a coach call still works
bad = os.path.join(d, "bad")
os.makedirs(bad)
with open(os.path.join(bad, "coach_memory.json"), "w", encoding="utf-8") as fh:
    json.dump({"notes": [{"kind": "fact"}, {"text": 5, "kind": "fact"}, "x", {"id": "z", "kind": "fact", "text": "t"},
                         {"id": 1, "kind": "fact", "text": "Fine."}], "next_id": "q", "last_reflect": "x"}, fh)
with open(os.path.join(bad, "coach_bond.json"), "w", encoding="utf-8") as fh:
    json.dump({"bond": {"hours": "x", "sessions": [1], "by_activity": {"flying": ["a", 1], "dead": [None, 1],
                                                                       "eating": [3, 1], "x": "y"}},
               "diary": [{"text": "no time"}, {"t": "x", "text": "bad time"}, {"t": 5, "text": 7},
                         {"t": True, "text": "bool"}, {"t": 9, "text": "Good one", "zone": "Elwynn Forest"}],
               "characters": {"a": {"brief": "no name"}, "b": {"name": "x", "brief": 5}, "c": "y",
                              "d": {"name": "Fizzle", "brief": "A gnome.", "lore": "not a list"}},
               "zones": ["Westfall", 3]}, fh)
b = memory_mod.Memory(os.path.join(bad, "coach_memory.json"))
check([n["text"] for n in b.list()] == ["Fine."], b.list())
check(b.bond["hours"] == 0.0 and b.bond["sessions"] == 0 and b.bond["by_activity"]["eating"] == [3, 1], b.bond)
check([x["text"] for x in b.diary] == ["Good one"] and b.zones == {"Westfall", "Elwynn Forest"}, (b.diary, b.zones))
check(list(b.characters) == ["fizzle"] and b.characters["fizzle"]["lore"] == [], b.characters)
check("Fizzle: A gnome." in b.characters_block() and b.diary_since(0), "the prompt parts build")
for broken in ({"characters": [1]}, {"diary": 5}, {"bond": 3}, [], "x"):
    with open(os.path.join(bad, "coach_bond.json"), "w", encoding="utf-8") as fh:
        json.dump(broken, fh)
    memory_mod.Memory(os.path.join(bad, "coach_memory.json")).characters_block()

# zones: remembered apart from the diary (which keeps the newest 80 moments)
z = memory_mod.Memory(None)
check(z.zone_seen("Duskwood") and not z.zone_seen("Duskwood"), "a zone is new once")
for i in range(100):
    z.diary_add("died", "Died %d" % i)
check(not z.zone_seen("Duskwood"), "still known after the diary moved on")

# forget everything: the history too
z.bond_add(sessions=1, asides=3)
z.character_save("Sarge", "A sergeant.")
z.remove("all")
check(not z.diary and not z.zones and not z.characters and z.bond["asides"] == 0, "forgotten")

# names in any script; short keys don't match strangers
u = memory_mod.Memory(None)
check(u.character_save("Тралл", "Вождь орды.") is not None and u.character("тралл")["name"] == "Тралл", "Cyrillic")
u.character_save("Sergeant Bluff", "A dwarf.")
check(u.character("se") is None and u.character("bluff")["name"] == "Sergeant Bluff", "no 2-letter fuzzy match")

# a Character edited in Settings (persona changes, persona_name doesn't) never takes Sarge's lore
mm = memory_mod.Memory(None)
c = coach.Coach({}, GlideDB("/x.db"), lambda msg: None, offline_ok=True, deps=CoachDeps(memory=mm))
c._directives("Aye!\nPERSONA: A grizzled dwarf drill sergeant.\nNAME: Sarge\nLORE: Lost his beard to a rocket.")
check(c.persona_name() == "Sarge" and mm.character("sarge")["lore"] == ["Lost his beard to a rocket."],
      "Sarge and his lore")
c.cfg["persona"] = "A sarcastic butler named Jeeves."              # Settings > Roleplay > Character
check(c.persona_name() != "Sarge" and c.main_name() == "", "the edited character has no saved identity")
check(not any("Lost his beard to a rocket." in block["text"] for block in c._system_blocks()),
      "the butler doesn't get Sarge's lore")
c._directives("Indeed.\nLORE: Served three kings.")
check(mm.character("sarge")["lore"] == ["Lost his beard to a rocket."] and
      mm.character("sarge")["brief"] == "A grizzled dwarf drill sergeant.", "Sarge untouched")
print("OK")
