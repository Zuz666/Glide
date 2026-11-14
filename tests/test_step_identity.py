#!/usr/bin/env python3
"""Source-step identity contract fixtures; no game, database, or pytest needed."""
import hashlib
import os
import sys
from unittest.mock import patch

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

from knowledge.step_identity import StepIdentityIndex  # noqa: E402
from knowledge.rxp_parser import parse_guide  # noqa: E402


WARRIOR = {"class": "WARRIOR", "race": "Human", "faction": "Alliance"}
MAGE = dict(WARRIOR, **{"class": "MAGE"})


def fingerprint(canonical_block):
    """Independent oracle: fixtures supply the exact canonical block bytes."""
    return "s1:" + hashlib.md5(
        b"Glide:RXP:step:1\n" + canonical_block.encode("utf-8"), usedforsecurity=False
    ).hexdigest()


def row(text, name="Trail", group="Routes", key=None):
    return {"key": key or group + "||" + name, "name": name, "grp": group, "text": text}


def summary(identity, source=2, step=1, name="Trail", group="Routes"):
    return {"step_fingerprint": identity, "step_source_index": source, "step": step,
            "guide": name, "guide_group": group}


def status(index, current, expected, player=MAGE):
    result = index.resolve(current, player)
    assert result["status"] == expected, result
    assert "steps" not in result, result
    if expected != "verified":
        assert "step" not in result, result
    return result


CURRENT = (
    "step\n"
    ".goto 1,30,40 >> Travel all the way to |cRXP_FRIENDLY_Alex|r.\n"
    "*Wait for the patrol, then cross the bridge.\n"
    "+Speak to the guard and read every instruction.\n"
)
BASE = ("#group Routes\n#name Trail\n#version 7\n"
        "step << Warrior\n>>Warrior path\n" + CURRENT +
        "step\n#group\n>>Optional party route\nstep\n>>Final destination\n")
CURRENT_ID = fingerprint(CURRENT)
EXPECTED = {"text": "Travel all the way to Alex.\nSpeak to the guard and read every instruction.",
            "notes": ["Wait for the patrol, then cross the bridge."]}
index = StepIdentityIndex([row(BASE)])
assert status(index, summary(CURRENT_ID), "verified") == {"status": "verified", "step": EXPECTED}

# Original source ordinal, never visible/browser numbering, is the lookup key.
for visible in (0, -1, 2, 9999, None, True, "1", 1.5):
    assert status(index, summary(CURRENT_ID, step=visible), "verified")["step"] == EXPECTED
for original in (1, 3, 9999):
    status(index, summary(CURRENT_ID, source=original), "mismatch")
assert status(index, {"step_source_index": 2, "step_fingerprint": CURRENT_ID},
              "verified")["step"] == EXPECTED
assert status(index, summary(CURRENT_ID, name="Other browser guide", group="Elsewhere"),
              "verified")["step"] == EXPECTED

# Other blocks and headers do not participate in this identity or its parsing.
elsewhere = BASE.replace("#version 7", "#version = runtimeFunction\n<< Female")
elsewhere = elsewhere.replace(">>Warrior path", ".unknownRuntimeFunction\n<< CustomPredicate")
elsewhere = elsewhere.replace(">>Final destination", ">>Different destination\n#foo = executable")
assert status(StepIdentityIndex([row(elsewhere)]), summary(CURRENT_ID), "verified")["step"] == EXPECTED
edited = BASE.replace("Travel all the way", "Walk directly instead")
status(StepIdentityIndex([row(edited)]), summary(CURRENT_ID), "mismatch")
inserted = BASE.replace(CURRENT, "step\n>>Inserted source step\n" + CURRENT)
status(StepIdentityIndex([row(inserted)]), summary(CURRENT_ID), "mismatch")
assert status(StepIdentityIndex([row(inserted)]), summary(CURRENT_ID, source=3),
              "verified")["step"] == EXPECTED

# Incorporated blocks resolve without their parent, includes, or settings.
PART = ("#include MissingHeader\nstep << Warrior\n#include MissingBefore\n" + CURRENT +
        "step\n#include MissingAfter\n")
part = StepIdentityIndex([row(PART, "Part")])
assert status(part, summary(CURRENT_ID, name="UnavailableRoot"), "verified")["step"] == EXPECTED
assert status(part, summary(CURRENT_ID, step=81, name="DifferentRoot"), "verified")["step"] == EXPECTED
aliases = StepIdentityIndex([row(BASE), row(PART, "Part"), row(edited, key="other-revision")])
assert status(aliases, summary(CURRENT_ID), "verified")["step"] == EXPECTED

