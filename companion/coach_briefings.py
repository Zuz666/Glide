"""The coach's quest briefings and step videos (the rules and texts are briefing.py's): accepted quests briefed once
the accepts stop coming, a burst of pickups held until the player gets to each quest, and the current RestedXP
step's YouTube clips. One of the coach thread's tickers (Coach._loop calls tick about once a second)."""
import math
import time

import briefing
from log import log_once
from prompt_context import short_json
from replies import EarlySpeech, is_skip, trim_words
from rules import Trigger


class Briefings:
    """coach: the Coach these brief for (its state, knowledge pack, model calls and messages)."""

    def __init__(self, coach):
        self.coach = coach
        opt = coach.opt
        self.accepted = briefing.QuestBriefs(float(opt.get("briefing_delay_s")))     # accepted quests to brief
        self.held = briefing.HeldBriefs(float(opt.get("briefing_gap_s")))     # picked up in a burst: brief later
        self.held_t = 0.0                     # when held quests were last checked
        self.quest_spots = {}                 # (quest id, map) -> objective points on that map, for HeldBriefs
        self.videos = briefing.StepVideos(    # RestedXP step YouTube links -> new browser window
            lambda url: briefing.open_new_window(url, coach.opt.get("video_browser") or None))

    def add(self, qid, now):
        """A quest was accepted: brief it once accepts stop coming for a moment."""
        self.accepted.add(qid, now)

    def busy(self):
        """Work waiting that should be looked at again soon."""
        return bool(self.accepted.queue or self.videos.pending)

    def tick(self):
        self.due_tick()
        self.held_tick()
        self.step_videos()

    def due_tick(self):
        """Quests accepted a moment ago: brief them, or (a burst of pickups) hold them until the player gets there."""
        c = self.coach
        due = self.accepted.due(c._now())
        if due and len(due) >= int(c.opt.get("briefing_burst")):
            self.hold(due)
        elif due:
            self.brief(due)

    def hold(self, qids):
        """Quests picked up back to back: list them quietly, brief each one when the player gets to it."""
        c = self.coach
        log = c._summary().get("quests") or {}
        names = [((log.get(q) or log.get(str(q)) or {}).get("name")) or c._quest_name(q) for q in qids]
        self.held.hold(qids, c._now())
        c.emit({"kind": "info", "speak": False, "t": time.time(),
                "text": "Picked up %d quests: %s. Each gets its briefing when you get to it." % (
                    len(qids), ", ".join(n for n in names if n) or "names unknown")})
        # spoken in the callout that sums up this stop at the quest hub (turn-ins, reward pick, what's next)
        c.queue_triggers([Trigger("quests_accepted", {"count": len(qids), "quests": [n for n in names if n]},
                                  priority=3)])

    def held_tick(self):
        """Brief a held quest once the player gets to it (briefing.HeldBriefs), checked about once a second."""
        c = self.coach
        if not self.held.quests or c.state is None or time.time() - self.held_t < 1:
            return None
        self.held_t = time.time()
        if not c.opt.get("quest_briefings"):
            self.held.quests.clear()
            return None
        s = c.state.summary()
        pack = c.pack
        target_fn = pack.target_quest_ids if pack is not None and hasattr(pack, "target_quest_ids") else None
        pick = self.held.check(s, c._now(), target_fn, self._near_quest)
        return self.brief([pick[0]], later=pick[1]) if pick else None

    def _near_quest(self, qid, s):
        """True when the player is within briefing_near_yd of one of the quest's objective spots on this map."""
        c = self.coach
        mp, x, y = s.get("map"), s.get("x"), s.get("y")
        if not mp or x is None:
            return False
        # the zone's size in yards; until the game gives it, a typical zone (good enough for "about 150 yards")
        size = (c.state.map_size(mp) if hasattr(c.state, "map_size") else None) or (3450.0, 2300.0)
        key = (qid, mp)
        if key not in self.quest_spots:
            pack = c.pack
            spots = pack.quest_spots(qid, mp) if pack is not None and hasattr(pack, "quest_spots") else []
            if not spots and pack is None:    # no knowledge base: glide.db's spawn centre per objective
                q = c.k.quest(qid, near_map=mp, detail=False) or {}
                spots = [(o["where"]["x"], o["where"]["y"]) for o in q.get("objectives") or ()
                         if (o.get("where") or {}).get("map") == mp]
            self.quest_spots[key] = spots
        r = float(c.opt.get("briefing_near_yd"))
        return any(math.hypot((px - x) / 100.0 * size[0], (py - y) / 100.0 * size[1]) <= r
                   for px, py in self.quest_spots[key])

    def brief(self, qids, later=None):
        """One message for the quests just accepted: each objective as the game words it, then the coach's
        explanation of how to do them (fast model; rule-based when the AI is off). Returns the message or None.
        later: a quest held from a burst of pickups, briefed now because the player got to it ("target" | "step" |
        "near")."""
        c = self.coach
        if not c.opt.get("quest_briefings") or c.state is None:
            return None
        infos = self._brief_infos(c.state.summary(), qids)
        if not infos:
            return None
        head, infos = briefing.spoken_head(infos, later, int(c.opt.get("briefing_max_quests")))
        words = min(int(c.opt.get("briefing_words")) + 20 * (min(len(infos), 4) - 1), 100)
        head = c.localize_text(head)
        speech = None
        speak_fn = c.deps.speak_fn
        if speak_fn and head:
            # Prepare the objective before speech; the model produces the explanation natively in the same language.
            if speak_fn(head, "briefing", False) is not False:
                speech = EarlySpeech(lambda t, u: speak_fn(t, "briefing", u), words, prefix=head)
        attribution = {}
        expl, model_txt, facts = self._explain_quests(infos, later, words, speech, attribution=attribution)
        if not expl:
            expl = " ".join(filter(None, (briefing.offline_explanation(b) for b in infos)))
            expl = c.localize_text(expl)
        msg = {"kind": "briefing", "text": (head + " " + expl).strip(), "speak": True, "urgent": False,
               "model": model_txt, "quests": [b["id"] for b in infos], "t": time.time()}
        if attribution:
            msg["attribution"] = attribution
        # Both parts are ready: do not retranslate the model's native explanation or the early-spoken objective.
        msg["text"] = c.localization.generated(msg["text"])
        if speech is not None:
            msg["spoken"] = speech.spoken          # the head (and any streamed sentences) was said already
        if c.pack is not None and facts:
            c._ground(msg, facts, True)
        c.emit(msg)
        return msg

    def _brief_infos(self, s, qids):
        """What the knowledge layer knows about each quest (briefing.quest_brief), else its name from the quest log;
        quests with neither are left out."""
        c = self.coach
        kctx = None
        if c.pack is not None:
            try:
                kctx = c.kctx(s, ["quest_accepted"])
            except Exception as e:  # noqa: BLE001
                log_once("briefings.kctx", e)
                kctx = None
        log = s.get("quests") or {}
        infos = []
        for q in qids:
            b = briefing.quest_brief(c.api, kctx, q) if kctx is not None else None
            if b is None:
                name = ((log.get(q) or log.get(str(q)) or {}).get("name"))
                if not name:
                    continue
                b = {"id": q, "name": name, "objective_text": ""}
            infos.append(b)
        return infos

    def _explain_quests(self, infos, later, words, speech, attribution=None):
        """The fast model's explanation of how to do the quests, when it can be called. -> (text or "", the model
        note, fact ids)."""
        c = self.coach
        model = c.cfg.get("fast_model")
        if not c.llm.available(model):
            return "", "offline", []
        ctx = c.context(deep=False, triggers=["quest_accepted"])
        trigger = briefing.explain_request(infos, later, words,
                                           short_json([briefing.prompt_fields(b) for b in infos], 5000))
        user = c._user_message(ctx, trigger, cc=c.llm.ref_marker(model))
        text, _meta = c.llm.call_once(model, user, 400, deep=False, tools=c._shared_tools(model),
                                     on_text=speech, role="quest briefing")
        if attribution is not None and _meta.get("attribution"):
            attribution.update(_meta["attribution"])
        if not text:
            return "", "offline", []
        text, facts, _had = c._directives(text, with_facts=True)
        if is_skip(text):
            return "", "offline", facts
        return trim_words(text, words), c._model_note(model, _meta), facts

    def step_videos(self):
        """The current RestedXP step's YouTube clips (see briefing.StepVideos): what each shows, said aloud, and the
        clip opened in the browser when that's on."""
        c = self.coach
        if c.state is None or c.pack is None:
            return []
        try:
            s = c.state.summary()
        except Exception as e:  # noqa: BLE001
            log_once("videos.summary", e)
            return []

        def resolve():
            k = c.kctx(s, None)
            return k.pack, k.guide_key, k.idx

        try:
            opened = self.videos.check(s, resolve, enabled=c.opt.get("step_videos"),
                                       notes=c.opt.get("video_tips"))
        except Exception as e:  # noqa: BLE001
            log_once("videos.check", e)
            return []
        for url, label, was_opened, note in opened:
            if note:                   # what the clip shows, in the coach's voice, with the clip one click away
                c.emit({"kind": "briefing", "text": note["instruction"], "speak": True, "url": url,
                        "model": "step video"})
                if note.get("detail"):  # the path, skills and mechanics in full, to read (not spoken)
                    c.emit({"kind": "info", "text": note["detail"], "speak": False, "url": url,
                            "model": "step video"})
            else:
                c.emit({"kind": "info", "text": "%s%s: %s" % (
                    "Opened this step's video" if was_opened else "A video for this step",
                    " (%s)" % label if label else "", url), "speak": False, "url": url})
        return opened

    def switched_off(self, key):
        """A switch said out loud turned a feature off: drop what it had waiting."""
        if key == "step_videos":
            self.videos.pending = []
        if key == "quest_briefings":
            self.accepted.queue = []
