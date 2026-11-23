"""Reflection: now and then the deep model reviews the recent conversation (and the player's "/glide mark" feedback)
against what the coach has learned about the player, and updates that memory (memory.py; the prompt and the reply's
handling are coach_prompts.py's and coach_tools.py's). One of the coach thread's tickers (Coach._loop calls tick about
once a second); the reflection itself runs on its own thread."""
import threading
import time

import coach_prompts as prompts
import coach_tools
from log import log


class Reflector:
    """coach: the Coach whose conversation and memory (deps.memory) this reviews."""

    REFLECT_SCHEMA = prompts.REFLECT_SCHEMA      # the reply shape the reflection asks for

    def __init__(self, coach):
        self.coach = coach
        self.reflect_t = 0.0                  # when the last reflection started (this process)
        self.marks = []                       # recent "/glide mark" feedback, for reflection
        self.soon = False                     # reflect at the next tick (the session just ended: its chapter)
        self.thread = None                    # the reflection running (on its own thread)

    def tick(self):
        """Now and then (enough new exchanges, or a feedback mark, and 20 minutes since the last), let the deep model
        review the recent conversation against the memory and update it. Off when learning is off or the AI is off."""
        c = self.coach
        m = c.deps.memory
        llm = c.llm
        if m is None or not m.learning or not llm.enabled or llm.suspended or time.time() < llm.backoff_until:
            return
        if self.thread is not None and self.thread.is_alive():
            return                   # one reflection at a time (they'd write the same stretch twice)
        now = c._now()
        soon, self.soon = self.soon, False
        if not soon and now - max(m.last_reflect, self.reflect_t) < float(c.opt.get("reflect_minutes")) * 60:
            return
        since = max(m.last_reflect, self.reflect_t)
        events = [e for e in list(c.convo) if e["t"] > since]
        asked = [e for e in events if e["who"] == "player"]
        asides = [e for e in events if e.get("kind") == "banter"]     # how they take the asides teaches rapport
        if soon:                     # the session ended (banter.py's goodbye): its chapter, if you talked at all
            if not asked and not asides:
                return
        elif len(asked) < int(c.opt.get("reflect_min_questions")) and len(asides) < 4 and not self.marks_since(since):
            return
        self.reflect_t = now
        self.thread = threading.Thread(target=self.reflect, args=(events,), daemon=True, name="coach-reflect")
        self.thread.start()

    def marks_since(self, since):
        return [x for x in self.marks if x["t"] > since]

    def note_mark(self, note, last_text):
        """A '/glide mark <note>' feedback flag: kept for reflection, and a note of its own when the player wrote
        one."""
        c = self.coach
        self.marks.append({"t": c._now(), "note": note or "", "coach_said": last_text or ""})
        del self.marks[:-20]
        if note and c.deps.memory is not None:
            text = "Corrected: %s%s" % (note.rstrip("."), (" (about: \"%s\")" % last_text[:120]) if last_text else "")
            n, _ = c.deps.memory.add(text, "feedback", source="mark")
            if n:
                c.emit({"kind": "info", "text": "Remembered your feedback: %s" % n["text"], "speak": False})

    def reflect(self, events=None):
        """One reflection pass (deep model, no tools). Applies the JSON reply to the memory; returns the changes."""
        c = self.coach
        m = c.deps.memory
        if m is None:
            return None
        since = max(m.last_reflect, 0)
        events = events if events is not None else [e for e in list(c.convo) if e["t"] > since]
        lines = coach_tools.reflection_events(events, self.marks_since(since))
        # what happened together (the diary: deaths, dings, close calls), for the rapport notes' shared moments
        lines += ["- shared moment: %s" % d["text"] for d in m.diary_since(since)[-20:]]
        if not lines:
            return None
        notes = "\n".join("[%d] (%s) %s" % (n["id"], n["kind"], n["text"]) for n in m.list()) or "(none yet)"
        threads = "\n".join("[%s] %s" % (x["id"], x["text"]) for x in m.threads_open()) or "(none)"
        model = c.cfg.get("deep_model")
        try:
            text, _meta = c.llm.call_once(model, prompts.REFLECT_PROMPT % (notes, threads, "\n".join(lines)), 1600,
                                          deep=True,
                                          schema=self.REFLECT_SCHEMA, role="reflection")
            if _meta.get("attribution"):
                log("memory", "Reflection generation finished.", _meta["attribution"])
        except Exception as e:  # noqa: BLE001 - learning is a background extra
            log("memory", "reflection failed: %s" % e)
            return None
        m.mark_reflected(c._now())
        return coach_tools.apply_reflection(m, text, c.emit, since=since or None, now=c._now())
