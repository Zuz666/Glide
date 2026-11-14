#!/usr/bin/env python3
"""The recorder's flight-master scan (Recorder.lua, TAXIMAP_OPENED) keeps both map coordinates of every node: the
y coordinate was always nil (`local x, y = a and b:GetXY()` keeps only x). Runs the whole addon under Lua 5.1 with
the mocked WoW API (tests/wowmock.py). Needs lupa.

Run: python3 tests/test_recorder_taxi.py   (prints OK)
"""
import os
import sys

from lupa import lua51

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import wowmock  # noqa: E402

L = lua51.LuaRuntime(unpack_returned_tuples=True)
L.execute(wowmock.MOCK)
wowmock.install_bit(L)
L.execute(r"""
function GetTaxiMapID() return 1414 end
local function at(x, y) return { GetXY = function() return x, y end } end
C_TaxiMap = { GetAllTaxiNodes = function() return {
  { nodeID = 23, state = 0, name = "Orgrimmar, Durotar", position = at(0.5134, 0.1922), slotIndex = 1 },
  { nodeID = 25, state = 1, name = "Crossroads, The Barrens", position = at(0.5155, 0.3022), slotIndex = 2 },
  { nodeID = 80, state = 2, name = "Ratchet, The Barrens" } } end }
""")
ns = L.table()
loader = L.eval("function(src, name, a, ns) local f, e = loadstring(src, name) if not f then error(e) end "
                "return f(a, ns) end")
for f in [ln.strip() for ln in open(os.path.join(ROOT, "addon", "Glide", "Glide.toc"), encoding="utf-8")
          if ln.strip().endswith(".lua")]:
    loader(open(os.path.join(ROOT, "addon", "Glide", *f.split("\\")), encoding="utf-8").read(), "@" + f, "Glide", ns)
G = L.globals()
G.fire("ADDON_LOADED", "Glide")
G.fire("PLAYER_LOGIN")
G.fire("TAXIMAP_OPENED")

db = G.GlideRecorderDB
taxi = db.sessions[db.lastSession].taxi
og, xr, ratchet = taxi["Orgrimmar, Durotar"], taxi["Crossroads, The Barrens"], taxi["Ratchet, The Barrens"]
assert (og.x, og.y) == (51.34, 19.22), (og.x, og.y)
assert (xr.x, xr.y) == (51.55, 30.22), (xr.x, xr.y)
assert ratchet.x is None and ratchet.y is None and ratchet.id == 80, "a node without a position keeps its id only"
print("OK")
