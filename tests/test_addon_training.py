#!/usr/bin/env python3
"""The new-at-your-trainer window (addon Training.lua) under Lua 5.1 with the mocked WoW API:
- /glide train lists the spells and ranks the trainer teaches now (RestedXP's list less the spellbook), each with
  its icon, name, rank and the spell's own text; a higher rank shows what it changes against the known one
  ("15 -> 35 (+133%)"), a new spell says so; costs come from the trainers the recorder has seen, with the total and
  the player's gold; hovering a spell shows the game's tooltip for it;
- a level up that brings spells opens it by itself, in combat only once combat ends, and not when switched off;
- the Glide window has the switch. Needs lupa."""
import os
import sys

from lupa import lua51

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wowmock  # noqa: E402

L = lua51.LuaRuntime(unpack_returned_tuples=True)
L.execute(wowmock.MOCK)
wowmock.install_bit(L)
L.execute(r"""
LEVEL = 12
function UnitLevel(u) return u == "target" and 11 or LEVEL end
function UnitRace() return "Orc", "Orc" end
KNOWN = { [6673] = true, [100] = true, [284] = true, [6546] = true }
NAMES = { [6673] = {"Battle Shout", "Rank 1"}, [100] = {"Charge", "Rank 1"}, [284] = {"Heroic Strike", "Rank 2"},
  [1715] = {"Hamstring", "Rank 1"}, [6546] = {"Rend", "Rank 2"}, [5242] = {"Battle Shout", "Rank 2"},
  [1160] = {"Demoralizing Shout", "Rank 1"} }
DESC = { [6673] = "The warrior shouts, increasing the melee attack power of all party members by 15 for 2 min.",
  [5242] = "The warrior shouts, increasing the melee attack power of all party members by 35 for 2 min.",
  [1715] = "Maims the enemy, reducing movement speed by 50% for 15 sec.",
  [1160] = "Reduces the melee attack power of all enemies within 10 yards by 35 for 30 sec." }
C_Spell = { GetSpellInfo = function(id) local n = NAMES[id] if n then return { name = n[1] } end end,
            GetSpellSubtext = function(id) return NAMES[id] and NAMES[id][2] end,
            GetSpellDescription = function(id) return DESC[id] end,
            GetSpellTexture = function(id) return 132333 end }
BOOK = { {name = "Battle Shout", subName = "Rank 1", spellID = 6673}, {name = "Charge", subName = "Rank 1", spellID = 100},
         {name = "Heroic Strike", subName = "Rank 2", spellID = 284}, {name = "Rend", subName = "Rank 2", spellID = 6546} }
C_SpellBook = { GetNumSpellBookSkillLines = function() return 1 end,
  GetSpellBookSkillLineInfo = function() return { itemIndexOffset = 0, numSpellBookItems = #BOOK } end,
  GetSpellBookItemInfo = function(slot) return BOOK[slot] end }
function IsPlayerSpell(id) return KNOWN[id] or false end
RXP.defaultSpellList = { WARRIOR = { [1] = {6673}, [4] = {100}, [8] = {284, 1715}, [10] = {6546}, [12] = {5242},
                                     [14] = {1160} } }
COMBAT_LOCK = false
function InCombatLockdown() return COMBAT_LOCK end
function GetMoney() return 2575 end
function GetCoinTextureString(c) return c .. "c" end
""")
ns = L.table()
toc = [ln.strip() for ln in open(os.path.join(ROOT, "addon", "Glide", "Glide.toc"), encoding="utf-8")
       if ln.strip().endswith(".lua") and not ln.startswith(("Data", "Recorder", "Scanner", "RXPDump"))]
assert toc.index("Extras.lua") < toc.index("Training.lua") < toc.index("Panel.lua"), toc
for f in toc:
    src = open(os.path.join(ROOT, "addon", "Glide", f), encoding="utf-8").read()
    L.eval("function(src, name, a, ns) local f, e = loadstring(src, name) if not f then error(e) end return f(a, ns) end")(
        src, "@" + f, "Glide", ns)
