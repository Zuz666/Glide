#!/usr/bin/env python3
"""The coach's long-term memory (companion/memory.py): parsing the player's words, notes on disk (outside the engine
home, with daily backups), the prompt block, the coach's local commands and memory tools, feedback marks, and a
reflection pass against a mocked model."""
import json
import os
import sys
import tempfile

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
sys.path.append(ROOT)
tmp = tempfile.mkdtemp()
os.environ["GLIDE_MEMORY_DIR"] = os.path.join(tmp, "mem")
import memory  # noqa: E402

# ---- where it lives -------------------------------------------------------------------------------------------
assert memory.memory_dir() == os.path.join(tmp, "mem")
assert memory.default_path().endswith(os.path.join("mem", "coach_memory.json"))

# ---- the player's words ------------------------------------------------------------------------------------------
P = memory.parse_command
assert P("remember that I prefer short callouts") == ("add", "preference", "I prefer short callouts")
assert P("Remember: I'm aiming for 60 by Sunday")[1] == "goal"
assert P("from now on always tell me when my hearth is ready") == ("add", "request", "Always tell me when my hearth is ready")
assert P("from now on don't mention gear")[1] == "preference"
assert P("forget that I play a dwarf") == ("forget", "I play a dwarf")
assert P("what do you remember about me") == ("list",)
assert P("remember to repair") is None, "a reminder, not a memory"
assert P("forget the repair reminder") is None
assert P("how far to Goldshire?") is None
assert memory.guess_kind("you were wrong about the elite patrol") == "feedback"
assert memory.guess_kind("I play evenings, 2 hours") == "fact"
assert memory.guess_kind("I hate long briefings") == "preference"

# ---- notes on disk ---------------------------------------------------------------------------------------------
m = memory.Memory(memory.default_path())
n, created = m.add("Prefers callouts under 10 words.", "preference")
assert created and n["id"] == 1
n2, created2 = m.add("prefers callouts under 10 words", "preference")
assert not created2 and n2["id"] == 1, "a near-duplicate refreshes the note instead of adding one"
m.add("Aiming for level 60 by Sunday.", "goal")
m.add("Plays a dwarf warrior.")
assert {x["kind"] for x in m.list()} == {"preference", "goal", "fact"}
assert os.path.exists(memory.default_path())
backups = [f for f in os.listdir(memory.memory_dir()) if f.startswith("coach_memory.") and f[13:21].isdigit()]
assert len(backups) == 1, backups
again = memory.Memory(memory.default_path())
assert [x["text"] for x in again.list()] == [x["text"] for x in m.list()], "survives a restart"
assert n2["text"] == "prefers callouts under 10 words", "the newest wording wins"
assert [x["text"] for x in m.remove("dwarf")] == ["Plays a dwarf warrior."]
m.update(2, pinned=True)
assert m.list()[1]["pinned"] is True
m.set_learning(False)
assert memory.Memory(memory.default_path()).learning is False
m.set_learning(True)
assert memory.Memory(None).block() == "" and memory.Memory(None).snapshot()["persistent"] is False

# ---- the coach ---------------------------------------------------------------------------------------------------
for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"):
    os.environ.pop(k, None)
from coach import Coach, CoachDeps  # noqa: E402
from glidedb import GlideDB  # noqa: E402

msgs = []
c = Coach({"fast_model": "claude-haiku-4-5", "deep_model": "claude-sonnet-5-5"}, GlideDB("/x.db"), msgs.append,
          offline_ok=True, deps=CoachDeps(memory=memory.Memory(os.path.join(tmp, "coach", "coach_memory.json"))))
assert c._memory_command("remember that I hate long briefings") == "Got it, I'll remember that."
assert any(x["text"].startswith("Remembered:") for x in msgs)
assert "hate long briefings" in c._memory_command("what do you remember about me")
assert c._memory_command("forget that I hate long briefings").startswith("Forgotten")
assert c._local_tool("memory_list", {})["notes"] == [], "the forgotten memory is no longer listed"
# the memory tools the AI uses
names = [t["name"] for t in c._tools()]
assert {"memory_remember", "memory_forget", "memory_list"} <= set(names)
r = c._local_tool("memory_remember", {"text": "Wants pace reviews only every 30 minutes.", "kind": "preference"})
assert r["remembered"]["id"] and c._local_tool("memory_list", {})["notes"][0]["kind"] == "preference"
assert c._local_tool("memory_forget", {"which": str(r["remembered"]["id"])})["forgot"]
# a feedback mark with a note becomes a correction
c.note_mark("wrong turn-in NPC", "Turn in Supplies to Tannok, 20 yards east.")
fb = [x for x in c.deps.memory.list() if x["kind"] == "feedback"]
assert fb and "wrong turn-in NPC" in fb[0]["text"] and "Tannok" in fb[0]["text"]

# ---- reflection against a mocked model ---------------------------------------------------------------------------
c.convo.append({"t": c._now(), "who": "player", "kind": "typed", "text": "shorter please, I just need the direction"})
c.convo.append({"t": c._now(), "who": "coach", "kind": "answer", "text": "80 yards northeast."})
seen = {}


def fake_call(model, user, max_tokens, deep=False, tools=None, **kw):
    seen["prompt"], seen["schema"] = user, kw.get("schema")
    keep = c.deps.memory.list()[0]["id"]
    return json.dumps({"add": [{"kind": "preference", "text": "Wants directions only, no explanations."}],
                       "update": [{"id": keep, "text": "Corrected: wrong turn-in NPC for Supplies."}],
                       "remove": []}), {}


c.llm.call_once = fake_call
done = c.reflect()
assert "REFLECTION" in seen["prompt"] and "shorter please" in seen["prompt"] and "wrong turn-in NPC" in seen["prompt"]
assert seen["schema"] is c.reflector.REFLECT_SCHEMA and seen["schema"]["required"] == [
    "add", "update", "remove", "chapter", "topics", "threads_add", "threads_done"], seen     # (the story so far too)
assert seen["schema"]["properties"]["add"]["items"]["properties"]["kind"]["enum"] == list(memory.KINDS), seen
assert done["add"] == ["Wants directions only, no explanations."] and done["update"], done
assert any(x["text"].startswith("Learned:") for x in msgs)
assert c.deps.memory.last_reflect > 0
# FACTS citing memory ids are counted, not checked against the knowledge base
assert c._local_tool("memory_list", {})["notes"]
print("memory: ok")
