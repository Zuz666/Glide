#!/usr/bin/env python3
"""The Glide window (addon Panel.lua) under Lua 5.1 with the mocked WoW API: /glide opens it, Escape closes it,
every control shows its text, every checkbox switches the real setting (its label is part of the click area), the
coach kinds grey out under the master switch, the cell stepper keeps 1-8, and changes made elsewhere (slash commands)
show in the open window. Needs lupa."""
import os
import sys

from lupa import lua51

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wowmock  # noqa: E402

L = lua51.LuaRuntime(unpack_returned_tuples=True)
L.execute(wowmock.MOCK)
wowmock.install_bit(L)
ns = L.table()
toc = [ln.strip() for ln in open(os.path.join(ROOT, "addon", "Glide", "Glide.toc"), encoding="utf-8")
       if ln.strip().endswith(".lua") and not ln.startswith(("Data", "Recorder", "Scanner", "RXPDump"))]
assert toc[-1] == "Panel.lua" and toc.index("Minimap.lua") < toc.index("Panel.lua"), toc
for f in toc:
    src = open(os.path.join(ROOT, "addon", "Glide", f), encoding="utf-8").read()
    L.eval("function(src, name, a, ns) local f, e = loadstring(src, name) if not f then error(e) end return f(a, ns) end")(
        src, "@" + f, "Glide", ns)
G = L.globals()
G.fire("ADDON_LOADED", "Glide")
G.fire("PLAYER_LOGIN")

# ---- opening: /glide alone, the minimap button; Escape closes it like the game's windows --------------------------
G.SlashCmdList.GLIDE("")
P = G.GlideOptionsFrame
assert P is not None and P.shown, "/glide opens the window"
assert "GlideOptionsFrame" in list(G.UISpecialFrames.values()), "Escape closes it"
assert G.SOUNDKIT.IG_CHARACTER_INFO_OPEN in list(G.SOUNDS.values()), "opening sounds like a game window"
G.SlashCmdList.GLIDE("")
assert not P.shown, "/glide again closes it"
G.SlashCmdList.GLIDE("help")
assert not P.shown, "/glide help prints the commands, it doesn't open the window"
G.SlashCmdList.GLIDE("")


def kids(frame):
    return list((frame.children or L.table()).values())


def strings(frame):
    out = list((frame.fontstrings or L.table()).values())
    for k in kids(frame):
        out += strings(k)
    return out


boxes = [k for k in kids(P) if k.kind == "CheckButton"]
buttons = [k for k in kids(P) if k.kind == "Button"]
labels = {b.label.text: b for b in boxes}

# ---- every control has its text: the bug this window replaces ----------------------------------------------------
for fs in strings(P):
    if fs.shown and fs is not None:
        assert fs.text not in (None, ""), "a visible font string with no text (font %s)" % fs.font
for b in boxes:
    assert not b.Text.shown, "the template's own label is hidden; Glide draws its own"
    assert b.label.text and b.hitRight < -len(b.label.text) * 5, "the label is part of the click area: %s" % b.label.text
for b in buttons:
    assert b.template != "UIPanelButtonTemplate" or b.text, "every button says what it does"
assert P.status.text and "Streaming" in P.status.text, P.status.text
want = ["Coach commentary", "Route", "Combat", "Buffs", "Gear", "Pace", "Upkeep", "Voice",
        "Auto repair", "Bag cleanup", "New spells on level up", "Stacks on cooldown bars", "Data strip", "Minimap button"]
assert [b.label.text for b in boxes] == want, [b.label.text for b in boxes]


def click(w):
    """A left click as the game delivers it (Lua method: self passed explicitly from Python)."""
    w.Click(w)


def cf():
    return {k: bool(G.GlideLinkDB.coach[k] is not False) for k in
            ("coach", "route", "combat", "buffs", "gear", "pace", "upkeep", "voice")}


