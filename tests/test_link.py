#!/usr/bin/env python3
"""Glide live link end to end: run the addon under Lua 5.1 with a mocked WoW API, paint its cells into an
image the way the screen would show them, and decode with the companion's protocol module."""
import os
import sys
from lupa import lua51

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "companion"))
import protocol  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wowmock  # noqa: E402
from state import GameState  # noqa: E402

L = lua51.LuaRuntime(unpack_returned_tuples=True)
L.execute(wowmock.MOCK)
ns = L.table()
# Core.lua first, as in the TOC: the /glide command and the mark live there (its recorder half: test_addon.py)
for f in ("Core.lua", "Codec.lua", "State.lua", "Gear.lua", "Extras.lua", "Coach.lua", "Strip.lua", "Minimap.lua",
          "Panel.lua"):
    src = open(os.path.join(ROOT, "addon", "Glide", f), encoding="utf-8").read()
    L.eval("function(src, name, a, ns) local f, e = loadstring(src, name) if not f then error(e) end return f(a, ns) end")(
        src, "@" + f, "Glide", ns)
G = L.globals()
G.fire("ADDON_LOADED", "Glide")
G.fire("QUEST_ACCEPTED", 92461)
G.fire("PLAYER_LEVEL_UP", 7)
G.fire("PLAYER_LOGIN")
cfg = G.GlideLinkDB
cols, rows, cell = cfg.cols, cfg.rows, cfg.cell
CAP = (cols * rows - 2) // 4 * 3 - 6             # payload bytes one frame carries
PAD = (11, 5)                                      # window offset of the strip in the "screen"
_shown = L.eval("""function(textures)
  local out = {}
  for _, t in pairs(textures) do              -- the order G.textures.values() gives: later textures paint over earlier
    if t.shown and not t.skip then             -- minimap button art (skip) is not part of the strip
      out[#out + 1] = string.format("%.17g %.17g %.17g %.17g %.17g %.17g %.17g", t.r, t.g, t.b, t.x, t.y, t.w, t.h)
    end
  end
  return table.concat(out, "\\n")
end""")


def shown_textures():
    """The shown textures as (r, g, b, x, y, w, h), read in one Lua call (reading each field from Python was slow)."""
    text = _shown(G.textures)
    return [tuple(map(float, ln.split())) for ln in text.split("\n")] if text else []


def screen_frame():
    """One strip frame: tick the addon, paint its cells as the screen shows them, decode like the companion.
    -> (payload, image)"""
    cols, rows, cell = cfg.cols, cfg.rows, cfg.cell          # (the test reuses these names later)
    G.tick()
    W, H = PAD[0] + cols * cell + 20, PAD[1] + 2 * rows * cell + 20      # binary strip: two cells per symbol
    buf = bytearray([30, 30, 40] * (W * H))
    for r, g, b, x, y, w, h in shown_textures():
        col = bytes((int(round(r * 255)), int(round(g * 255)), int(round(b * 255))))
        # a row of the texture at a time (pixel by pixel took most of the test); a row that isn't wholly inside the
        # buffer goes pixel by pixel as before, so the bytes are exactly the same
        x0, y0, w, h = PAD[0] + int(x), PAD[1] + int(-y), int(w), int(h)
        row, n, end = col * w, 3 * w, len(buf)
        for yy in range(h):
            j = ((y0 + yy) * W + x0) * 3
            if n > 0 and 0 <= j and j + n <= end:
                buf[j:j + n] = row
                continue
            for xx in range(w):
                k = j + xx * 3
                buf[k:k + 3] = col
    img = protocol.Image(W, H, bytes(buf), bgra=False)
    res, loc = protocol.decode_image(img, cols, rows)
    assert res, "frame did not decode (loc=%s)" % (loc,)
    assert loc == (PAD[0], PAD[1], cell, protocol.BINARY), loc
    return res[1], img


