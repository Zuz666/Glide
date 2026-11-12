#!/usr/bin/env python3
"""Glide companion: reads the Glide addon's strip from the WoW window, tracks state, coaches you.

  python companion\\app.py              live (Windows, WoW running with the Glide addon)
  python companion\\app.py --sim        simulated run (no game needed) to test the whole pipeline
  python companion\\app.py --calibrate  save what the capture sees to data\\live\\capture.png and exit
Options: --no-voice --no-overlay --no-ptt --port 8765
Push-to-talk: hold F8 (config "ptt") while WoW is focused to ask the coach out loud.
Coach: set ANTHROPIC_API_KEY, GEMINI_API_KEY or OPENROUTER_API_KEY to enable the AI coach; without one, offline callouts.
The models picked in the dashboard are saved in config.json (companion.llm) and used again at the next start.
Knowledge base: data\\kb.db (python knowledge\\build_kb.py) gives the coach its knowledge block, tools, the
turnin_decay_soon trigger and the "Why this step" dashboard panel; without it the coach runs on glide.db as before.
RestedXP settings for the knowledge layer: config.json "companion": {"rxp": {"ssf": true, "group_quests": true}}.
Feedback: "/glide mark [note]" (or the Glide key binding) saves what the coach knew to data\\live\\flags.jsonl.
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback

if sys.version_info < (3, 12):      # checked before the engine's own imports, which use 3.12 (the bundled version)
    sys.exit("Glide's engine needs Python 3.12 or newer, this is Python %s. Install it with:\n"
             "    winget install -e --id Python.Python.3.12" % sys.version.split()[0])

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
if ROOT not in sys.path:
    sys.path.append(ROOT)       # the knowledge package

import calibration  # noqa: E402
import protocol  # noqa: E402
from coach import Coach, CoachDeps  # noqa: E402
from dashboard import Hub, Route, announce_route, ask_route, get_json, post_json, serve  # noqa: E402
from feedback import Feedback  # noqa: E402
import glideconfig  # noqa: E402
from log import log, log_once  # noqa: E402
import log as console  # noqa: E402
import msg_kinds  # noqa: E402
from glidedb import GlideDB  # noqa: E402
from knowledge_layer import KB_PATH, handle_view  # noqa: E402
import rules as rules_mod  # noqa: E402
from rules import Rules  # noqa: E402
import settings  # noqa: E402
from state import GameState  # noqa: E402
from voice import Voice  # noqa: E402

DEFAULTS = settings.defaults()      # every engine setting's default: companion/settings_manifest.json
MSG_HISTORY, TRIGGER_HISTORY = 10, 20
LIVE_DIR = os.path.join(ROOT, "data", "live")      # this install's logs, layout, reminders, watched buffs...


def live_path(name):
    """A file in data/live."""
    return os.path.join(LIVE_DIR, name)


def console_log(line):
    """A line for the engine's console (the desktop app shows it in its log, and keeps it on disk)."""
    console.line(line)


LOOP_ERROR_PAUSE = 1.0          # seconds a background loop waits after an error, so a lasting fault doesn't spin
_loop_errors = set()


def loop_error(where, e):
    """A background loop (the game link, the dashboard feed) hit an error: log it once per distinct error with
    where it came from, pause briefly, and let the loop carry on instead of ending for the rest of the session."""
    key = (where, type(e).__name__, str(e)[:200])
    if key not in _loop_errors and len(_loop_errors) < 100:
        _loop_errors.add(key)
        where_from = "".join(traceback.format_list(traceback.extract_tb(e.__traceback__)[-3:])).rstrip()
        console_log("[%s] error (still running): %s: %s\n%s" % (where, type(e).__name__, e, where_from))
    time.sleep(LOOP_ERROR_PAUSE)


def kb_status_line(pack, reason):
    """The startup line's knowledge-base part."""
    if pack is None:
        if not reason or "missing" in reason:
            return "knowledge base missing: run build-kb.cmd"
        return "knowledge base not loaded: %s" % reason
    st = pack.status()
    n = lambda v: "{:,}".format(v) if isinstance(v, int) else "?"  # noqa: E731
    return "knowledge base loaded (data/kb.db built %s: %s guides, %s step briefs, %s heuristics) -> knowledge " \
           "block, %d tools, why panel" % (st.get("built") or "?", n(st.get("guides")), n(st.get("briefs")),
                                          n(st.get("heuristics")), len(pack.tool_definitions()))


def split_briefings(triggers, cfg):
    """(quests to brief, triggers left for the coach). With quest_briefings on (the default), accepted quests get
    their own briefing (objective + explanation) instead of a one-line callout."""
    if not triggers or not settings.Settings("llm", cfg).get("quest_briefings"):
        return [], triggers
    return ([t.facts.get("arg") for t in triggers if t.kind == "quest_accepted"],
            [t for t in triggers if t.kind != "quest_accepted"])


def load_config():
    """The engine's settings from config.json (glideconfig.load): a typo in it shouldn't stop the coach, so an
    unreadable file means the defaults and a log line saying why."""
    return glideconfig.load(ROOT, log=console_log)


BARGE_S = 0.15          # push-to-talk held this long is talking over the coach (a brush of the key isn't)
PTT_STUCK_S = 120       # ... held longer than this: taken as released (a lost key-up never leaves the coach mute)


def ptt_talking(app):
    """Whether the player holds push-to-talk now (App.on_ptt_state); a hold past PTT_STUCK_S counts as released."""
    if not getattr(app, "_ptt_down", False):
        return False
    if time.time() - getattr(app, "_ptt_down_t", 0.0) > PTT_STUCK_S:
        App._ptt_release(app)
        return False
    return True