G = L.globals()
# a trainer the recorder saw: Battle Shout rank 2 costs 100c there
L.execute('GlideRecorderDB = { sessions = { a = { trainers = { [3169] = { services = { '
          '{ n = "Battle Shout", r = "Rank 2", cost = 100 }, { n = "Hamstring", r = "Rank 1", cost = 40 } } } } } } }')
G.fire("ADDON_LOADED", "Glide")
G.fire("PLAYER_LOGIN")
ns.ExtrasFlush()

# ---- what a new rank changes ---------------------------------------------------------------------------------
assert ns.RankChange("by 15 for 2 min", "by 35 for 2 min") == "15 -> 35 (+133%)", ns.RankChange("by 15 for 2 min", "by 35 for 2 min")
assert ns.RankChange("causes 11 to 19 damage", "causes 21 to 33 damage") == "11 -> 21, 19 -> 33 (+91%)"
assert ns.RankChange("one 5", "two numbers 5 and 7") is None, "texts that don't line up show no change"
assert ns.RankChange("", "anything 3") is None

# ---- /glide train ----------------------------------------------------------------------------------------------
G.SlashCmdList.GLIDE("train")
F = G.GlideTrainingFrame
assert F is not None and F.shown, "/glide train opens the window"
assert "GlideTrainingFrame" in list(G.UISpecialFrames.values()), "Escape closes it"


def kids(frame):
    return list((frame.children or L.table()).values())


def rows():
    return [r for r in kids(F.list) if r.shown]


got = {r.name.text: r for r in rows()}
assert set(got) == {"Hamstring", "Battle Shout"}, list(got)
shout, ham = got["Battle Shout"], got["Hamstring"]
assert shout.rank.text.startswith("Rank 2") and "level 12" in shout.rank.text, shout.rank.text
assert "15 -> 35 (+133%)" in shout.change.text, shout.change.text
assert "by 35" in shout.desc.text, "the new rank's own text"
assert "New spell" in ham.change.text and "50%" in ham.desc.text, (ham.change.text, ham.desc.text)
assert shout.cost.text == "100c" and ham.cost.text == "40c", (shout.cost.text, ham.cost.text)
assert "2 spells" in F.sub.text and "Level 12" in F.sub.text, F.sub.text
assert "140c" in F.total.text and "2575c" in F.total.text, F.total.text
# hovering a spell: the game's own tooltip for it
shout.scripts.OnEnter(shout)
assert G.GameTooltip.spell == 5242 and G.GameTooltip.shown
shout.scripts.OnLeave(shout)
ns.ToggleTraining(False)
assert not F.shown

# ---- a level up that brings spells opens it; in combat, once combat ends; switched off, never --------------------
G.LEVEL = 14
G.fire("PLAYER_LEVEL_UP", 14)
ns.ExtrasFlush()
assert F.shown, "the level up brought Demoralizing Shout: the window opens"
assert "Demoralizing Shout" in [r.name.text for r in rows()]
ns.ToggleTraining(False)

G.LEVEL = 15
G.fire("PLAYER_LEVEL_UP", 15)
ns.ExtrasFlush()
assert not F.shown, "nothing new at 15: it stays closed"

G.LEVEL = 14
G.COMBAT_LOCK = True
G.fire("PLAYER_LEVEL_UP", 14)
ns.ExtrasFlush()
assert not F.shown, "never in combat"
G.COMBAT_LOCK = False
G.fire("PLAYER_REGEN_ENABLED")
assert F.shown, "it opens once combat ends"
ns.ToggleTraining(False)

ns.SetTrainAuto(False)
G.fire("PLAYER_LEVEL_UP", 14)
ns.ExtrasFlush()
assert not F.shown, "switched off, it never opens by itself"
G.SlashCmdList.GLIDE("train")
assert F.shown, "/glide train still opens it"
ns.SetTrainAuto(True)
print("OK")