state = GameState()
pages_seen = set()
page_text = {}                                     # page id -> full page as sent
for frame in range(40):
    payload, img = screen_frame()
    state.gauges = protocol.read_gauges(img, (PAD[0], PAD[1], cell, protocol.BINARY), rows)
    core, page = protocol.parse_payload(payload)
    pages_seen.add(page.get("pg"))
    if page.get("pg"):
        page_text[page["pg"]] = payload.partition("|")[2]
    ring = state.update(core, page)
    if frame == 0:
        print("payload bytes:", len(payload.encode()), "capacity:", (cols * rows - 2) // 4 * 3 - 6)
        print(payload)
        assert {k for _, k, _ in ring} == {"acc", "lvl"}, ring

s = state.summary()
assert s["level"] == 7 and s["xp"] == 1234 and s["hp"] == 80 and not s["combat"], s
assert s["target"]["npc"] == "250001" and s["target"]["hp"] == 40, s["target"]   # secret value -> read from the gauge
assert state.gauges == {"hp": 80, "power": 50, "target_hp": 40}, state.gauges
assert s["power"] == 50 and s["power_type"] == "rage" and s["class"] == "WARRIOR", (s["power"], s["power_type"], s["class"])
assert abs(s["x"] - 42.31) < 0.01 and s["map"] == 2521, (s["x"], s["map"])
assert s["durability"] == 30 and s["free_bags"] == 18, (s["durability"], s["free_bags"])
assert s["guide"] == "1-14 Zephras Isle" and s["step"] == 5, s["guide"]
st = s["steps"][0]
assert st["elements"][0] == {"tag": "goto", "map": 2521, "x": 42.1, "y": 23.5}, st
assert st["elements"][1]["quest"] == 92461 and s["steps"][1]["elements"][0]["quest"] == 92462, s["steps"]
assert s["quests"] == {92461: {"done": False, "obj": [(3, 8)]}}, s["quests"]
assert s["zone"] == "Zephras Isle" and s["name"] == "Fermin", (s["zone"], s["name"])
b = {x["name"]: x for x in state.summary()["buffs"]}
now = G.NOW
assert b["Battle Shout"]["mine"] and b["Battle Shout"]["castable"] and abs(b["Battle Shout"]["remaining"] - (190 - now)) <= 1, (b, now)
assert abs(b["Power Word  Fortitude"]["remaining"] - (1900 - now)) <= 1 and not b["Power Word  Fortitude"]["mine"], b
assert not b["Power Word  Fortitude"]["castable"], b
assert abs(b["Main-hand enchant"]["remaining"] - 600) <= 5, b   # mock enchant time does not tick
assert state.summary()["addon_version"] == "0.15.3", state.summary()["addon_version"]
w, i = state.gear["W"], state.gear["I"]
assert len(w) == 1 and w[0]["name"] == "Battleworn Hammer" and w[0]["stats"] == {"DP": 3.5, "WS": 2.9}, w
assert len(i) == 1 and i[0]["name"] == "Heavy Hand Axe" and i[0]["key"] == "0.1", i      # hearthstone skipped
assert i[0]["stats"] == {"ST": 3.0, "DP": 8.1, "WS": 3.2, "CR": 1.0} and i[0]["req"] == 9, i[0]
assert state.map_sizes.get(2521) == (3000.0, 2000.0), state.map_sizes
# Windows Auto HDR, as captured from WoW Forever 1.60.1.70532 on 2026-10-21 (all 64 old 6-bit colours drawn in a row):
# mid levels boost and clip, 64 colours land on 41, so the old strip can't be read. Off/on survive.
LV = (0, 85, 170, 255)
HDR = {(0, 0, 0): (0, 0, 0), (0, 0, 85): (0, 0, 153), (0, 0, 170): (5, 1, 255), (0, 0, 255): (23, 1, 255),
       (0, 255, 0): (0, 255, 0), (0, 255, 255): (0, 255, 255), (255, 0, 0): (255, 0, 0), (255, 0, 255): (255, 0, 255),
       (255, 255, 0): (255, 255, 0), (255, 255, 255): (255, 255, 255), (85, 85, 85): (153, 153, 153),
       (170, 170, 170): (255, 255, 255), (0, 170, 170): (0, 255, 255), (85, 255, 0): (0, 255, 0)}
assert len({HDR[k] for k in ((170, 170, 170), (255, 255, 255))}) == 1          # why the 6-bit strip fails


def through(img, f):
    raw = bytearray()
    for y in range(img.h):
        for x in range(img.w):
            raw += bytes(f(img.px(x, y)))
    return protocol.Image(img.w, img.h, bytes(raw), bgra=False)


payload, img = screen_frame()
for name, f in (("auto hdr", lambda px: HDR.get(px, px)),
                ("auto hdr, drifting", lambda px: tuple(int(40 + v * 0.75) for v in HDR.get(px, px)))):
    res, loc = protocol.decode_image(through(img, f), cols, rows)
    assert res and res[1] == payload and loc[3] == protocol.BINARY, name
# the old 6-bit strip (cyan marker, older addons) still reads without HDR, and fails under it
syms = protocol.encode_to_symbols(9, "l=7", CAP + 6)
old = protocol.render(syms, cols, rows, cell)
res, loc = protocol.decode_image(old, cols, rows)
assert res == (9, "l=7") and loc[3] == protocol.SIX, (res, loc)
boost = {0: 0, 85: 153, 170: 255, 255: 255}                        # the measured grey levels
assert protocol.decode_image(through(old, lambda px: tuple(boost.get(v, v) for v in px)), cols, rows)[0] is None
assert protocol.decode_image(protocol.render(syms, cols, rows, cell, binary=True), cols, rows)[0] == (9, "l=7")
# unreadable aura data -> last good list kept, ak=1; in combat the list is frozen; casts still reported
L.execute('''
OLD_AURAS = C_UnitAuras.GetAuraDataByIndex
C_UnitAuras.GetAuraDataByIndex = function(u, i) return {name = "SECRET", spellId = 6673, expirationTime = "SECRET"} end
''')
ns.RefreshPages()
core, _ = protocol.parse_payload(ns.Core() + "|")
assert core["ak"] == "1" and core["an"] == "1" and ns.buffSource == "unreadable", (core, ns.buffSource)
L.execute("C_UnitAuras.GetAuraDataByIndex = function() return nil end; COMBAT = true")
ns.RefreshPages()
core, _ = protocol.parse_payload(ns.Core() + "|")
assert core["ak"] == "1" and core["an"] == "1" and ns.buffSource == "frozen (combat)", (core, ns.buffSource)
# right after combat: the first (empty) read is not published; two agreeing reads are
L.execute("COMBAT = false")
ns.RefreshPages()
assert ns.buffSource == "settling" and ns.Core().find("ak=1") is not None
L.execute("C_UnitAuras.GetAuraDataByIndex = OLD_AURAS")
ns.RefreshPages(); ns.RefreshPages()
assert ns.buffSource == "list" and ns.Core().find("ak=0") is not None, ns.buffSource
G.fire("UNIT_SPELLCAST_SUCCEEDED", "player", "cast-guid", 6673)
core, _ = protocol.parse_payload(ns.Core() + "|")
assert core["e1"].endswith(":cst:6673"), core["e1"]
# trained skills: core sw/sa bitmasks -> state.skills (item subclass ids)
sk = state.summary()["skills"]
assert sk["weapons"] == {0, 4, 7} and sk["armor"] == {1, 2, 3, 6}, sk
L.execute("KNOWN[202] = true")
ns.RefreshPages()
c, _ = protocol.parse_payload(ns.Core() + "|")
assert int(c["sw"]) == (1 | 16 | 128 | 256), c["sw"]
L.execute("IsPlayerSpell = nil")
ns.RefreshPages()
c, _ = protocol.parse_payload(ns.Core() + "|")
assert c["sw"] == "" and c["sa"] == "", (c["sw"], c["sa"])     # no API -> unknown, not "nothing trained"

# coach switches: /coach ... -> core cf bitmask -> companion flags -> trigger filter
import rules as rules_mod
def cf():
    c, _ = protocol.parse_payload(ns.Core() + "|")
    st = GameState()
    st.core = c
    return st.coach_flags()
assert all(cf().values())
G.SlashCmdList.GLIDECOACH("combat off"); G.SlashCmdList.GLIDECOACH("voice")
f = cf()
assert not f["combat"] and not f["voice"] and f["coach"] and f["route"], f
assert not rules_mod.allowed("low_health_in_combat", f) and rules_mod.allowed("step_changed", f)
G.SlashCmdList.GLIDELINK("coach off")
f = cf()
assert not f["coach"] and not rules_mod.allowed("step_changed", f) and rules_mod.allowed("addon_outdated", f)
G.SlashCmdList.GLIDECOACH("on"); G.SlashCmdList.GLIDECOACH("fight on")
f = cf()
assert f["coach"] and f["combat"] and not f["voice"], f
G.SlashCmdList.GLIDECOACH("reset")
assert all(cf().values()) and G.GlideLinkDB.coach is not None
# minimap button: left-click = the Glide window (test_addon_panel.py), right-click = coach on/off, drag = move
btn = G.GlideMinimapButton
assert btn is not None and btn.shown, "minimap button"
btn.scripts.OnClick(btn, "RightButton")
assert not cf()["coach"] and btn.icon.desat
btn.scripts.OnClick(btn, "RightButton")
assert cf()["coach"] and not btn.icon.desat
btn.scripts.OnClick(btn, "LeftButton")
assert G.GlideOptionsFrame.shown, "left-click opens the Glide window"
btn.scripts.OnClick(btn, "LeftButton")
assert not G.GlideOptionsFrame.shown, "and closes it"
assert G.MENU is None, "MenuUtil must not be used (taints the UI panel manager)"
G.SlashCmdList.GLIDELINK("off"); assert btn.icon.vg < 1, "strip off: the button turns red"
G.SlashCmdList.GLIDELINK("on"); assert btn.icon.vg == 1
btn.scripts.OnDragStart(btn); btn.scripts.OnUpdate(btn)
assert abs(G.GlideLinkDB.minimap.angle - 90) < 0.01, G.GlideLinkDB.minimap.angle
G.SlashCmdList.GLIDELINK("minimap off"); assert not btn.shown
G.SlashCmdList.GLIDELINK("minimap on"); assert btn.shown

# ---- mark: "/glide mark <note>" (Core.lua) -> live ring event "mark" -> companion -> feedback.py ----
assert G.GlideRecorderDB.sessions[G.GlideRecorderDB.lastSession] is not None and ns.errors == 0, ns.lastError
G.SlashCmdList.GLIDE("mark hello")
c, _ = protocol.parse_payload(ns.Core() + "|")
assert c["e1"].endswith(":mark:hello"), c["e1"]
payload, _ = screen_frame()                                   # through the strip, the screen and the decoder
core, page = protocol.parse_payload(payload)
new = state.update(core, page)
assert [a for _, k, a in new if k == "mark"] == ["hello"], new
G.Glide_Mark()                                                # the key binding: a mark without a note
c, _ = protocol.parse_payload(ns.Core() + "|")
assert c["e1"].endswith(":mark:") and c["e2"].endswith(":mark:hello"), (c["e1"], c["e2"])
assert ns.Mark("a" + "é" * 30) == "a" + "é" * 19       # cut to 40 bytes, never inside a character
# long notes are cleaned of ;=|: and cut to 40 bytes. Three of them leave less room for the page: a page
# that no longer fits whole waits (nothing cut, nothing lost) and goes out once short events push them out.
L.execute("RXP.currentGuide.name = string.rep('n', 40); RXP.currentGuide.group = string.rep('g', 40);"
          "RXP.currentGuide.next = string.rep('x', 26)")         # page G: 122 bytes
ns.RefreshPages()
for i in range(3):
    G.SlashCmdList.GLIDE("mark note %d: turn=in; |now| " % i + "z" * 60)
c, _ = protocol.parse_payload(ns.Core() + "|")
for k in ("e1", "e2", "e3"):
    _, kind, arg = c[k].split(":", 2)
    assert kind == "mark" and len(arg.encode()) == 40 and not set(arg) & set(";=|:"), c[k]
assert c["e3"].split(":", 2)[2] == "note 0 turn in now " + "z" * 21, c["e3"]
assert len(ns.Core()) + 1 + 122 > CAP, "page G would not fit whole"
sent = set()
for _ in range(20):
    payload, _ = screen_frame()
    assert payload == ns.lastPayload and len(payload.encode()) <= CAP, (len(ns.lastPayload.encode()), CAP)
    core, page = protocol.parse_payload(payload)
    state.update(core, page)
    sent.add(page.get("pg"))
assert "G" not in sent and len(sent - {None}) >= 5, sent
G.fire("PLAYER_DEAD"); G.fire("PLAYER_ALIVE"); G.fire("PLAYER_UNGHOST")
for _ in range(5):
    payload, _ = screen_frame()
    assert payload == ns.lastPayload and len(payload.encode()) <= CAP
    core, page = protocol.parse_payload(payload)
    state.update(core, page)
    if page.get("pg") == "G":
        break
assert state.pages["G"]["gx"] == "x" * 26 and state.summary()["guide"] == "n" * 40, state.pages["G"]
# NextPage(room) skips what doesn't fit and keeps it queued
L.execute("RXPCData.currentStep = 6")
ns.RefreshPages()
assert ns.NextPage(5) == ""
assert ns.NextPage(1000).startswith("pg=S0;i=6;")

# Step titles lose color markup before protocol escaping or the 60-byte budget consumes visible words.
from app import App  # noqa: E402

saved_step = G.RXP.currentGuide.steps[6]
title = "If you see a Tornado, use it for faster travel."
for raw, visible, quest in (
        ("|cFFFCDC00" + title + "|r", title, False),
        ("|cRXP_WARN_Use |cRXP_ENEMY_the path|r safely|r", "Use the path safely", False),
        ("|cnWARNING_COLOR:Follow the road|r", "Follow the road", True),
        ("|cff00ff00" + "A long visible instruction " * 4 + "|r", ("A long visible instruction " * 4)[:60], True),
        ("Choose ||cFFFCDC00 literally; then=go", "Choose ,cFFFCDC00 literally, then,go", False)):
    element = L.table_from({"tag": "complete" if quest else "text", "text": raw})
    if quest:
        element.questId = 92461
    G.RXP.currentGuide.steps[6] = L.table_from({"elements": L.table_from([element])})
    ns.RefreshPages()
    core, page = protocol.parse_payload(ns.Core() + "|" + ns.NextPage(1000))
    assert page["pg"] == "S0" and page["tx"] == visible, page
    title_state = GameState()
    title_state.update(core, page)
    summary = title_state.summary()
    assert summary["steps"][0]["text"] == visible, summary["steps"]
    assert visible in App.step_line(summary), App.step_line(summary)
    assert element.text == raw, "Serialization must not alter the rich guide text"
    if raw == "|cFFFCDC00" + title + "|r" and os.environ.get("GLIDE_STEP_TITLE_PROOF"):
        import json
        with open(os.environ["GLIDE_STEP_TITLE_PROOF"], "w", encoding="utf-8") as fh:
            json.dump({"steps": summary["steps"], "level": summary["level"], "title": visible,
                       "overlay_step": App.step_line(summary)}, fh)
G.RXP.currentGuide.steps[6] = saved_step
ns.RefreshPages()

# feedback.py: one JSON line per mark in data/live/flags.jsonl
import json
import tempfile
from feedback import Feedback
fb = Feedback(tempfile.mkdtemp())
assert fb.count() == 0
msgs = [{"kind": "callout", "text": "message %d" % i, "t": 1000.0 + i} for i in range(8)]
trig = [rules_mod.Trigger("zone_changed", {"arg": "Zephras Isle"}, 3), rules_mod.Trigger("mark", {"arg": "hello"}, 3)]
summary = state.summary()
summary["buffs"] = summary["buffs"] * 5                        # a big list stays out of the flag
summary["skills"] = {"weapons": {7, 0, 4}, "armor": None}       # sets -> sorted lists
rec = fb.on_mark("hello", summary, msgs, trig)
assert rec is not None and fb.count() == 1
with open(fb.path, encoding="utf-8") as fh:
    line = json.loads(fh.read().splitlines()[0])
assert set(line) == {"t", "note", "state", "last_messages", "recent_triggers"}, set(line)
assert line["note"] == "hello" and line["t"] > 1.7e9
assert [m["text"] for m in line["last_messages"]] == ["message %d" % i for i in range(3, 8)], line["last_messages"]
assert [t["kind"] for t in line["recent_triggers"]] == ["zone_changed", "mark"], line["recent_triggers"]
assert line["recent_triggers"][1]["facts"] == {"arg": "hello"} and line["recent_triggers"][1]["priority"] == 3
st = line["state"]
assert st["level"] == 7 and st["map"] == 2521 and st["guide"] == "n" * 40, st
assert "buffs" not in st and st["_omitted"] == {"buffs": len(summary["buffs"])}, st.get("_omitted")
assert st["skills"] == {"weapons": [0, 4, 7], "armor": None} and st["quests"] == {"92461": {"done": False, "obj": [[3, 8]]}}, st
assert fb.on_mark(None, None, None, None)["note"] == "" and fb.count() == 2

# ---- one error policy (Core.lua ns.ReportError): a failing live-link handler is counted, kept and sent ----------
assert ns.errors == 0 and "er=" not in ns.Core(), ns.lastError
G.ns = ns
L.execute('ns.On("PLAYER_FLAGS_CHANGED", function() error("broken handler") end)')
G.fire("PLAYER_FLAGS_CHANGED")
G.fire("PLAYER_FLAGS_CHANGED")
c, _ = protocol.parse_payload(ns.Core() + "|")
assert c["er"] == "2" and "broken handler" in ns.lastError, (c.get("er"), ns.lastError)
sess = G.GlideRecorderDB.sessions[G.GlideRecorderDB.lastSession]
assert any("broken handler" in e for e in sess.errors.values()), "kept with the session like the recorder's"
for _ in range(40):
    payload, _ = screen_frame()
    core, page = protocol.parse_payload(payload)
    state.update(core, page)
    if page.get("pg") == "E":
        break
errs = state.summary()["addon_errors"]
assert errs["count"] == 2 and errs["last"].startswith("PLAYER_FLAGS_CHANGED: ") and "broken handler" in errs["last"], errs
print("pages seen:", sorted(p for p in pages_seen if p))
print("OK")