class App:
    """The running companion: the strip comes in (capture_loop, or sim_loop), on_frame updates the game state and
    runs the rules, the coach answers on its own thread, and the dashboard gets the state every second."""

    # the knowledge layer's parts, read from the one handle (self.kb_handle, the coach's)
    pack, kb_reason = handle_view("pack"), handle_view("reason")

    def __init__(self, args, cfg):
        self.args, self.cfg = args, cfg
        self.state = GameState()
        self.rules = Rules(cfg["rules"])
        if not args.sim:
            # databases rebuilt in the background during the last session become the current ones now
            import kb_refresh
            kb_refresh.promote(os.path.join(ROOT, "data"), log=console_log)
        self.k = GlideDB(os.path.join(ROOT, "data", "glide.db"))
        self.live_db = {}                    # glide / kb paths in use after a background build switched them
        import xph_history
        self.xph = xph_history.XphHistory(None if args.sim else live_path("xph_history.json"))
        self.hub = Hub()
        os.makedirs(LIVE_DIR, exist_ok=True)
        self.voice = self._make_voice()
        self.overlay = None
        # knowledge layer (data/kb.db): the coach's knowledge block and tools, the why panel, turnin_decay_soon.
        # Missing or unloadable: everything runs as before (no block, no tools, no KB trigger).
        self.coach = Coach(cfg["llm"], self.k, self.on_message, kb=KB_PATH, rxp=cfg.get("rxp"),
                           deps=self._coach_deps(cfg["llm"]))
        # the current pack, its api and why there is none: one holder for the app, the coach and the rules,
        # switched by coach.swap_kb after a background rebuild
        self.kb_handle = self.coach.kb_handle
        self.voice.style_fn = self.coach.persona_voice     # the app's narrator performs the coach's persona
        if not args.sim:
            self.catalog.refresh_if_old()
        self.rules.kb_fn = self.kb_handle       # -> the current knowledge.api.KnowledgePack or None
        self._kb_status = self.pack.status() if self.pack is not None else {"ok": False, "reason": self.kb_reason}
        # the community feed (community_feed.py): notes published after this app was built, written into kb.db; a
        # simulated run leaves it out (nothing fetched, nothing written)
        self.feed = None
        feed_opt = settings.Settings("community", cfg.get("community") or {})
        if not args.sim and feed_opt.get("feed"):
            import community_feed
            self.feed = community_feed.Feed(lambda: self.live_db.get("kb") or KB_PATH, LIVE_DIR, feed_opt.get("feed_url"),
                                            lambda: feed_opt.get("feed_minutes"), on_landed=self._feed_landed)
            self._apply_feed()
        # buff watch list (from "remind me about X"), kept across restarts
        self.watch_file = live_path("watched_buffs.json")
        try:
            self.rules.watched = set(json.load(open(self.watch_file, encoding="utf-8")))
        except FileNotFoundError:
            pass                                # none saved yet
        except Exception as e:  # noqa: BLE001 - a damaged list: start with none
            log_once("buffs.load", e)
        from gear import Gear
        self.rules.gear = Gear(cfg["gear"])
        import versions
        self.versions = versions
        self.addon_expected = versions.expected()
        self._installed = (0, None)
        self.decoded = self.attempts = 0
        self.last_frame_t = 0.0                  # when the last new frame arrived (the Status page's game link)
        # clean frames in another strip version (an addon newer or older than this engine): {"version", "frames",
        # "t"} or None; the Status page names the mismatch while they are newer than the last good frame
        self.strip_other = None
        self.app_version = os.environ.get("GLIDE_APP_VERSION") or None    # the desktop app that runs this engine
        self.refresher = None
        import health
        self.health = health.Health(self)
        self.last_seq = None
        self._last_pages = {}
        self._flags = None                      # coach switches from the addon (/coach)
        # player feedback marks ("/glide mark"): what the coach said and saw lately goes with each flag
        self.feedback = Feedback(ROOT)
        self.msg_history = []                   # last 10 coach messages (callout/answer/review/info)
        self.trigger_history = []               # last 20 (kind, facts) triggers handed to the coach
        self._hist_lock = threading.Lock()      # on_message runs on the coach, ptt and capture threads
        self._ptt_down = False                  # push-to-talk held: the player is talking (on_ptt_state)
        self._ptt_down_t = 0.0
        self._held = []                         # answers that came while it was held, said when it comes up
        self._barge_timer = None
        self._ring_primed = False               # set by the first decoded frame (its ring events are old)
        self.log = open(live_path(time.strftime("%Y%m%d-%H%M%S") + ".jsonl"), "a", encoding="utf-8")
        self.last_log = 0
        self.voice.announce(self.coach.localize_text)
        self.coach.start()

    def _make_voice(self):
        """The narrator: the desktop app's (it speaks the "speak" events) or the engine's own speech process."""
        args, cfg = self.args, self.cfg
        vcfg = {**cfg["voice"], "enabled": cfg["voice"]["enabled"] and not args.no_voice}
        if args.app and cfg["voice"].get("output", "app") == "app":
            from voice import HubVoice
            return HubVoice(vcfg, self.hub, announce=False)      # the desktop app speaks ("speak" events)
        return Voice(vcfg, log_dir=LIVE_DIR, announce=False,
                     on_spoken=lambda data: self.hub.publish("voice_spoken", data))

    def _coach_deps(self, llm_cfg):
        """What the coach uses from the app, handed over when it is built: the model catalog, reminders, the player
        memory, the usage log, and the getters and callbacks for gear, buffs, addon status, early speech and voice
        switches."""
        import llm_client
        import memory
        import models
        import reminders
        keys, endpoints = llm_client.api_keys(llm_cfg)
        self.catalog = models.Catalog(live_path("models.json"), keys.get("anthropic"),
                                      keys.get("openrouter"), gemini_key=keys.get("gemini"), endpoints=endpoints)
        # what the coach learns about the player, kept outside the install and the engine home (memory.memory_dir);
        # a simulated run gets a throwaway memory so test sessions never teach the real one
        self.memory = memory.Memory(None if self.args.sim else memory.default_path())
        return CoachDeps(
            catalog=self.catalog,
            language=settings.Settings("voice", self.cfg.get("voice", {})).get("language"),
            usage_fn=lambda u: self.write_log({"api": u}),           # per-call tokens: cache hit rate
            on_toggle=lambda k, v: self.on_settings({k: v}),         # "quest briefings off" by voice
            reminders=reminders.Reminders(None if self.args.sim else live_path("reminders.json")),
            memory=self.memory,
            on_watch=self.set_watch,
            gear_fn=lambda: (self.rules.upgrades, self.rules.rewards, self.state.gear["W"], self.rules.bag_items),
            system_fn=lambda: self.rules.addon_status,
            speak_fn=self.speak_early,
            watched_fn=lambda: self.rules.watched,
            on_thinking=lambda role: self.hub.publish("thinking", {"role": role}))

    # ------------------------------------------------------------------------------------------
    def on_message(self, m):
        coach = getattr(self, "coach", None)
        if coach is not None:
            coach.localization.deliver(m, self._deliver_message)
        else:
            self._deliver_message(m)

    def _deliver_message(self, m):
        m.setdefault("t", time.time())
        if m.get("kind") in msg_kinds.HISTORY:
            with self._hist_lock:
                self.msg_history.append(m)
                del self.msg_history[:-MSG_HISTORY]
        self.hub.publish("message", m)
        self.write_log({"msg": m})
        log(m.get("kind"), m.get("text"), m.get("attribution"))
        if m.get("speak") and m.get("kind") != "answer" and self._flags and not self._flags.get("voice", True):
            m = dict(m, speak=False)                 # /coach voice off: text only (answers to questions still speak)
        if m.get("speak") and ptt_talking(self):
            # the player is talking (push-to-talk held): an answer waits for the key to come up, the rest is dropped
            if m.get("kind") == "answer":
                with self._hist_lock:
                    self._held.append(m)
        elif m.get("speak"):
            App._say(self, m)
        if self.overlay and m.get("kind") in msg_kinds.COACH_SAID:
            self.overlay.set_message(m["text"], "#ff8a95" if m.get("urgent") else "#e6e9ef")

    def _say(self, m):
        """Say a message: all of it, or only the rest when its start was said while it streamed (speak_early). The
        tone goes as its own field with the first sentence only (ElevenLabs v3 acts it as a tag: once a line)."""
        tone = str(m.get("tone") or "")
        if m.get("spoken"):
            said, full = " ".join(m["spoken"].split()), " ".join(m["text"].split())
            rest = full[len(said):].strip() if full.startswith(said) else full
            if rest:
                self.voice.say(rest, interrupt=False, tone=tone, first=False)
        else:
            self.voice.say(m["text"], interrupt=m.get("urgent", False), tone=tone)

    def on_ptt_state(self, st):
        """Push-to-talk as it happens ("listening" | "transcribing" | "idle"): the "ptt" event, and talking over the
        coach stops it, like on a call: held BARGE_S (a brush of the key is not talking), the line being said stops
        (the app's narrator on the event; the engine's own speech process here) and so does the reply on its way
        (Coach.barge); while the key is down an answer waits and the rest is dropped; key up says what waited. "idle"
        changes nothing (the previous press's can come in the middle of a new one)."""
        self.hub.publish("ptt", {"state": st})
        if st == "listening":
            self._ptt_down, self._ptt_down_t = True, time.time()
            if self._barge_timer is not None:
                self._barge_timer.cancel()
            self._barge_timer = threading.Timer(BARGE_S, self._barge)
            self._barge_timer.daemon = True
            self._barge_timer.start()
        elif st == "transcribing" or not getattr(getattr(self, "ptt", None), "key_down", False):
            self._ptt_release()       # key up ("idle" too: a tap too short, a failed recording; not a stale one)

    def _ptt_release(self):
        """The key came up: what waited is said."""
        self._ptt_down = False
        if self._barge_timer is not None:
            self._barge_timer.cancel()
        with self._hist_lock:
            held, self._held = self._held, []
        for m in held:
            self._say(m)

    def _barge(self):
        """The key is still down after BARGE_S: the player is talking over the coach."""
        if not self._ptt_down:
            return
        self.voice.hush()
        coach = getattr(self, "coach", None)
        if coach is not None:
            coach.barge()

    def speak_early(self, text, kind, urgent=False, tone="", first=True):
        """The coach's streamed replies: say one finished sentence now (same voice switch rule as on_message).
        -> False when it isn't said (push-to-talk held: the reply then keeps it for on_message)."""
        if ptt_talking(self):
            return False
        if kind != "answer" and self._flags and not self._flags.get("voice", True):
            return None
        self.voice.say(text, interrupt=urgent, tone=tone or "", first=first)
        return True

    def set_watch(self, name, on=True):
        self.rules.watch(name, on)
        try:
            with open(self.watch_file, "w", encoding="utf-8") as fh:
                json.dump(sorted(self.rules.watched), fh)
        except Exception as e:  # noqa: BLE001 - kept for this session anyway
            log_once("buffs.save", e)

    def on_mark(self, note):
        """'/glide mark [note]' or the key binding: flag the moment (data/live/flags.jsonl) for review."""
        note = (note or "").strip()
        with self._hist_lock:
            recent = self.msg_history[-5:]
        rec = self.feedback.on_mark(note, self.state.summary(), recent, self.trigger_history[-TRIGGER_HISTORY:])
        last = next((m.get("text") for m in reversed(recent) if m.get("kind") in msg_kinds.RECENT), "")
        self.coach.note_mark(note, last)
        if rec is None:
            self.on_message({"kind": "error", "text": "Could not save the mark (data/live/flags.jsonl)",
                             "speak": False})
            return
        self.on_message({"kind": "info", "text": "Marked for review: %s" % (note or "last callout"),
                         "speak": False})

    def write_log(self, obj):
        obj["t"] = time.time()
        self.log.write(json.dumps(obj, default=str) + "\n")
        self.log.flush()

    def on_frame(self, seq, payload):
        """One decoded strip frame: update the game state, run the rules, and hand the triggers the player hasn't
        muted to the coach (accepted quests to the briefings)."""
        if seq == self.last_seq:
            return
        self.last_seq = seq
        self.last_frame_t = time.time()
        core, page = protocol.parse_payload(payload)
        pid = page.get("pg")
        if pid and self._last_pages.get(pid) != page:        # log each page when its content changes
            self._last_pages[pid] = page
            self.write_log({"page": page})
        ring = self.state.update(core, page)
        if not self._ring_primed:
            # The addon's ring keeps its last 3 events forever, so the first frame after the engine starts
            # replays old ones (state.update has recorded their ids): don't coach or mark on them. An addon
            # restart later (UI reload) is told apart in state.update: those rings are new events.
            self._ring_primed = True
            if ring:
                self.on_message({"kind": "info", "text": "ignored %d stale ring events on connect" % len(ring),
                                 "speak": False})
            ring = []
        triggers = self.rules.check(self.state, ring)
        flags = self._update_switches(self.state.coach_flags())
        if triggers:
            muted = [t for t in triggers if not rules_mod.allowed(t.kind, flags)]
            triggers = [t for t in triggers if rules_mod.allowed(t.kind, flags)]
            self.write_log({"triggers": [(t.kind, t.facts) for t in triggers],
                            "muted": [t.kind for t in muted]})
            self.trigger_history = (self.trigger_history + [(t.kind, t.facts) for t in triggers])[-TRIGGER_HISTORY:]
        for _id, kind, arg in ring:
            if kind == "mark":
                self.on_mark(arg)
        briefs, triggers = split_briefings(triggers, self.coach.cfg)
        for quest in briefs:
            self.coach.brief(quest)
        self.coach.push(triggers, self.state)
        if time.time() - self.last_log >= 1:
            self.last_log = time.time()
            self.write_log({"core": core})

    def _update_switches(self, flags):
        """The addon's coach switches (/coach) for this frame; a change is shown. -> flags."""
        if flags != self._flags:
            if self._flags is not None:
                changed = [("%s %s" % (k, "on" if v else "off")) for k, v in flags.items() if self._flags.get(k) != v]
                self.on_message({"kind": "info", "text": "Coach switches: " + ", ".join(changed), "speak": False})
            self._flags = flags
        return flags

    def on_models(self, refresh=False, reasoning_model=None):
        """Dashboard model selector: the catalog (Anthropic + OpenRouter), the current choices and the token profile
        used for the $/hour estimates."""
        import tuning
        if refresh:
            self.catalog.refresh()
        import openai_capabilities
        import openai_compat
        import reasoning
        llm = self.coach.cfg
        opt = settings.Settings("llm", llm)
        if hasattr(self.catalog, "endpoints") and "endpoints" in llm:
            prev_eps = self.catalog.endpoints or {}
            self.catalog.endpoints = {
                eid: {**ep, "key": (prev_eps.get(eid) or {}).get("key", "")}
                for eid, ep in openai_compat.endpoints(llm).items()
            }
        if reasoning_model is not None:
            return {"model": reasoning_model,
                    "reasoning": reasoning.describe(
                        reasoning_model, llm, openai_capabilities.catalog_raw_entry(self.catalog, reasoning_model))}
        return {"status": self.catalog.status(), "models": self.catalog.entries(),
                "selected": {path + suffix: opt.get(path + suffix)
                             for path in ("fast", "deep") for suffix in ("_model", "_reasoning_effort")},
                "selected_prices": {path: self.catalog.price(opt.get(path + "_model"), llm) for path in ("fast", "deep")},
                "reasoning": {path: reasoning.describe(
                    opt.get(path + "_model"), llm,
                    openai_capabilities.catalog_raw_entry(self.catalog, opt.get(path + "_model")))
                    for path in ("fast", "deep")},
                "profile": tuning.profile(self.coach), "spent_usd": round(self.coach.stats.get("cost_usd", 0.0), 4)}

    def on_new_knowledge(self, glide_path, kb_path):
        """The background refresh finished (kb_refresh.Refresher): use the new glide.db / kb.db from now on."""
        t0 = time.time()
        if glide_path:
            self.k.reopen(glide_path)
            self.live_db["glide"] = glide_path
        why = self.coach.swap_kb(kb_path) if kb_path else None
        if kb_path and why is None:
            self.live_db["kb"] = kb_path
            self.health.invalidate()
            self._kb_status = self.pack.status()
            self._apply_feed()                     # the rebuilt kb.db has only the notes this install shipped with
        st = getattr(getattr(self, "refresher", None), "status", {}) or {}
        self.on_message({"kind": "info", "speak": False, "text": (
            "Knowledge refreshed in the background (%ss): %s%s" % (
                st.get("seconds", "?"), ", ".join(x for x in (
                    "glide.db %s" % st.get("glide") if st.get("glide") else None,
                    "kb.db %s" % st.get("kb") if st.get("kb") else None) if x),
                "" if why is None else "; kept the old knowledge base: %s" % why)
            + (" - switched over in %.1fs." % (time.time() - t0) if why is None else ""))})

    def _continent(self):
        """The continent (map id) the player is on, for building its walking map first."""
        try:
            return self.pack.continent_of(self.state.summary().get("map")) if self.pack is not None else None
        except Exception as e:  # noqa: BLE001
            log_once("app.continent", e)
            return None

    def nav_rebuild(self):
        """Settings > General > Walking map: rebuild it from the game files now, in the background."""
        import kb_refresh
        if self.args.sim:
            return {"state": "done", "error": "simulation mode reads nothing from the game"}
        if getattr(self, "navbuild", None) is None:
            self.navbuild = kb_refresh.NavBuilder(ROOT, log=console_log, first_map=self._continent)
        st = self.navbuild.start(force=True)
        if not st.get("already"):
            self.on_message({"kind": "info", "speak": False, "text": "Rebuilding the walking map from the game files "
                             "in the background; the old one is used until it's done."})
        return st

    def refresh_now(self, force=False):
        """Re-read the game files and rebuild glide.db / kb.db now (the desktop app's Status page). Returns the
        refresher status; a refresh already running is left to finish."""
        r = self.refresher
        if r is not None and r.status.get("state") not in ("waiting", "done"):
            return dict(r.status, already=True)
        if self.args.sim:
            return {"state": "done", "error": "simulation mode reads nothing new from the game"}
        self.on_message({"kind": "info", "speak": False, "text": "Re-reading the game files and rebuilding the "
                         "knowledge%s; coaching continues meanwhile." % (" from scratch" if force else "")})
        self._start_refresher(delay_s=0.5, force=force)
        self.health.invalidate()
        return dict(self.refresher.status)

    def _start_refresher(self, **options):
        """Re-read the game files and rebuild glide.db / kb.db in the background (kb_refresh.Refresher, options
        passed on); on_new_knowledge switches over when it's done. The session's own logs are not inputs."""
        import kb_refresh
        self.refresher = kb_refresh.Refresher(ROOT, on_ready=self.on_new_knowledge, log=console_log,
                                              ignore=[self.log.name, self.coach.grounding_path], live=self.live_db,
                                              **options).start()

    def on_memory(self, body=None):
        """The app's Memory page. GET (body None): the notes, and the characters the coach played ("characters";
        "playing": the one played, by name, or ""). POST {"op": "add"|"update"|"remove"|"learning"|"reflect"|
        "character"|"character_forget"}."""
        m = self.memory
        if body:
            op = body.get("op")
            if op == "add":
                n, _ = m.add(body.get("text"), body.get("kind"), source="player", pinned=bool(body.get("pinned")))
                if n:
                    self.on_message({"kind": "info", "text": "Remembered: %s" % n["text"], "speak": False})
            elif op == "update":
                m.update(body.get("id"), text=body.get("text"), kind=body.get("kind"), pinned=body.get("pinned"))
            elif op == "remove":
                m.remove(int(body.get("id")) if str(body.get("id", "")).isdigit() else body.get("id"))
            elif op == "learning":
                m.set_learning(bool(body.get("on")))
            elif op == "reflect":
                threading.Thread(target=self.coach.reflect, daemon=True).start()
            elif op == "character":
                self.coach.character_edit(str(body.get("name") or ""), str(body.get("field") or ""),
                                          body.get("text"), body.get("index"), body.get("was"))
            elif op == "character_forget":
                self.coach.character_forget(str(body.get("name") or ""))
            else:
                raise ValueError("unknown op %r" % op)
        return dict(m.snapshot(), characters=m.characters_view(), playing=self.coach.main_name())

    def on_reminder(self, body):
        """Dashboard Reminders panel: {"done": id} closes one."""
        rem = self.coach.deps.reminders
        if rem is None or body.get("done") is None:
            return {"closed": []}
        gone = rem.remove(str(body["done"]))
        for x in gone:
            self.on_message({"kind": "info", "text": "Reminder closed: %s" % x["what"], "speak": False})
        return {"closed": [x["what"] for x in gone]}

    def on_settings(self, changes):
        """POST /settings (the app's Settings and model picker, voice toggles): apply to the live coach and save in
        config.json. {"save": patch} (the desktop app's Settings, while the engine runs) also saves that patch of the
        whole file, as the app's config:set would (None removes a key); it applies at the next coach start unless it
        is sent live too."""
        import tuning
        from llm_client import log_settings
        path = None if self.args.sim else os.path.join(ROOT, "config.json")
        before = dict(self.coach.cfg)
        changes = dict(changes)
        save = changes.pop("save", None)
        if save is not None:
            if not isinstance(save, dict):
                raise ValueError("save takes an object (a patch of config.json)")
            def saved_llm(config):
                if not isinstance(config, dict):
                    return {}
                companion = config.get("companion")
                llm = companion.get("llm") if isinstance(companion, dict) else None
                return llm if isinstance(llm, dict) else {}
            # Validate live changes before writing either half of a combined Settings save.
            # Commit once, then publish the new running values. A failed write changes neither.
            applied = tuning.apply(dict(self.coach.cfg), changes, catalog=self.catalog)
            patch = dict(save)
            if applied:
                companion = patch.get("companion")
                companion = dict(companion) if isinstance(companion, dict) else {}
                llm = dict(saved_llm(patch))
                for key, value in applied.items():
                    # A reset removes the persisted override but applies its manifest default now.
                    if key not in llm or llm[key] is not None or value != settings.default("llm." + key):
                        llm[key] = value
                companion["llm"] = llm
                patch["companion"] = companion
            saved_before = saved_llm(glideconfig.read(ROOT)[0])
            patch_llm = saved_llm(patch)
            prospective_saved = {**self.coach.cfg, **saved_before,
                                 **{k: v for k, v in patch_llm.items() if v is not None}}
            for mk in tuning.MODEL_KEYS:
                if mk in patch_llm and patch_llm[mk] is not None:
                    tuning.validate_model_choice(patch_llm[mk], prospective_saved, catalog=self.catalog)
            saved = glideconfig.patch((), patch, ROOT)  # the app's saves also land in sim
            saved_after = saved_llm(saved)
            self.coach.cfg.update(applied)
            v_patch = (patch.get("companion") or {}).get("voice")
            if isinstance(v_patch, dict):
                v_live = {k: v_patch[k] for k in ("rate", "volume") if k in v_patch and v_patch[k] is not None}
                if v_live:
                    if hasattr(self, "cfg") and isinstance(self.cfg, dict):
                        self.cfg.setdefault("voice", {}).update(v_live)
                    voice_inst = getattr(self, "voice", None)
                    if voice_inst is not None and hasattr(voice_inst, "cfg") and isinstance(voice_inst.cfg, dict):
                        voice_inst.cfg.update(v_live)
            log_settings(saved_before, saved_after, "pending restart", self.catalog)
        else:
            applied = tuning.apply(self.coach.cfg, changes, path, catalog=self.catalog)
        switched = {k: (before.get(k), applied[k]) for k in tuning.MODEL_KEYS
                    if k in applied and applied[k] != before.get(k)}
        if switched:
            self.coach.announce_models(switched)
        log_settings(before, self.coach.cfg, "saved and applied" if path else "applied; not saved", self.catalog)
        return dict(applied, saved=save is not None or bool(applied and path))

    def _apply_feed(self):
        """The kept community feed's notes into the kb.db in use (at start, and after a rebuild swapped it in)."""
        if self.feed is None:
            return
        try:
            self.feed.apply()
        except Exception as e:  # noqa: BLE001 - the notes this install shipped with still work
            log_once("app.feed_apply", e)
        if self.pack is not None:
            self.pack._comm_cache = None           # the Community panel shows them now, not in a minute

    def _feed_landed(self, count, titles):
        """New community notes arrived: the panel shows them at once, and the chat says so (the coach screen
        celebrates them from the state's community_feed)."""
        if self.pack is not None:
            self.pack._comm_cache = None
        newest = next((t for t in titles if t), None)
        self.on_message({"kind": "info", "speak": False, "text": "%d new community note%s%s" % (
            count, "" if count == 1 else "s", ": %s" % newest if newest else "")})

    def publish_state(self):
        """The dashboard's state, once a second (the "state" event), and the overlay's step line. The community notes
        (half the state, and they rarely change) go on their own "community" event, only when they change; a new
        client's init and /api/state have them in the state (Hub.SIDE_KINDS)."""
        while True:
            try:
                s = self.dashboard_state()
                self.xph.add(s.get("xp_per_hour"))             # the dashboard chart's history (GET /xph)
                if "community" in s:
                    community = s.pop("community")
                    if community != getattr(self, "_community_sent", ()):
                        self.hub.publish("community", community)
                        self._community_sent = community
                self.hub.publish("state", s)
                if self.overlay and s["overlay_step"]:
                    self.overlay.set_step(s["overlay_step"])
            except Exception as e:  # noqa: BLE001
                loop_error("state", e)
            time.sleep(1)

    def dashboard_state(self):
        """GameState.summary() with what the dashboard and the desktop app show around it: names and places, the
        addon status, gear, the coach's models and stats, the knowledge panel, the refresh progress."""
        s = self.state.summary()
        me = self.state.player_geo()
        self._describe_places(s, me)
        s["addon"] = self.rules.addon_status = self._addon_status(s)
        s["upgrades"] = self.rules.upgrades[:6]
        try:
            s["community"] = self.pack.community_recent(6) if self.pack is not None else None
        except Exception as e:  # noqa: BLE001
            log_once("app.community", e)
            s["community"] = None
        s["community_feed"] = self.feed.state() if self.feed is not None else None
        s["walk"] = self.coach.route_view(s)
        s["facing"] = (me or {}).get("facing")
        s["map_image"] = os.path.exists(os.path.join(ROOT, "data", "gamedata", "maps", "%s.png" % s.get("map")))
        rem = self.coach.deps.reminders
        if rem is not None:
            import reminders as reminders_mod
            s["reminders"] = [{"id": x["id"], "what": x["what"], "when": reminders_mod.describe_when(x),
                               "every_min": x["every_min"]} for x in rem.list()]
        try:
            # no UI reads it today (the dashboard's settings panel went), so it is kept cheap: recomputed only when
            # what it reads changes (contract 3 keeps the key)
            if getattr(self, "_tuning", None) is None:
                import tuning
                self._tuning = tuning.SnapshotCache()
            s["tuning"] = self._tuning.get(self.coach,
                                           settings.Settings("rules", self.cfg["rules"]).get("review_minutes"))
        except Exception as e:  # noqa: BLE001 - an extra
            log_once("app.tuning", e)
            s["tuning"] = None
        s["bag_items"] = self.rules.bag_items[:12]
        s["talent_plan"] = self.coach.talent_pick(s)              # RestedXP's build: the next pick
        s["level_history"] = self.coach.level_history(s, 8)       # RestedXP's levelling tracker (from disk)
        s.pop("talents", None)                                    # the raw list: the dashboard doesn't need it
        s["rewards"] = self.rules.rewards
        w = self.rules.gear.weights(s.get("class"), s.get("level")) if s.get("class") else None
        s["weights_profile"] = w["title"] if w else None
        s["frames"] = self.state.frames
        s["versions"] = self.version_info()
        s["decode_rate"] = int(100 * self.decoded / self.attempts) if self.attempts else 0
        r = self.refresher
        s["refresh"] = dict(r.status) if r is not None else None      # the app's footer shows its progress
        nb = getattr(self, "navbuild", None)
        s["navbuild"] = dict(nb.status, needed=None) if nb is not None else None
        s["models"] = self._model_status()
        import health as health_mod
        lk = health_mod.link_check(self)
        s["coach"] = {**self.coach.stats, "mode": ("offline: " + self.coach.suspended) if self.coach.suspended
                      else ("claude" if self.coach.enabled else "offline"),
                      "thinking": self.coach.thinking, "link_issue": lk["short"],
                      "link_status": lk["status"] if lk["short"] else None}
        s["kb"] = self._kb_status
        if self.pack is not None:
            s["why"], s["pace"] = self._why_panel(s)
        s["overlay_step"] = self.step_line(s)       # the desktop app's overlay shows it
        return s

    def _describe_places(self, s, me):
        """Name the step elements' quests and describe their waypoints, the player's area and the current
        waypoint, and name the quest log's quests."""
        geo = self.coach.geo
        for step in s["steps"]:
            for e in (step or {}).get("elements", []):
                if e.get("quest"):
                    e["name"] = self._quest_basics(e["quest"]).get("name")
                if e.get("tag") == "goto":
                    e["where"] = geo.describe(e.get("map") or s["map"], e["x"], e["y"], player=me,
                                              faction=s.get("faction")).get("text")
        if me:
            s["standing_in"] = geo.area_at(me["map"], me["x"], me["y"])[0]
        wp = self.state.goto_target()
        s["waypoint"] = geo.describe(wp.get("map") or s["map"], wp["x"], wp["y"], player=me,
                                     faction=s.get("faction")) if wp else None
        ql = {}
        for qid, q in (s["quests"] or {}).items():
            info = self._quest_basics(qid)
            ql[qid] = {**q, "name": info.get("name"), "level": info.get("level")}
        s["quests"] = ql

    def _quest_basics(self, qid):
        """{"name", "level"} of a quest: from the knowledge pack, glide.db without one."""
        pack = self.pack
        if pack is not None:
            return pack.quest_basics(qid)
        return self.k.quest(qid, detail=False) or {}

    def _addon_status(self, s):
        """The Glide addon the game runs vs the one this engine expects and the installed copies (versions.py;
        the folders are read again every 10 s)."""
        if time.time() - self._installed[0] > 10:
            self._installed = (time.time(), None if self.args.sim else self.versions.installed())
        running = s.get("addon_version") or (self.addon_expected if self.args.sim else None)
        return self.versions.status(running, self.state.frames, self.addon_expected, self._installed[1],
                                    app=getattr(self.args, "app", False))

    def note_other_version(self, version):
        """A clean frame in another strip version (protocol.OtherVersion): count it for the Status page."""
        o = self.strip_other if self.strip_other and self.strip_other["version"] == version else \
            {"version": version, "frames": 0}
        self.strip_other = dict(o, frames=o["frames"] + 1, t=time.time())

    def version_info(self):
        """The versions in play (the state's and /health's "versions"): the desktop app that runs this engine (None
        run on its own), the addon expected, installed and running, the strip version this engine reads and the
        one the addon last sent, and the kb.db schema and walking map builder this engine expects."""
        if not hasattr(self, "_built_versions"):
            try:
                from knowledge import store
                kb_schema = store.SCHEMA_VERSION
            except Exception:  # noqa: BLE001 - the knowledge package is optional
                kb_schema = None
            try:
                from wowfiles import navformat
                nav = navformat.NAV_VERSION
            except Exception:  # noqa: BLE001 - so is wowfiles
                nav = None
            self._built_versions = (kb_schema, nav)
        o = self.strip_other
        seen = o["version"] if o and o["t"] >= self.last_frame_t else (protocol.PROTO if self.state.frames else None)
        return {"app": self.app_version, "addon_expected": self.addon_expected,
                "addon_installed": self._installed[1], "addon_running": self.state.core.get("gv"),
                "strip": protocol.PROTO, "strip_seen": seen,
                "kb_schema": self._built_versions[0], "nav": self._built_versions[1]}

    def _model_status(self):
        """The models each coach path uses (ids, names, reasoning and PTT routing) and speech-to-text."""
        llm = self.coach.cfg
        opt = getattr(self.coach, "opt", None) or settings.Settings("llm", llm)
        models = {"fast": llm.get("fast_model"), "deep": llm.get("deep_model"),
                  "fast_name": self.coach.model_name(llm.get("fast_model")),
                  "deep_name": self.coach.model_name(llm.get("deep_model")),
                  "fast_reasoning": opt.get("fast_reasoning_effort"),
                  "deep_reasoning": opt.get("deep_reasoning_effort"),
                  "voice_question": "deep" if opt.get("voice_question_model") == "deep" else "fast"}
        language = self.coach.localization.language
        fast = llm.get("fast_model")
        models["translation"] = {"language": language, "available": bool(
            language != "en" and fast and self.coach.llm.available(fast)
            and self.coach.llm.provider_for(fast) is not None)}
        # push-to-talk's speech-to-text, for the app's status bar (speech output is the app's own setting)
        p = getattr(self, "ptt", None)
        st = p.stt if p is not None and p.ok else None
        models["listen"] = st.name if st else ("loading" if p is not None and p.ok else "off")
        models["listen_fallback"] = p.fallback.name if st and p.fallback and p.fallback is not st else None
        return models

    def _why_panel(self, s):
        """(why, pace): the compact step brief and pace from the coach thread's last knowledge block (never
        computed here)."""
        try:
            p = self.pack.panel(s)
        except Exception as e:  # noqa: BLE001
            p = {"why": {"unavailable": "why panel failed: %s" % e}, "pace": None}
        why = p.get("why")
        if p.get("stale") and why:
            why = dict(why, stale=True)
        return why, p.get("pace")

    @staticmethod
    def step_line(s):
        """The overlay's one line: level, current step, its quests, and the walk to its waypoint."""
        cur = s["steps"][0] if s.get("steps") else None
        if not cur:
            return None
        names = [("%s %s" % (e["tag"], e.get("name") or e.get("quest", ""))).strip()
                 for e in cur["elements"] if e.get("quest")]
        line = "L%s  %s  |  %s" % (s["level"], cur.get("text") or "", ", ".join(names[:3]))
        wp = s.get("waypoint") or {}
        if wp.get("yards") is not None or wp.get("relative"):
            line += "  |  %s%s" % (("%d yd " % wp["yards"]) if wp.get("yards") is not None else "",
                                  wp.get("relative") or wp.get("direction") or "")
        return line

    # ------------------------------------------------------------------------------------------
    def capture_loop(self):
        from capture import WowCapture
        cap = WowCapture(self.cfg["capture"]["width"], self.cfg["capture"]["height"],
                         self.cfg["capture"]["window_title"])
        self.cap = cap
        coach = getattr(self, "coach", None)
        if coach is not None:
            coach.banter.window_gone = lambda: not cap.hwnd     # the game closed: the character says goodbye
        base = (self.cfg["capture"]["width"], self.cfg["capture"]["height"])
        g = self.cfg["grid"]
        loc = None
        period = 1.0 / self.cfg["capture"]["fps"]
        warned = 0
        while True:
            t0 = time.time()
            try:
                img = cap.frame()
                self.last_img, self.last_img_t, self.last_loc = img, time.time(), loc    # for calibrate_now()
                if img is None:
                    if time.time() - warned > 30:
                        warned = time.time()
                        log("capture", "WoW window not found / minimised")
                else:
                    self.attempts += 1
                    res, loc2 = protocol.decode_image(img, g["cols"], g["rows"], loc)
                    if res is None and loc is not None:
                        res, loc2 = protocol.decode_image(img, g["cols"], g["rows"], None)   # re-locate
                    if loc2:
                        # the grab follows the strip's real size (bigger /glide cell, a strip pushed down), so it never
                        # cuts the strip or its gauges off; the next frame is read at the new size
                        w, h = protocol.fit_size(loc2, g["cols"], g["rows"], base)
                        if cap.resize(w, h):
                            log("capture", "the strip has %d px cells; reading a %d x %d corner" % (loc2[2], w, h))
                            if res is None:
                                loc = loc2
                                continue
                    if res:
                        loc = loc2
                        self.decoded += 1
                        self.state.gauges = protocol.read_gauges(img, loc, g["rows"])
                        self.on_frame(*res)
                    elif isinstance(res, protocol.OtherVersion):
                        # the strip is there and reads cleanly, in a version this engine doesn't speak: say so
                        # (the Status page too) instead of "is the addon on?"
                        loc = loc2
                        self.note_other_version(res.version)
                        if time.time() - warned > 30:
                            warned = time.time()
                            m = self.versions.strip_mismatch(res.version, protocol.PROTO,
                                                             app=getattr(self.args, "app", False))
                            log("capture", "%s: %s." % (m["problem"], m["fix"]))
                    elif time.time() - warned > 30 and self.attempts > 20 and self.decoded == 0:
                        warned = time.time()
                        log("capture", "no Glide strip found. Is the addon on (/glide strip on)? Check it under "
                                       "Settings > Game capture (or run with --calibrate).")
            except Exception as e:  # noqa: BLE001
                loop_error("capture", e)
            time.sleep(max(0, period - (time.time() - t0)))

    def set_muted(self, muted):
        """Mute or unmute the engine's narrator (POST /mute). The coach still writes its callouts to the dashboard and
        the overlay; it just doesn't speak them. Under the desktop app with voice output "app", the app owns mute
        (its narrator drops lines while muted) and doesn't send this."""
        return {"muted": self.voice.set_muted(muted)}

    def calibrate_now(self):
        """What the capture sees right now, for Settings > Game capture: the captured area as a PNG, where the
        addon's strip is in it, and what it decodes to. Uses the running capture's latest frame when fresh."""
        from capture import WowCapture
        if self.args.sim:
            return {"ok": False, "problem": "sim", "text": "Simulation mode doesn't read the game. Turn it off "
                    "under General, restart the coach, then check again."}
        c = self.cfg["capture"]
        img = getattr(self, "last_img", None)
        if img is None or time.time() - getattr(self, "last_img_t", 0) > 2:
            try:
                cap = getattr(self, "cap", None)        # the running capture's size, which follows the strip
                img = WowCapture(cap.w if cap else c["width"], cap.h if cap else c["height"], c["window_title"]).frame()
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "problem": "error", "text": "The capture failed: %s" % e}
        if img is None:
            return {"ok": False, "problem": "window", "text": "No WoW window titled \"%s\" was found, or it's "
                    "minimised. Start the game (or fix the window title below) and check again." % c["window_title"]}
        return calibration.diagnose(img, self.cfg["grid"], self.state.summary, getattr(self, "attempts", 0),
                                    getattr(self, "decoded", 0))

    def sim_loop(self):
        import sim
        sim.run(self, self.cfg["grid"])

    def vocabulary(self):
        """Names likely to come up, to help speech-to-text spell them right."""
        s = self.state.summary()
        names = [s.get("zone"), s.get("subzone"), (s.get("target") or {}).get("name"), s.get("class")]
        for step in s["steps"]:
            for e in (step or {}).get("elements", []):
                if e.get("quest"):
                    names.append(self._quest_basics(e["quest"]).get("name"))
        for qid in (s.get("quests") or {}):
            names.append(self._quest_basics(qid).get("name"))
        names = [n for n in dict.fromkeys(names) if n]
        return "World of Warcraft leveling question. Names: " + ", ".join(names)

    def start_ptt(self):
        c = dict(self.cfg["ptt"], language=settings.Settings("voice", self.cfg.get("voice", {})).get("language"))
        if not c.get("enabled") or self.args.no_ptt or sys.platform != "win32":
            return
        focus = None
        if settings.Settings("ptt", c).get("only_when_wow_focused") and not self.args.sim:
            import ctypes
            u32 = ctypes.windll.user32

            def focus():
                hwnd = u32.GetForegroundWindow()
                buf = ctypes.create_unicode_buffer(256)
                u32.GetWindowTextW(hwnd, buf, 255)
                return buf.value.startswith(self.cfg["capture"]["window_title"])
        try:
            from ptt import PushToTalk
            # the "ptt" event goes out as it happens: the overlay, the session bar and the coach screen show it
            self.ptt = PushToTalk(c, lambda q: self.coach.ask(q, voice=True), self.on_message,
                                  self.vocabulary, focus, on_state=self.on_ptt_state)
        except Exception as e:
            log("ptt", "disabled: %s" % e)

    def on_localize(self, body):
        """POST /localize: English sample text, localized with the coach's configured LLM or kept English."""
        text = body.get("text")
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        return self.coach.localize(text)

    def http_routes(self):
        """The engine's HTTP routes (contract 2) besides the dashboard's own page, stream, state and layout. POSTs
        need the run's token; so do the GETs that run something (/calibrate, /models?refresh=1 rebuilds the model
        list) or show the player's notes (/memory)."""
        def models(q):
            return self.on_models(refresh=q.get("refresh") == "1", reasoning_model=q.get("reasoning_model"))

        def refresh(body):
            return self.nav_rebuild() if body.get("nav") else self.refresh_now(bool(body.get("force")))

        return {
            ("POST", "/ask"): ask_route(self.coach.ask),
            ("POST", "/announce"): announce_route(self.coach.announce_update),
            ("POST", "/localize"): post_json(self.on_localize),
            ("POST", "/settings"): post_json(self.on_settings, lambda applied: {"applied": applied}),  # + "saved"
            ("GET", "/models"): Route(get_json(models, on_error=lambda e: {"error": str(e)}), token=True),
            ("POST", "/reminders"): post_json(self.on_reminder),
            ("GET", "/memory"): Route(get_json(lambda q: self.on_memory(None)), token=True),
            ("POST", "/memory"): post_json(self.on_memory, lambda notes: {"memory": notes}),
            ("POST", "/mute"): post_json(lambda body: self.set_muted(bool(body.get("muted")))),
            ("POST", "/refresh"): post_json(refresh, lambda st: {"refresh": st}),
            ("GET", "/calibrate"): Route(get_json(lambda q: self.calibrate_now(), on_error=lambda e: {
                "ok": False, "problem": "error", "text": str(e)}), token=True),
            ("GET", "/health"): get_json(lambda q: self.health.snapshot(),
                                         on_error=lambda e: {"checks": [], "error": str(e)}),
            ("GET", "/xph"): get_json(lambda q: self.xph.snapshot()),
            ("POST", "/community/seen"): post_json(lambda body: self.feed.seen() if self.feed is not None else None,
                                                   lambda st: {"community_feed": st}),
        }

    def start(self):
        """Serve the dashboard, start the background work and the strip reader, then run until the app closes
        (--app), the overlay window closes, or Ctrl+C."""
        port = self.args.port or self.cfg["dashboard"]["port"]
        # POSTs need this run's token: the desktop app's (GLIDE_TOKEN, it opens the page with it), or one made here
        # that only the link printed below carries (in its fragment, which never reaches a server)
        import secrets
        token = os.environ.get("GLIDE_TOKEN") or secrets.token_urlsafe(24)
        link = "http://127.0.0.1:%d/" % port + ("" if os.environ.get("GLIDE_TOKEN") else "#t=" + token)
        serve(self.hub, port, self.http_routes(), layout_path=live_path("dashboard_layout.json"), token=token)
        console_log("Glide companion: expects Glide addon %s" % (self.addon_expected or "?"))
        console_log("Glide companion: dashboard %s  | coach: %s | db: %s | %s" % (
            link, self.coach.describe_models() if self.coach.enabled
            else "offline (set ANTHROPIC_API_KEY, GEMINI_API_KEY or OPENROUTER_API_KEY)",
            "ok" if self.k.ok else "MISSING data/glide.db", kb_status_line(self.pack, self.kb_reason)))
        if self.coach.enabled:
            self.coach.announce_models({}, startup=True)
        threading.Thread(target=self.publish_state, daemon=True).start()
        self._start_background_work()
        if self.feed is not None:
            self.feed.start()
        loop = self.sim_loop if self.args.sim else self.capture_loop
        threading.Thread(target=loop, daemon=True).start()
        if self.args.app:
            self._run_under_app(port)
        else:
            self._run_standalone()

    def _start_background_work(self):
        """The SavedVariables archiver, push-to-talk, and (playing for real) the knowledge refresh and the walking
        map build."""
        if not self.args.sim and not self.args.no_archive:
            from archiver import Archiver
            Archiver(glideconfig.wow_dir(ROOT), ROOT, log=console_log).start()
        if not self.args.sim:
            threading.Thread(target=self._sync_rxp_imports, daemon=True).start()
        self.start_ptt()
        if not self.args.sim and not getattr(self.args, "no_kb", False):
            import kb_refresh
            self._start_refresher()
            # the walking map: once per install (missing or an older NAV_VERSION), after the map extraction
            self.navbuild = kb_refresh.NavBuilder(ROOT, log=console_log,
                                                  first_map=self._continent, wait_for=self.refresher)
            self.navbuild.start()

    def _sync_rxp_imports(self):
        """The RestedXP strings added in the app that RestedXP doesn't have yet back in the addon's
        Data/RXPImports.lua (an addon update replaced it), and the ones it has out of it (rxp_import.sync)."""
        try:
            import rxp_import
            rxp_import.sync(ROOT, glideconfig.wow_dir(ROOT))
        except Exception as e:  # noqa: BLE001 - the next start tries again
            log_once("rxp_import.sync", e)

    def _run_under_app(self, port):
        """Run by the desktop app: it shows the overlay and speaks; the companion stops when the app closes its
        stdin (the app exiting or crashing does too)."""
        # the app's version (GLIDE_APP_VERSION) goes after the port, so the app's /GLIDE_READY port=N/ still matches
        console_log("GLIDE_READY port=%d app=%s" % (port, self.app_version or "?"))
        try:
            while sys.stdin.readline():
                pass
        except (OSError, ValueError):
            pass
        if getattr(self, "navbuild", None) is not None:
            self.navbuild.stop()            # it resumes from its finished tiles next time
        console_log("Glide companion stopped (the app closed).")

    def _run_standalone(self):
        """Run on its own: the engine's overlay window until Ctrl+C, or (overlay off or failing) until killed."""
        if self.cfg["overlay"]["enabled"] and not self.args.no_overlay:
            try:
                from overlay import Overlay
                self.overlay = Overlay(self.cfg["overlay"],
                                       state_file=live_path("overlay.json"))
                # Ctrl+C: Tk swallows KeyboardInterrupt inside callbacks, so set a flag the overlay polls
                import signal
                signal.signal(signal.SIGINT, lambda *_: setattr(self.overlay, "stop", True))
                self.overlay.run()              # blocks (tk main loop) until Ctrl+C
                console_log("Glide companion stopped.")
                return
            except Exception as e:
                log("overlay", "disabled: %s" % e)
        while True:
            time.sleep(3600)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--no-voice", action="store_true")
    ap.add_argument("--no-overlay", action="store_true")
    ap.add_argument("--no-ptt", action="store_true")
    ap.add_argument("--no-archive", action="store_true")
    ap.add_argument("--no-kb", action="store_true", help="don't refresh glide.db / kb.db in the background")
    ap.add_argument("--port", type=int)
    ap.add_argument("--app", action="store_true",
                    help="run under the Glide desktop app (it speaks and shows the overlay; stop when stdin closes)")
    a = ap.parse_args()
    # Load the compiled libraries the background threads use (numpy, and faster-whisper / ctranslate2 for
    # push-to-talk) here, on the main thread, before any thread starts. Loaded for the first time inside the
    # push-to-talk thread while the capture, dashboard and coach threads run, their DLL initialisation can deadlock
    # in the Windows loader, which freezes every thread that starts or loads anything (the dashboard stops answering).
    # On an install that has extracted map data this happened to be avoided, because the map code loads numpy early.
    for _mod in ("numpy", "faster_whisper"):
        try:
            __import__(_mod)
        except ModuleNotFoundError:
            pass                            # optional: a missing one only turns its feature off
        except Exception as _e:  # noqa: BLE001 - installed but broken: its feature reports it when it starts
            log_once("startup.import." + _mod, _e)
    if a.app:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except AttributeError:
            pass
    if not a.sim:
        try:
            glideconfig.drop_retired(ROOT)     # (settings no longer used: out of the player's config.json)
        except (OSError, ValueError) as e:
            console_log("[config] couldn't tidy %s: %s" % (glideconfig.path(ROOT), e))
    c = load_config()
    if a.calibrate:
        calibration.save_snapshot(c, live_path("capture.png"))
    else:
        try:
            App(a, c).start()
        except KeyboardInterrupt:
            pass