# Missing/mismatched data and hints never authorize authored text.
status(StepIdentityIndex([]), summary(CURRENT_ID), "missing")
for unavailable in (None, ""):
    status(StepIdentityIndex([row(unavailable)]), summary(CURRENT_ID), "unverified")
status(StepIdentityIndex([row("#name Trail\n")]), summary(CURRENT_ID), "missing")
status(StepIdentityIndex([row(edited, "Other", "Elsewhere")]), summary(CURRENT_ID), "missing")
assert status(StepIdentityIndex([row(None), row(PART, "Part")]), summary(CURRENT_ID),
              "verified")["step"] == EXPECTED
for invalid in (None, "", "m1:" + "0" * 32, "s2:" + "0" * 32, "s1:" + "A" * 32,
                "s1:1234", CURRENT_ID + "\n", 12):
    status(index, summary(invalid), "unverified")
for invalid in (None, 0, -1, True, False, "2", 2.0):
    status(index, summary(CURRENT_ID, source=invalid), "unverified")
status(index, {"step_fingerprint": CURRENT_ID}, "unverified")

# A raw block digest remains identical across player-specific inline branches.
ACTION = (
    "step\n"
    ".goto 1,1,2 >> The mage takes the northern road. << Mage\n"
    ".goto 1,3,4 >> The warrior takes the southern road. << Warrior\n"
    "*A note only for mages. << Mage\n"
    "*A note only for warriors. << Warrior\n"
    ">>Shared main instruction in full.\n"
)
action_id = fingerprint(ACTION)
actions = StepIdentityIndex([row(ACTION)])
assert status(actions, summary(action_id, source=1), "verified")["step"] == {
    "text": "The mage takes the northern road.\nShared main instruction in full.",
    "notes": ["A note only for mages."]}
assert status(actions, summary(action_id, source=1), "verified", WARRIOR)["step"] == {
    "text": "The warrior takes the southern road.\nShared main instruction in full.",
    "notes": ["A note only for warriors."]}
assert status(actions, summary(action_id, source=1), "verified",
              dict(MAGE, **{"class": "mage"}))["step"]["notes"] == ["A note only for mages."]
status(actions, summary(action_id, source=1), "unverified", {})
for condition, player in (
    ("Human", {"class": "MAGE", "faction": "Alliance"}),
    ("!Horde", {"class": "MAGE", "race": "Human"}),
    ("Alliance/Horde", {"class": "MAGE", "race": "Human"}),
    ("!Warrior", {"race": "Human", "faction": "Alliance"}),
):
    block = "step\n>>Conditional instruction << " + condition + "\n"
    status(StepIdentityIndex([row(block)]), summary(fingerprint(block), source=1), "unverified", player)
normalized = "step\n>>Known player << DeathKnight NightElf Alliance\n"
assert status(StepIdentityIndex([row(normalized)]), summary(fingerprint(normalized), source=1),
              "verified", {"class": "death_knight", "race": "night elf", "faction": "alliance"})["step"][
                  "text"] == "Known player"
conditional = "step << Warrior\n>>Warrior-only instruction\n"
conditional_index = StepIdentityIndex([row(conditional)])
status(conditional_index, summary(fingerprint(conditional), source=1), "unverified")
assert status(conditional_index, summary(fingerprint(conditional), source=1), "verified",
              WARRIOR)["step"]["text"] == "Warrior-only instruction"

# Unsupported current content fails closed; unrelated content is never parsed.
for block in (
    "step << 20\n>>Level-gated\n",
    "step\n>>Gender-gated << Female\n",
    "step\n>>Locale-gated << enUS\n",
    "step\n>>Runtime-gated << CustomRuntimePredicate\n",
    "step\n#group = runtimeFunction\n>>Executable tag\n",
    "step\n<< Mage\n>>Standalone condition\n",
    "step\n.unknownRuntimeFunction 1,2,3 >> Unknown command\n",
    "step\n.singleboxtext >> Solo instruction\n.multiboxtext >> Group instruction\n",
    "step\n.singleboxtext >> Solo instruction\n",
    "step\n.multiboxtext >> Group instruction\n",
):
    status(StepIdentityIndex([row(block)]), summary(fingerprint(block), source=1), "unverified")
    assert status(StepIdentityIndex([row(block + CURRENT)]),
                  summary(CURRENT_ID), "verified")["step"] == EXPECTED