# ---- checkboxes switch the real settings, with the game's checkbox sounds --------------------------------------
n = len(list(G.SOUNDS.values()))
click(labels["Combat"])
assert not cf()["combat"] and not labels["Combat"].checked
assert list(G.SOUNDS.values())[-1] == G.SOUNDKIT.IG_MAINMENU_OPTION_CHECKBOX_OFF and len(list(G.SOUNDS.values())) > n
click(labels["Combat"])
assert cf()["combat"] and labels["Combat"].checked
click(labels["Auto repair"]); assert not ns.RepairOn()
click(labels["Auto repair"]); assert ns.RepairOn()
click(labels["Bag cleanup"]); assert not ns.TrashOn()
click(labels["Bag cleanup"]); assert ns.TrashOn()
click(labels["Data strip"])
assert not ns.StripOn() and "off" in P.status.text and G.GlideMinimapButton.icon.vg < 1, "strip off shows everywhere"
click(labels["Data strip"]); assert ns.StripOn() and "Streaming" in P.status.text
click(labels["Minimap button"]); assert not G.GlideMinimapButton.shown
click(labels["Minimap button"]); assert G.GlideMinimapButton.shown

# ---- the master switch greys out the kinds, and a greyed kind can't be clicked ---------------------------------
click(labels["Coach commentary"])
assert not cf()["coach"] and G.GlideMinimapButton.icon.desat
for name in want[1:8]:
    b = labels[name]
    assert not b.enabled and b.label.font == "GameFontDisable", name
click(labels["Route"]); assert cf()["route"], "a disabled kind ignores clicks"
labels["Route"].scripts.OnEnter(labels["Route"])
assert "Turn on Coach commentary" in " ".join(G.GameTooltip.lines.values()), "a greyed kind says why"
click(labels["Coach commentary"])
assert all(labels[nm].enabled and labels[nm].label.font == "GameFontHighlight" for nm in want[1:8])

# ---- "Turn all on" -----------------------------------------------------------------------------------------
click(labels["Voice"]); click(labels["Gear"])
assert not cf()["voice"] and not cf()["gear"]
reset = [b for b in buttons if b.text == "Turn all on"][0]
click(reset)
assert all(cf().values()) and labels["Voice"].checked and labels["Gear"].checked

# ---- the cell size stepper: 1 to 8, the ends disable their button ------------------------------------------------
minus = [b for b in buttons if b.text == "-"][0]
plus = [b for b in buttons if b.text == "+"][0]
value = [fs for fs in strings(P) if fs.text and fs.text.endswith(" px")][0]
assert ns.StripCell() == 3 and value.text == "3 px", value.text
click(plus); assert ns.StripCell() == 4 and value.text == "4 px" and G.GlideLinkDB.cell == 4
for _ in range(10):
    click(plus)
assert ns.StripCell() == 8 and not plus.enabled and minus.enabled
for _ in range(10):
    click(minus)
assert ns.StripCell() == 1 and not minus.enabled and plus.enabled
G.SlashCmdList.GLIDELINK("cell 3")
assert value.text == "3 px", "a slash command shows in the open window"
click(labels["Data strip"])                     # the strip off: its cell size greys out, and says why
assert not plus.enabled and not minus.enabled and value.font == "GameFontDisable"
plus.scripts.OnEnter(plus)
assert "Turn on the data strip" in " ".join(G.GameTooltip.lines.values())
click(labels["Data strip"])
assert plus.enabled and minus.enabled and value.font == "GameFontHighlight"

# ---- changes made elsewhere show in the open window -----------------------------------------------------------------
G.SlashCmdList.GLIDECOACH("combat off")
assert not labels["Combat"].checked
G.SlashCmdList.GLIDE("repair off")
assert not labels["Auto repair"].checked
G.SlashCmdList.GLIDE("repair on")
G.SlashCmdList.GLIDECOACH("reset")
assert labels["Combat"].checked

# ---- tooltips explain each option; the window keeps its place ------------------------------------------------------
labels["Bag cleanup"].scripts.OnEnter(labels["Bag cleanup"])
assert G.GameTooltip.shown and "RestedXP" in " ".join(G.GameTooltip.lines.values())
P.scripts.OnDragStop(P)
assert G.GlideLinkDB.panel.point == "CENTER" and G.GlideLinkDB.panel.y == 60
G.GlideMinimapButton.scripts.OnClick(G.GlideMinimapButton, "LeftButton")
assert not P.shown, "the minimap button closes it too"
print("OK")
