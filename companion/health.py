"""What the companion needs and doesn't have, for the desktop app's Status page (GET /health).

Each check is {"id", "area", "status": ok|warn|error|info, "title", "detail", "action"}; action names what fixes it:
"refresh" (re-read the game files and rebuild the databases, POST /refresh), "refresh_force" (the same, ignoring
what is up to date), or an app-side action ("addon_install", "wow_dir", "keys") the desktop app performs.
The slower checks (the knowledge base's staleness, the client's build) are cached for a minute. The answer also
carries "versions" (App.version_info: the app, addon, strip, kb.db schema and walking map versions in play).
"""
import json
import os
import time

import glideconfig
import protocol
import versions

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
SLOW_TTL = 60.0


def wow_dir(root=ROOT):
    """The WoW folder set in config.json ("wow_dir"), or None (not set, or config.json missing or unreadable)."""
    return glideconfig.wow_dir(root)


def client_version(flavor_dir):
    """The installed client's version for this flavour folder (…\\_classic_beta_), from .build.info, or None."""
    try:
        product = open(os.path.join(flavor_dir, ".flavor.info"), encoding="utf-8").read().split()[-1]
        lines = open(os.path.join(os.path.dirname(os.path.normpath(flavor_dir)), ".build.info"),
                     encoding="utf-8").read().splitlines()
    except (OSError, IndexError):
        return None
    head = [h.split("!")[0] for h in lines[0].split("|")] if lines else []
    for ln in lines[1:]:
        row = dict(zip(head, ln.split("|")))
        if row.get("Product") == product:
            return row.get("Version")
    return None