# Native already selected this step; local visibility tags are not re-applied.
settings_block = "step\n#ssf\n#hardcore\n#group\n.showwhiledead\n>>Selected by native\n"
assert status(StepIdentityIndex([row(settings_block)]),
              summary(fingerprint(settings_block), source=1), "verified")["step"] == {
                  "text": "Selected by native", "notes": []}
for block, expected in (
    ("step\n*Only a yellow note\n", {"text": "", "notes": ["Only a yellow note"]}),
    ("step\n.goto 1,2,3\n", {"text": "", "notes": []}),
    ("step\n", {"text": "", "notes": []}),
    ("step\n#label Keep\n#include Missing\n", {"text": "", "notes": []}),
    ("step\n+123\n.macro /say hi >> Not authored main text\n", {"text": "", "notes": []}),
):
    assert status(StepIdentityIndex([row(block)]), summary(fingerprint(block), source=1),
                  "verified")["step"] == expected

# Explicit canonical bytes: CR/LF, comments, ASCII edges, internal tabs, UTF-8.
UNICODE = "step\n>>Привет, путник! Déjà vu.\u2028Still one source line.\n"
noisy = ("--cached\r\n#name Anything\r\tstep\v -- comment\r\n"
         "\f>>Привет,\t путник!  Déjà\tvu.\u2028Still one source line.\t--ignored\r")
assert status(StepIdentityIndex([row(noisy)]), summary(fingerprint(UNICODE), source=1),
              "verified")["step"]["text"] == "Привет, путник! Déjà vu.\u2028Still one source line."
escaped = "step\n>>Keep ||cRXP_FRIENDLY_literal||r unchanged\n"
assert status(StepIdentityIndex([row(escaped)]), summary(fingerprint(escaped), source=1),
              "verified")["step"]["text"] == "Keep |cRXP_FRIENDLY_literal|r unchanged"
prefix_opener = "stepAnything\n>>Native counts the first four characters\n"
assert status(StepIdentityIndex([row(prefix_opener)]), summary(fingerprint(prefix_opener), source=1),
              "verified")["step"]["text"] == "Native counts the first four characters"
comment_block = "step\n>>Keep only this\n"
assert status(StepIdentityIndex([row("step\n>>Keep only this--not text\n")]),
              summary(fingerprint(comment_block), source=1), "verified")["step"]["text"] == "Keep only this"

# Metadata forms explain status only; all hints are excluded from identity.
for name, group, name_hint, group_hint in (
    ("Я" * 21, "界" * 14, "Я" * 20, "界" * 13 + "\ufffd"),
    ("Trail;=|", "Group;=|", "Trail,,,", "Group,,,"),
):
    current = summary(CURRENT_ID, name=name_hint, group=group_hint)
    assert status(StepIdentityIndex([row(BASE, name, group)]), current, "verified")["step"] == EXPECTED
    status(StepIdentityIndex([row(edited, name, group)]), current, "mismatch")
    status(StepIdentityIndex([row(None, name, group)]), current, "unverified")

# Hash once per block; parse only a matched block once; return fresh mutable values.
with patch("knowledge.step_identity.parse_guide", wraps=parse_guide) as parsing:
    cached = StepIdentityIndex([row(BASE), row(PART, "Part")])
    assert parsing.call_count == 0
    first = status(cached, summary(CURRENT_ID), "verified")
    assert parsing.call_count == 1
    assert parsing.call_args.args == (CURRENT,)
    with patch("knowledge.step_identity.hashlib.md5", side_effect=AssertionError("rehashed source block")):
        first["step"]["text"] = "consumer mutation"
        first["step"]["notes"].append("consumer note")
        assert status(cached, summary(CURRENT_ID, step=999), "verified")["step"] == EXPECTED
        assert status(cached, summary(CURRENT_ID), "verified", WARRIOR)["step"] == EXPECTED
        assert parsing.call_count == 1

# Source snapshots survive mutations to caller rows, selection, and player objects.
mutable_row = row(BASE)
rows = [mutable_row]
snapshot = StepIdentityIndex(rows)
mutable_row["text"] = edited
mutable_row["name"] = "Renamed"
rows.clear()
assert status(snapshot, summary(CURRENT_ID), "verified")["step"] == EXPECTED
status(StepIdentityIndex([mutable_row]), summary(CURRENT_ID, name="Renamed"), "mismatch")
player = dict(MAGE)
status(actions, summary(action_id, source=1), "verified", player)
player["class"] = "WARRIOR"
assert status(actions, summary(action_id, source=1), "verified", player)["step"]["notes"] == [
    "A note only for warriors."]

print("source step identity: ok")
