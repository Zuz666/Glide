"""Addon golden master: the Glide addon (every TOC file but the recorder's data) under Lua 5.1 with the mocked WoW API
(tests/wowmock.py), driven through a scripted session: login, quest events, ticks of the strip, slash commands, the
Glide window. Recorded: every payload the strip encodes, the symbols it paints, the coach mask, the window's texts and
checkbox states, the recorder's session counts. Deterministic (the mock has its own clock)."""
TIER = "portable"     # golden.py tier: the addon and the mocked WoW API only
import hashlib
import json
import os
import sys

from lupa import lua51

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import wowmock  # noqa: E402

MASKED_VERSION = "golden"           # what the probe's addon reports as its version (see run)


def run():
    L = lua51.LuaRuntime(unpack_returned_tuples=True)
    L.execute(wowmock.MOCK)
    wowmock.install_bit(L)
    # the addon's version is masked: it goes into every payload (gv=) and so into the strip's paint, and the window
    # shows it, so each addon version bump would change this golden without any behaviour changing (R13). "golden"
    # has the length of a real version, so the pages fit as they would.
    L.execute("local get = C_AddOns.GetAddOnMetadata\n"
              "C_AddOns.GetAddOnMetadata = function(a, k)\n"
              "  if a == 'Glide' and k == 'Version' then return '%s' end\n"
              "  return get(a, k)\n"
              "end" % MASKED_VERSION)
    ns = L.table()
    toc = [ln.strip() for ln in open(os.path.join(ROOT, "addon", "Glide", "Glide.toc"), encoding="utf-8")
           if ln.strip().endswith(".lua")]
    loader = L.eval("function(src, name, a, ns) local f, e = loadstring(src, name) if not f then error(e) end "
                    "return f(a, ns) end")
    for f in toc:
        src = open(os.path.join(ROOT, "addon", "Glide", *f.split("\\")), encoding="utf-8").read()
        loader(src, "@" + f, "Glide", ns)
    G = L.globals()
    out = {"toc": toc, "frames": []}

    def frame(label):
        G.tick()
        rec = {"at": label, "payload": ns.lastPayload, "mask": ns.CoachMask() if ns.CoachMask else None}
        cells = [t for t in G.textures.values() if not getattr(t, "gauge", None)]
        rec["paint"] = hashlib.sha256(json.dumps([[t.r, t.g, t.b] for t in cells]).encode()).hexdigest()[:16]
        out["frames"].append(rec)

    G.fire("ADDON_LOADED", "Glide")
    G.fire("PLAYER_LOGIN")
    for i in range(6):
        frame("login %d" % i)
    G.fire("QUEST_ACCEPTED", 92461)
    G.fire("PLAYER_LEVEL_UP", 7)
    for i in range(6):
        frame("events %d" % i)
    for cmd in ("combat off", "voice off", "reset"):
        G.SlashCmdList.GLIDECOACH(cmd)
        frame("/coach " + cmd)
    for cmd in ("cell 4", "cell 3", "off", "on"):
        G.SlashCmdList.GLIDELINK(cmd)
        frame("/glidelink " + cmd)
    G.SlashCmdList.GLIDE("mark golden note")
    frame("mark")
    G.SlashCmdList.GLIDE("")                       # the Glide window
    P = G.GlideOptionsFrame

    def texts(f):
        got = [fs.text for fs in (f.fontstrings or L.table()).values() if fs.shown]
        for k in (f.children or L.table()).values():
            got += texts(k)
        return got
    out["window"] = {"shown": bool(P and P.shown), "texts": texts(P) if P else None,
                     "checks": [bool(k.checked) for k in (P.children or L.table()).values()
                                if k.kind == "CheckButton"] if P else None}
    db = G.GlideRecorderDB
    s = db.sessions[db.lastSession] if db and db.sessions else None
    out["session"] = {k: len(list((s[k] or L.table()).values())) for k in ("quests", "events", "errors")} if s else None
    out["link_db"] = {k: G.GlideLinkDB[k] for k in ("cell", "fps", "enabled", "cols", "rows")}
    return out


if __name__ == "__main__":                 # python probe_addon.py OUT.json
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump(run(), fh, indent=1, sort_keys=True, ensure_ascii=False, default=str)