def maps_status(root=ROOT):
    """(manifest version or None, stages done) from data/gamedata/manifest.json, and the map image count."""
    try:
        with open(os.path.join(root, "data", "gamedata", "manifest.json"), encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        m = {}
    d = os.path.join(root, "data", "gamedata", "maps")
    n = len([f for f in os.listdir(d) if f.endswith(".png")]) if os.path.isdir(d) else 0
    return m.get("version"), sorted((m.get("stages") or {}).keys()), n


def _knowledge_status(root):
    """knowledge.api.status: the knowledge layer's answers about its files (raises when the package won't load)."""
    import importlib
    from knowledge_layer import knowledge_package
    api, why = knowledge_package(root)
    if api is None:
        raise RuntimeError(why)
    return importlib.import_module("knowledge.api.status")


# knowledge inputs that grow while you play (session logs, archived SavedVariables): new ones are expected, and the
# background rebuild at the next start merges them
PLAY_INPUTS = {"live logs", "recorder SV"}

def link_check(app, now=None):
    """The game link check (Status page, and coach.link_issue on the game overlay)."""
    now = time.time() if now is None else now
    sim = getattr(app.args, "sim", False)
    under_app = _under_app(app)
    frames, last = app.state.frames, getattr(app, "last_frame_t", 0)
    other = getattr(app, "strip_other", None)
    if sim:
        return {"id": "link", "area": "game", "status": "info", "title": "Simulation",
                "detail": "A simulated character is playing; nothing is read from the game.", "action": None,
                "short": None}
    if other and other["t"] >= last:
        m = versions.strip_mismatch(other["version"], protocol.PROTO, app=under_app)
        return {"id": "link", "area": "game", "status": "error", "title": m["title"],
                "detail": "%s. %s." % (_cap(m["problem"]), _cap(m["fix"])), "action": m["action"],
                "short": "Addon mismatch"}
    if not frames:
        return {"id": "link", "area": "game", "status": "error", "title": "No game data yet",
                "detail": "Start WoW in Windowed or Windowed (Fullscreen) mode with the Glide addon enabled. You should "
                          "see a strip of coloured squares in the top-left corner; in game, /glide strip on turns it on.",
                "action": None, "short": "No game data"}
    if now - last > 15:
        ago = _ago(now - last)
        return {"id": "link", "area": "game", "status": "warn", "title": "Game data stopped %s ago" % ago,
                "detail": "WoW is closed, minimised, or the data strip is covered or off (/glide strip on).",
                "action": None, "short": "Game data stopped %s ago" % ago}
    rate = int(100 * app.decoded / app.attempts) if app.attempts else 0
    if rate and rate < 60:
        return {"id": "link", "area": "game", "status": "warn",
                "title": "Game data is patchy (%d%% of reads decode)" % rate,
                "detail": "Keep the top-left corner of the game uncovered, or make the strip bigger in game with "
                          "/glide cell 4.", "action": None, "short": "Patchy data (%d%%)" % rate}
    return {"id": "link", "area": "game", "status": "ok", "title": "Reading the game",
            "detail": "%s frames so far." % "{:,}".format(frames), "action": None, "short": None}


class Health:
    def __init__(self, app, root=ROOT):
        self.app, self.root = app, root
        self._slow, self._slow_t = {}, 0.0

    def _slow_checks(self):
        """Knowledge-base staleness and the client build: cached, they read files."""
        if time.time() - self._slow_t < SLOW_TTL:
            return self._slow
        out = {}
        try:
            kstatus = _knowledge_status(self.root)
            ign = [p for p in (getattr(getattr(self.app, "log", None), "name", None),
                               getattr(getattr(self.app, "coach", None), "grounding_path", None)) if p]
            # after a background build switched over, judge the kb.db and glide.db in use (a build made against a
            # staged glide.db recorded that one, so the old data/glide.db would always look "changed")
            live = getattr(self.app, "live_db", None) or {}
            st = kstatus.kb_staleness(self.root, kb=live.get("kb"), glide=live.get("glide"), ignore=ign)
            changes = [ch for ch in st.get("changes") or () if ch.get("key")]
            # play data grows every session; the background rebuild at the next start merges it, so it's no reason
            # to call the knowledge base out of date
            play = sorted({ch["key"] for ch in changes if ch["key"] in PLAY_INPUTS})
            real = sorted({ch["key"] for ch in changes if ch["key"] not in PLAY_INPUTS})
            if st.get("stale") and changes and not real:
                out["kb"] = {"stale": False, "play": play, "built": st.get("built")}
            else:
                reason = st.get("reason") or ""
                if real:
                    reason = "%d input%s changed since it was built (%s) (%s)" % (
                        len(real), "" if len(real) == 1 else "s", st.get("built"), ", ".join(real))
                out["kb"] = {"stale": st.get("stale"), "reason": reason, "built": st.get("built"), "play": play}
        except Exception as e:  # noqa: BLE001 - a status check never takes the companion down
            out["kb"] = {"stale": None, "reason": "couldn't check: %s" % e}
        w = wow_dir(self.root)
        out["client"] = client_version(w) if w else None
        out["maps"] = maps_status(self.root)
        if not _under_app(self.app):          # the app checks RestedXP's install itself
            try:
                out["rxp"] = _knowledge_status(self.root).rxp_source(self.root)
            except Exception as e:  # noqa: BLE001
                out["rxp"] = {"kind": None, "error": str(e)}
        self._slow, self._slow_t = out, time.time()
        return out

    def invalidate(self):
        self._slow_t = 0.0

    def checks(self):
        app, c = self.app, []
        sim = getattr(app.args, "sim", False)
        # run by the desktop app, the app checks the install (addon folder, old addons, WoW folder, RestedXP) and
        # offers the fix; the engine reports only what it alone sees (the addon the game is running)
        under_app = _under_app(app)

        def add(cid, area, status, title, detail="", action=None):
            c.append({"id": cid, "area": area, "status": status, "title": title, "detail": detail, "action": action})

        # ---- the game link: the addon's data strip -------------------------------------------------------------
        frames = app.state.frames
        lk = link_check(app)
        add(lk["id"], lk["area"], lk["status"], lk["title"], lk["detail"], lk["action"])

        # ---- the addon ---------------------------------------------------------------------------------------------
        a = getattr(app.rules, "addon_status", None) or {}
        if a.get("ok") is False and under_app:
            if a.get("kind") == "running":
                add("addon", "addon", "error", "Restart WoW to load Glide %s" % (a.get("expected") or ""),
                    "%s. %s." % (_cap(a.get("problem")), _cap(a.get("fix"))))
        elif a.get("ok") is False:
            add("addon", "addon", "error", "Glide addon needs attention", "%s. %s." % (a.get("problem"), a.get("fix")),
                "addon_install")
        elif a.get("ok"):
            add("addon", "addon", "ok", "Glide addon %s" % (a.get("running") or a.get("installed") or ""), "Up to date.")
        st = app.state.summary() if frames else {}
        errs = st.get("addon_errors")                 # core er / page E (addon 0.15.3)
        if errs:
            add("addon_errors", "addon", "warn", "The addon reported %d error%s" % (
                errs["count"], "" if errs["count"] == 1 else "s"),
                ("Last: %s. " % errs["last"].rstrip(".") if errs["last"] else "") +
                "In game, /glide debug prints each one as it happens; /glide dump shows the count and the last.")
        if not sim and not under_app and not wow_dir(self.root):
            add("wow_dir", "addon", "error", "WoW folder not set",
                "Set it under Addon & game so Glide can install the addon and read your recordings and the game's map data.",
                "wow_dir")

        slow = self._slow_checks()        # sim reads the same RestedXP guides and game files, so they report too

        # ---- RestedXP ----------------------------------------------------------------------------------------------
        rxp = slow.get("rxp")
        if rxp is not None and rxp.get("kind") in (None, "missing", "none"):
            add("rxp", "route", "warn", "RestedXP not found",
                "Glide reads RestedXP's guides from Interface\\AddOns\\RXPGuides. Install RestedXP, or set the WoW folder.",
                "wow_dir")
        elif rxp is not None:
            add("rxp", "route", "ok", "RestedXP %s" % (rxp.get("version") or ""), "Guides are read from the installed addon.")
        if frames and not st.get("guide"):
            add("guide", "route", "warn", "No RestedXP guide is active",
                "Open RestedXP in game and pick a guide; the coach follows its current step.")

        # ---- knowledge base and quest database ---------------------------------------------------------------
        ks = getattr(app, "_kb_status", None) or {}
        if not ks.get("ok"):
            add("kb", "data", "error", "Knowledge base not loaded", ks.get("reason") or "", "refresh")
        else:
            kb = slow.get("kb") or {}
            if kb.get("stale"):
                add("kb", "data", "warn", "Knowledge base is out of date", "Rebuild it: %s." % kb.get("reason"), "refresh")
            elif kb.get("stale") is None and kb.get("reason"):
                add("kb", "data", "info", "Knowledge base", kb.get("reason"))
            else:
                add("kb", "data", "ok", "Knowledge base current", "Built %s; %s guides.%s" % (
                    ks.get("built") or "?", ks.get("guides") or "?",
                    " Play since then (%s) is merged at the next start." % ", ".join(kb["play"]) if kb.get("play") else ""))
        if not app.k.ok:
            add("glide_db", "data", "error", "Quest database missing", "data\\glide.db isn't there yet; rebuild it.", "refresh")

        # ---- game files: maps, zones, flight paths -----------------------------------------------------------------
        mver, stages, nmaps = slow.get("maps") or (None, [], 0)
        client = slow.get("client")
        if not mver:
            add("maps", "game_files", "warn", "Game map data not read yet",
                "Map images, exact zone borders, flight paths and walking routes come from your WoW install.", "refresh")
        elif client and client != mver:
            add("maps", "game_files", "warn", "WoW was patched since the map data was read",
                "Map data is from %s; the game is %s. Re-read the game files." % (mver, client), "refresh")
        else:
            add("maps", "game_files", "ok", "Game data from WoW %s" % mver,
                "%d map images: %s." % (nmaps, ", ".join(stages) or "no stages"), "refresh_force")
        # the buff icons' spell table is read with the map data (stage "spells"); without it buffs show letters
        import icons
        if (mver and "spells" not in stages) or icons.status.get("state") == "error":
            add("spell_icons", "game_files", "warn", "Buff icons show letters",
                "The spell table couldn't be read from WoW%s. Re-read the game files; if it still fails, the Logs "
                "page has the details." % (" " + mver if mver else ""),
                "refresh")                  # a plain re-read runs only the stages not done, so just this one

        # ---- the background rebuild ------------------------------------------------------------------------------
        r = getattr(app, "refresher", None)
        rs = getattr(r, "status", None) or {}
        if rs.get("state") not in (None, "waiting", "done"):
            add("refresh", "data", "info", "Re-reading: %s" % rs.get("state"), "Glide keeps coaching while this runs.")
        elif rs.get("state") == "done":
            failed = [k for k in ("maps", "glide", "kb") if rs.get(k) == "failed"]
            if failed:
                add("refresh", "data", "error", "The last rebuild failed (%s)" % ", ".join(failed),
                    "The Logs page has the details.", "refresh")

        # ---- the AI coach ---------------------------------------------------------------------------------------
        co = app.coach
        if not co.enabled:
            add("ai", "ai", "warn", "AI coach is off",
                "No API key or endpoint is set, so callouts are rule-based only. Add a key under Settings > AI models & keys.",
                "keys")
        elif co.suspended:
            add("ai", "ai", "error", "AI coach paused: %s" % co.suspended,
                "Fix the key or top up credits, then ask the coach anything to resume.", "keys")
        else:
            add("ai", "ai", "ok", "AI coach ready", co.describe_models())
        ptt = getattr(app, "ptt", None)
        if ptt is not None and getattr(ptt, "stt", None) is not None:
            add("ptt", "ai", "ok", "Push-to-talk: %s" % app.cfg["ptt"].get("key", "F8"), "Speech-to-text: %s." % ptt.stt.name)
        return c

    def snapshot(self):
        r = getattr(self.app, "refresher", None)
        info = getattr(self.app, "version_info", None)        # the versions in play (the state's "versions")
        return {"checks": self.checks(), "refresh": dict(getattr(r, "status", None) or {}), "t": time.time(),
                "versions": info() if info else None}


def _under_app(app):
    return bool(getattr(getattr(app, "args", None), "app", False))


def _cap(text):
    return (text or "")[:1].upper() + (text or "")[1:]


def _ago(s):
    s = int(s)
    return "%ds" % s if s < 90 else "%dm" % (s // 60) if s < 5400 else "%dh" % (s // 3600)
