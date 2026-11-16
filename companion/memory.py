"""The coach's long-term memory of the player: coach_memory.json in memory_dir().

What the player tells or teaches the coach outlives the session: preferences about how to be coached, goals, standing
requests, corrections of the coach's mistakes, and facts about the player and their playstyle. Each note:

    {"id": 7, "kind": "preference", "text": "Wants callouts under 10 words.", "source": "player" | "coach" |
     "reflection" | "mark", "created": <unix>, "updated": <unix>, "pinned": false, "used": 3}

Notes come from the player ("remember that ...", "from now on ...", the app's Memory page), from the coach's
memory_remember tool, from "/glide mark <note>" feedback, and from reflection: now and then the deep model reviews the
recent conversation and feedback against the notes and proposes additions, rewrites and removals (Coach.reflect).

Where it lives (memory_dir): GLIDE_MEMORY_DIR when set, else %APPDATA%\\Glide\\memory on Windows (~/.glide/memory
elsewhere): outside the app's install folder and the engine home, so it survives app updates, reinstalls, a moved
engine home and the plain start-glide.cmd companion alike. A dated copy is kept once a day (the last 14).

block() is the prompt section every coach call carries (a system block of its own, so it stays in the prompt cache
until a note changes). Pure stdlib; thread-safe; a missing or broken file starts an empty memory.

The bond with the player (banter.py: hours together, sessions, how the asides went, the diary of shared moments, the
zones visited together, the characters played with their lore) lives beside it in coach_bond.json, a file older
engines never read or write, so going back to an older version keeps it. "rapport" notes are written to
coach_memory.json as kind "fact" with "rapport": true (older engines keep and show them as facts), and notes of a kind
or top-level keys this engine doesn't know are written back as they were.
"""
import json
import math
import os
import re
import sys
import threading
import time

import jsonfile
from log import log

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if ROOT not in sys.path:
    sys.path.append(ROOT)       # the knowledge package: its values module (stdlib only) reads stored numbers
from knowledge.values import as_float, as_int  # noqa: E402

KINDS = ("preference", "request", "goal", "feedback", "fact", "rapport")
KIND_LABEL = {"preference": "How they like to be coached", "request": "Standing requests",
              "goal": "Goals", "feedback": "Corrections (don't repeat these mistakes)",
              "fact": "About the player",
              "rapport": "Your bond with them (what lands, inside jokes, nicknames, shared moments)"}
BACKUPS = 14                  # daily copies kept beside the file
MAX_NOTES = 200               # oldest unpinned notes beyond this are dropped
MAX_TEXT = 300                # characters per note
BLOCK_CHARS = 2400            # the prompt block's budget (about 600 tokens)
PER_KIND = {"preference": 12, "request": 10, "goal": 6, "feedback": 10, "fact": 10, "rapport": 12}
BOND_SAVE_S = 300             # the bond clock is saved at most this often (asides and replies at once)
# how long the coach and the player have played together -> the stage of their bond (banter.py speaks to it)
BOND_STAGES = ((3, "new"), (15, "getting to know each other"), (50, "familiar"), (None, "old friends"))
MAX_DIARY = 80                # shared moments kept (newest)
MAX_LORE = 24                 # facts a character told about itself, per character
LORE_KEPT = 8                 # ... its first ones are never dropped (who it is); the oldest of the rest go first
MAX_CHARACTERS = 12           # characters kept (most recently played)
MAX_CHAPTERS = 16             # session chapters kept whole; past this the oldest FOLD fold into one era
FOLD = 8
MAX_ERAS = 24                 # eras kept (the oldest stretches of the climb, in brief)
MAX_THREADS = 12              # open threads (promises, questions, plans); the oldest goes when it's full
# a line given another speaker ("[Name] ..."), as an older version kept some replies: never the coach's own words,
# taken out of what is kept so the coach never reads the form back
_OTHER_SPEAKER = re.compile(r"^[ \t]*\[[^\]\n]{1,40}\][ \t]+.*(?:\n|$)", re.M)


def _own_words(text):
    """The text without its lines given another speaker ("[Name] ...")."""
    return _OTHER_SPEAKER.sub("", text).strip()


KEEP_CONVO = 60               # conversation lines kept across restarts
CHARACTER_LISTS = {"samples": 6, "favorites": 6, "plot": 4, "lines": 60}   # per character: kept, newest
BOND_KEYS = ("bond", "diary", "zones", "characters", "chapters", "eras", "threads", "next_thread", "convo", "cleared_t")


def memory_dir():
    """The folder that holds the memory, the same for every install and engine home of this Windows user."""
    d = os.environ.get("GLIDE_MEMORY_DIR")
    if d:
        return d
    base = os.environ.get("APPDATA")
    return os.path.join(base, "Glide", "memory") if base else os.path.join(os.path.expanduser("~"), ".glide", "memory")


def default_path():
    return os.path.join(memory_dir(), "coach_memory.json")


def _when(v):
    """A stored time: a finite number of seconds, not more than a day in the future; else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v > time.time() + 86400:
        return None
    return float(v)


def _typed(d, key, kind):
    """d[key] when it is a `kind` (list, dict), else an empty one: a stored file's part, checked."""
    v = d.get(key)
    return v if isinstance(v, kind) else kind()


def _norm(text):
    return re.sub(r"[^a-z0-9 ]", "", " ".join(str(text or "").lower().split()))


def _similar(a, b):
    """Rough duplicate test: same normalised text, or one contains the other, or 80% of the words shared."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    wa, wb = set(a.split()), set(b.split())
    return len(wa & wb) / max(1, min(len(wa), len(wb))) >= 0.8 and min(len(wa), len(wb)) >= 4


def guess_kind(text):
    """The kind a free-text note most likely is (the player's own "remember that ..." words)."""
    t = (text or "").lower()
    if re.search(r"\b(you were wrong|that was wrong|incorrect|you said|mistake|actually|not true|wrong about)\b", t):
        return "feedback"
    if re.search(r"\b(always|every time|whenever|each time|remind me when|tell me when)\b", t):
        return "request"
    if re.search(r"\b(goal|aim|aiming|want to (?:reach|hit|get)|by (?:mon|tue|wed|thu|fri|sat|sun)\w*|plan to|planning)\b", t):
        return "goal"
    if re.search(r"\b(prefer|don'?t|do not|never|stop|less|more|shorter|longer|quiet|only|rather|no need|skip|hate|love|like|dislike|annoy\w*|can't stand|enjoy)\b", t):
        return "preference"
    return "fact"


class Memory:
    def __init__(self, path):
        """path: the JSON file; None keeps the memory in this process only (simulation, tests)."""
        self.path = path
        self.lock = threading.Lock()
        self.notes = []
        self.next_id = 1
        self.learning = True               # reflection on (the app's Memory page switch; saved with the notes)
        self.last_reflect = 0.0
        self.version = 0                   # bumped on every change (the prompt block is rebuilt then)
        self._recall_texts = None          # immutable eligible text snapshot, rebuilt only after relevant changes
        self.bond_path = os.path.join(os.path.dirname(path), "coach_bond.json") if path else None
        self._foreign = []                 # notes of kinds this engine doesn't know: written back untouched
        self._extra = {}                   # top-level keys this engine doesn't know: written back untouched
        self.zones = set()                 # the zones visited together (not capped like the diary)
        # time played together and how the coach's asides went (banter.py): {"hours", "sessions", "asides", "replied"}
        self.bond = {"hours": 0.0, "sessions": 0, "asides": 0, "replied": 0, "last_seen": 0.0, "first_seen": 0.0,
                     "by_activity": {}}
        # the story so far: chapters (one per reflection: what happened, the jokes, what they shared) folded into
        # eras when old; the open threads between you; the conversation's last lines (kept across restarts)
        self.chapters, self.eras, self.threads, self.convo = [], [], [], []
        self.next_thread = 1
        self.cleared_t = 0.0                # "forget everything" happened then: nothing older comes back
        self._bond_extra = {}               # coach_bond.json keys this engine doesn't know: written back untouched
        # the diary of shared moments: [{"t", "kind", "text", "session"}] (banter.py writes it, the asides recall it)
        self.diary = []
        # the characters the coach played for them, by key: {"name", "brief", "voice", "lore": [...], "last"}
        self.characters = {}
        self._bond_saved = 0.0
        self._tidied = False                # what an older version kept was taken out on load: saved once, at once
        self._load()

    # ---- storage --------------------------------------------------------------------------------------------------
    _KNOWN = ("notes", "next_id", "learning", "last_reflect")

    @staticmethod
    def _read(path):
        """A JSON object from `path`, or {} (missing, unreadable, not an object)."""
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def _load(self):
        d = self._read(self.path)
        notes = _typed(d, "notes", list)
        for n in notes:
            if not isinstance(n, dict) or not isinstance(n.get("text"), str) or not n["text"].strip():
                continue
            if n.get("kind") == "fact" and n.get("rapport"):
                n = {k: v for k, v in n.items() if k != "rapport"}
                n["kind"] = "rapport"
            if n.get("kind") not in KINDS:
                self._foreign.append(n)
                continue
            try:
                n["id"], n["updated"] = int(n.get("id") or 0), float(n.get("updated") or n.get("created") or 0)
            except (TypeError, ValueError):
                continue
            self.notes.append(n)
        ids = [n["id"] for n in self.notes] + [x.get("id") for x in self._foreign if isinstance(x.get("id"), int)]
        self.next_id = max([as_int(d.get("next_id"), 1, bool_ok=False)] + [i + 1 for i in ids])
        self.learning = bool(d.get("learning", True))
        self.last_reflect = as_float(d.get("last_reflect"), 0.0, finite=True, bool_ok=False)
        self._extra = {k: v for k, v in d.items() if k not in self._KNOWN}
        # the bond: coach_bond.json (an earlier build of this engine kept it in coach_memory.json: read from there once)
        b = self._read(self.bond_path) or {k: d.get(k) for k in ("bond", "diary", "characters", "zones")}
        for k in ("bond", "diary", "characters", "zones"):
            self._extra.pop(k, None)
        self._load_bond(b)
        if self._tidied:
            self._save_bond()

    def _load_bond(self, d):
        """The bond's parts from a parsed coach_bond.json, each checked: a bad entry is dropped, never fatal."""
        bond = _typed(d, "bond", dict)
        for k in ("hours", "last_seen", "first_seen"):
            self.bond[k] = as_float(bond.get(k), 0.0, finite=True, bool_ok=False)
        for k in ("sessions", "asides", "replied"):
            self.bond[k] = as_int(bond.get(k), 0, bool_ok=False)
        acts = _typed(bond, "by_activity", dict)
        for a, r in acts.items():
            if isinstance(a, str) and isinstance(r, list) and len(r) == 2:
                self.bond["by_activity"][a] = [as_int(r[0], 0, bool_ok=False), as_int(r[1], 0, bool_ok=False)]
        diary = _typed(d, "diary", list)
        self.diary = []
        for x in diary:
            if not isinstance(x, dict) or not isinstance(x.get("text"), str) or _when(x.get("t")) is None:
                continue
            row = {"t": _when(x["t"]), "kind": str(x.get("kind") or "moment"), "text": x["text"][:200],
                   "session": as_int(x.get("session"), 0, bool_ok=False)}
            if isinstance(x.get("zone"), str):
                row["zone"] = x["zone"]
            self.diary.append(row)
        del self.diary[:-MAX_DIARY]
        chars = _typed(d, "characters", dict)
        for k, v in chars.items():
            if isinstance(v, dict) and any(f in v for f in ("tts_voice", "ties")):
                self._tidied = True         # (fields only an older version kept)
                # a second character an older version kept beside the one played (a voice of its own) and never
                # played itself (not a line of its own): not one the coach played
                if v.get("tts_voice") and not (v.get("lines") or v.get("favorites") or v.get("scene")):
                    continue
            if isinstance(v, dict) and isinstance(v.get("name"), str) and isinstance(v.get("brief"), str) \
                    and v["name"].strip() and v["brief"].strip():
                lore = _typed(v, "lore", list)
                ch = self.characters[self._key(v["name"]) or str(k)] = {
                    "name": v["name"][:60], "brief": v["brief"], "voice": v.get("voice") if isinstance(
                        v.get("voice"), str) else "", "lore": [x for x in lore if isinstance(x, str)][-MAX_LORE:],
                    "last": as_float(v.get("last"), 0.0, finite=True, bool_ok=False),
                    "role": v.get("role") if isinstance(v.get("role"), str) else "",
                    "scene": v.get("scene") if isinstance(v.get("scene"), str) else "",
                    "scene_t": as_float(v.get("scene_t"), 0.0, finite=True, bool_ok=False)}
                for field, cap in CHARACTER_LISTS.items():
                    rows = [x for x in _typed(v, field, list) if isinstance(x, str)][-cap:]
                    own = [y for y in (_own_words(x) for x in rows) if y]
                    self._tidied = self._tidied or own != rows
                    ch[field] = own
        zones = _typed(d, "zones", list)
        self.zones = {z for z in zones if isinstance(z, str)} | {x["zone"] for x in self.diary if "zone" in x}
        for x in _typed(d, "chapters", list):
            if isinstance(x, dict) and isinstance(x.get("text"), str) and x["text"].strip():
                self.chapters.append({"t0": as_float(x.get("t0"), 0.0, finite=True, bool_ok=False),
                                      "t1": as_float(x.get("t1"), 0.0, finite=True, bool_ok=False),
                                      "text": x["text"][:1200], "topics": [t for t in _typed(x, "topics", list)
                                                                           if isinstance(t, str)][:12]})
        for x in _typed(d, "eras", list):
            if isinstance(x, dict) and isinstance(x.get("text"), str) and x["text"].strip():
                self.eras.append({"t0": as_float(x.get("t0"), 0.0, finite=True, bool_ok=False),
                                  "t1": as_float(x.get("t1"), 0.0, finite=True, bool_ok=False),
                                  "text": x["text"][:1200], "topics": [t for t in _typed(x, "topics", list)
                                                                       if isinstance(t, str)][:24]})
        for x in _typed(d, "threads", list):
            if isinstance(x, dict) and isinstance(x.get("text"), str) and isinstance(x.get("id"), str):
                self.threads.append({"id": x["id"], "text": x["text"][:200],
                                     "t": as_float(x.get("t"), 0.0, finite=True, bool_ok=False)})
        self.next_thread = max([as_int(d.get("next_thread"), 1, bool_ok=False)] +
                               [as_int(x["id"][1:], 0, bool_ok=False) + 1 for x in self.threads])
        self.cleared_t = as_float(d.get("cleared_t"), 0.0, finite=True, bool_ok=False)
        for x in _typed(d, "convo", list):
            if isinstance(x, dict) and x.get("who") in ("player", "coach") and isinstance(x.get("text"), str) \
                    and isinstance(x.get("kind"), str) and _when(x.get("t")) is not None \
                    and x["t"] > self.cleared_t:
                text = _own_words(x["text"]) if x["who"] == "coach" else x["text"]
                if text != x["text"]:
                    self._tidied = True
                    if not text:
                        continue
                row = {"t": _when(x["t"]), "who": x["who"], "kind": x["kind"], "text": text}
                if x.get("chat") is True:
                    row["chat"] = True
                self.convo.append(row)
        del self.convo[:-KEEP_CONVO]
        self._bond_extra = {k: v for k, v in d.items() if k not in BOND_KEYS}

    def _save(self):
        """coach_memory.json: the notes ("rapport" ones as facts with "rapport": true, so older engines keep them),
        then the notes and keys this engine doesn't know, as they were."""
        self.version += 1
        self._recall_texts = None
        if not self.path:
            return
        notes = [dict(n, kind="fact", rapport=True) if n["kind"] == "rapport" else n for n in self.notes]
        try:
            jsonfile.write_atomic(self.path, dict(self._extra, notes=notes + self._foreign, next_id=self.next_id,
                                                  learning=self.learning, last_reflect=self.last_reflect),
                                  ensure_ascii=False, indent=1)
            self._backup(self.path)
        except OSError as e:
            log("memory", "could not save %s: %s" % (self.path, e))

    def _save_bond(self):
        """coach_bond.json: the bond, the diary, the zones and the characters."""
        self.version += 1
        if not self.bond_path:
            return
        try:
            jsonfile.write_atomic(self.bond_path, dict(
                self._bond_extra, bond=self.bond, diary=self.diary, zones=sorted(self.zones), characters=self.characters,
                chapters=self.chapters, eras=self.eras, threads=self.threads, next_thread=self.next_thread,
                convo=self.convo, cleared_t=self.cleared_t), ensure_ascii=False, indent=1)
            self._backup(self.bond_path)
        except OSError as e:
            log("memory", "could not save %s: %s" % (self.bond_path, e))

    @staticmethod
    def _backup(path):
        """One dated copy a day (coach_memory.YYYYMMDD.json, coach_bond.YYYYMMDD.json), the newest BACKUPS kept."""
        folder, base = os.path.dirname(path), os.path.splitext(os.path.basename(path))[0]
        day = os.path.join(folder, "%s.%s.json" % (base, time.strftime("%Y%m%d")))
        if not os.path.exists(day):
            try:
                with open(path, "rb") as src, open(day, "wb") as dst:
                    dst.write(src.read())
            except OSError:
                return
        olds = sorted(f for f in os.listdir(folder) if re.fullmatch(re.escape(base) + r"\.\d{8}\.json", f))
        for f in olds[:-BACKUPS]:
            try:
                os.remove(os.path.join(folder, f))
            except OSError:
                pass

    # ---- changes ----------------------------------------------------------------------------------------------
    def add(self, text, kind=None, source="player", pinned=False, now=None):
        """Add a note, or refresh the matching one. Returns (note, created: bool), or (None, False) for empty text."""
        text = " ".join(str(text or "").split())[:MAX_TEXT].strip()
        if not text:
            return None, False
        kind = kind if kind in KINDS else guess_kind(text)
        now = time.time() if now is None else now
        with self.lock:
            for n in self.notes:
                if n["kind"] == kind and _similar(n["text"], text):
                    n["text"], n["updated"] = text, now
                    n["pinned"] = n.get("pinned") or pinned
                    self._save()
                    return dict(n), False
            n = {"id": self.next_id, "kind": kind, "text": text, "source": source, "created": now, "updated": now,
                 "pinned": bool(pinned), "used": 0}
            self.next_id += 1
            self.notes.append(n)
            if len(self.notes) > MAX_NOTES:
                unpinned = sorted((x for x in self.notes if not x.get("pinned")), key=lambda x: x["updated"])
                drop = {x["id"] for x in unpinned[:len(self.notes) - MAX_NOTES]}
                self.notes = [x for x in self.notes if x["id"] not in drop]
            self._save()
            return dict(n), True

    def update(self, note_id, text=None, kind=None, pinned=None):
        with self.lock:
            for n in self.notes:
                if n["id"] == int(note_id):
                    if text is not None and " ".join(str(text).split()):
                        n["text"] = " ".join(str(text).split())[:MAX_TEXT]
                    if kind in KINDS:
                        n["kind"] = kind
                    if pinned is not None:
                        n["pinned"] = bool(pinned)
                    n["updated"] = time.time()
                    self._save()
                    return dict(n)
        return None

    def remove(self, which):
        """Remove by id, by words of the note ("the gear one"), or "all" (pinned notes too). -> removed notes."""
        with self.lock:
            if isinstance(which, int) or (isinstance(which, str) and which.strip().isdigit()):
                gone = [n for n in self.notes if n["id"] == int(which)]
            elif str(which).strip().lower() in ("all", "everything"):
                gone = list(self.notes)
            else:
                words = [w for w in _norm(which).split() if len(w) > 2 and w not in ("the", "that", "about", "one", "note")]
                gone = [n for n in self.notes if words and all(w in _norm(n["text"]) for w in words)]
            if gone:
                ids = {n["id"] for n in gone}
                self.notes = [n for n in self.notes if n["id"] not in ids]
                self._save()
            if str(which).strip().lower() in ("all", "everything"):      # and the history together, all of it
                self.bond = {"hours": 0.0, "sessions": 0, "asides": 0, "replied": 0, "last_seen": 0.0,
                             "by_activity": {}}
                self.bond["first_seen"] = 0.0
                self.diary, self.zones, self.characters = [], set(), {}
                self.chapters, self.eras, self.threads, self.convo = [], [], [], []
                self._recall_texts = None
                self.cleared_t = time.time()
                self._save_bond()
            return gone

    def set_learning(self, on):
        with self.lock:
            self.learning = bool(on)
            self._save()

    def mark_reflected(self, now=None):
        with self.lock:
            self.last_reflect = time.time() if now is None else now
            self._save()

    # ---- reading ----------------------------------------------------------------------------------------------
    def recall_texts(self):
        """Eligible texts for cheap recall observation (same notes, moments and story as recall.items).

        The immutable snapshot is shared until a relevant mutation; no history copies on each state-stream tick.
        """
        with self.lock:
            if self._recall_texts is None:
                texts = [n["text"] for n in self.notes if n["kind"] in ("rapport", "fact", "goal")]
                texts.extend(x["text"] for rows in (self.diary, self.eras, self.chapters) for x in rows)
                self._recall_texts = tuple(dict.fromkeys(t for text in texts if (t := " ".join(text.split()))))
            return self._recall_texts

    def list(self):
        with self.lock:
            return [dict(n) for n in sorted(self.notes, key=lambda n: (KINDS.index(n["kind"]), -n["updated"]))]

    def snapshot(self):
        """For the app's Memory page."""
        return {"notes": self.list(), "learning": self.learning, "last_reflect": self.last_reflect or None,
                "kinds": [{"id": k, "label": KIND_LABEL[k]} for k in KINDS], "persistent": bool(self.path)}

    def block(self):
        """The prompt section: pinned notes first, then each kind's newest notes, within BLOCK_CHARS. '' when empty."""
        with self.lock:
            notes = list(self.notes)
        if not notes:
            return ""
        out, size = [], 0
        for kind in KINDS:
            rows = sorted((n for n in notes if n["kind"] == kind), key=lambda n: (not n.get("pinned"), -n["updated"]))
            rows = rows[:PER_KIND[kind]]
            if not rows:
                continue
            lines = ["%s:" % KIND_LABEL[kind]] + ["- [m%d] %s" % (n["id"], n["text"]) for n in rows]
            chunk = "\n".join(lines)
            if size + len(chunk) > BLOCK_CHARS:
                break
            out.append(chunk)
            size += len(chunk)
        return ("WHAT YOU REMEMBER ABOUT THIS PLAYER (learned in earlier sessions; ids in brackets):\n" +
                "\n".join(out) +
                "\nFollow their preferences and standing requests in every callout and answer, use goals and facts to "
                "tailor advice, and never repeat a corrected mistake. Don't recite this list unless they ask what you "
                "remember.")

    # ---- the bond: time together and how the asides went (banter.py) ----------------------------------------------
    def bond_add(self, hours=0.0, sessions=0, asides=0, replied=0, activity=None):
        """Count time played together and asides (and, with `activity`, per what the player was doing then); saved at
        once for a session, aside or reply, the clock every BOND_SAVE_S."""
        with self.lock:
            b = self.bond
            b["hours"] = round(b["hours"] + hours, 4)
            b["sessions"] += sessions
            b["asides"] += asides
            b["replied"] += replied
            if hours:
                b["last_seen"] = time.time()
                b["first_seen"] = b["first_seen"] or time.time()
            if activity and (asides or replied):
                row = b["by_activity"].setdefault(activity, [0, 0])
                row[0] += asides
                row[1] += replied
            if sessions or asides or replied or time.time() - self._bond_saved >= BOND_SAVE_S:
                self._bond_saved = time.time()
                self._save_bond()

    def bond_stage(self):
        """'new', 'getting to know each other', 'familiar' or 'old friends', from the hours played together."""
        h = self.bond["hours"]
        return next(name for top, name in BOND_STAGES if top is None or h < top)

    def bond_line(self):
        """The bond for the aside request: time together, how the asides landed, the stage."""
        b = self.bond
        return ("You and this player: %.0f hours played together over %d sessions; %d of your %d asides got a reply. "
                "Your bond: %s." % (b["hours"], max(1, b["sessions"]), b["replied"], b["asides"], self.bond_stage()))

    def reply_rate(self, activity, least=4):
        """How often the player answers an aside while doing `activity` (0..1), or None before `least` asides."""
        row = self.bond["by_activity"].get(activity) or [0, 0]
        return row[1] / float(row[0]) if row[0] >= least else None

    def activity_line(self):
        """When they answer the asides: 'flying 4 of 5, questing 1 of 9', or ''."""
        rows = sorted(self.bond["by_activity"].items(), key=lambda kv: -kv[1][0])
        return ", ".join("%s %d of %d" % (k, v[1], v[0]) for k, v in rows if v[0])

    # ---- the diary of shared moments (banter.py) --------------------------------------------------------------
    def diary_add(self, kind, text, now=None):
        """A moment worth remembering together (a death, a ding, a close call, a new zone): kept, newest MAX_DIARY."""
        with self.lock:
            self.diary.append({"t": time.time() if now is None else now, "kind": kind,
                               "text": " ".join(str(text).split())[:200], "session": self.bond["sessions"]})
            del self.diary[:-MAX_DIARY]
            self._recall_texts = None
            self._save_bond()

    def diary_since(self, t):
        with self.lock:
            return [dict(x) for x in self.diary if x["t"] > t]

    def diary_recent(self, n=8):
        with self.lock:
            return [dict(x) for x in self.diary[-n:]]

    def zones_seen(self):
        """The zones visited together."""
        with self.lock:
            return set(self.zones)

    def zone_seen(self, zone, now=None):
        """Note a zone first visited together (a diary moment of its own). -> True the first time."""
        if not zone:
            return False
        with self.lock:
            if zone in self.zones:
                return False
            self.zones.add(zone)
            self.diary.append({"t": time.time() if now is None else now, "kind": "zone", "zone": zone,
                               "text": "First time in %s together" % zone, "session": self.bond["sessions"]})
            del self.diary[:-MAX_DIARY]
            self._recall_texts = None
            self._save_bond()
        return True

    # ---- the characters played for them (persona.py) ----------------------------------------------------------
    @staticmethod
    def _key(name):
        """A character's key: its name casefolded, letters (any script), digits and spaces only."""
        return " ".join(re.sub(r"[^\w ]", "", str(name or "").casefold()).replace("_", " ").split())[:40]

    def character_save(self, name, brief, voice=""):
        """Keep the character being played (by name) so "bring back <name>" restores it, its lore and all."""
        key = self._key(name)
        if not key or not brief:
            return None
        with self.lock:
            ch = self.characters.setdefault(key, {"name": name, "lore": []})
            ch.update({"name": name, "brief": brief, "voice": voice or ch.get("voice") or "", "last": time.time()})
            if len(self.characters) > MAX_CHARACTERS:
                for k in sorted(self.characters, key=lambda k: self.characters[k].get("last") or 0)[:-MAX_CHARACTERS]:
                    del self.characters[k]
            self._save_bond()
            return dict(ch)

    def character(self, name):
        """A saved character by name (exact, else the one whose name contains it), or None."""
        key = self._key(name)
        if not key:
            return None
        with self.lock:
            ch = self.characters.get(key) or (next((v for k, v in self.characters.items()
                                                     if key in k or (len(k) >= 3 and k in key)), None)
                                               if len(key) >= 3 else None)
            return dict(ch, lore=list(ch.get("lore") or ())) if ch else None

    # ---- the app's Memory page: the characters, to read, change and forget -------------------------------------
    def characters_view(self):
        """The characters kept, most recently played first: who each is, its voice, who the player is to it, its
        scene, its past (lore) and its own story threads."""
        with self.lock:
            rows = sorted(self.characters.values(), key=lambda v: -(v.get("last") or 0))
            return [{"name": v["name"], "brief": v.get("brief") or "", "voice": v.get("voice") or "",
                     "role": v.get("role") or "", "scene": v.get("scene") or "", "lore": list(v.get("lore") or ()),
                     "plot": list(v.get("plot") or ()), "last": v.get("last") or 0} for v in rows]

    def same_character(self, a, b):
        """Whether two names are one kept character's: exactly (its key), never the loose match of character()."""
        return bool(self._key(a)) and self._key(a) == self._key(b)

    def character_edit(self, name, field, text, index=None, was=None):
        """The player's change on the Memory page. field "brief" (never empty), "voice", "role", "scene": its text
        ("" clears it); "lore" (its past) or "plot" (its story threads): the item at index, the one that read `was`
        when the page was drawn (found by it if the list moved since; refused if it's gone) (text "" removes it;
        index None adds one, unless one like it is there). -> whether it changed."""
        text = " ".join(str(text or "").split())
        key = self._key(name)
        with self.lock:
            ch = self.characters.get(key)
            if ch is None:
                raise ValueError("no character called %s" % name)
            if field in ("brief", "voice", "role", "scene"):
                if field == "brief" and not text:
                    raise ValueError("a character needs a description")
                value = text[:200 if field == "role" else 600]
                if (ch.get(field) or "") == value:
                    return False
                ch[field] = value
                if field == "scene":
                    ch["scene_t"] = time.time() if value else 0.0
            elif field in ("lore", "plot"):
                rows = ch.setdefault(field, [])
                text = text[:200 if field == "lore" else 300]
                if index is None:
                    if not text or any(_similar(text, y) for y in rows):
                        return False
                    rows.append(text)
                    cap = MAX_LORE if field == "lore" else CHARACTER_LISTS["plot"]
                    while len(rows) > cap:
                        del rows[LORE_KEPT if field == "lore" else 0]
                else:
                    i = int(index)
                    if was is not None and not (0 <= i < len(rows) and rows[i] == was):
                        i = rows.index(was) if was in rows else -1      # (the coach added to it since)
                    if not 0 <= i < len(rows):
                        raise ValueError("it changed meanwhile: here it is as it is now")
                    if text:
                        rows[i] = text
                    else:
                        del rows[i]
            else:
                raise ValueError("no character field %r" % field)
            self._save_bond()
            return True

    def character_forget(self, name):
        """Forget a character (its past and all). -> whether there was one."""
        with self.lock:
            if self.characters.pop(self._key(name), None) is None:
                return False
            self._save_bond()
            return True

    def character_lore(self, name, fact):
        """A fact the character made up about itself: kept so it never contradicts itself."""
        key = self._key(name)
        fact = " ".join(str(fact or "").split())[:200]
        with self.lock:
            ch = self.characters.get(key)
            if ch is None or not fact or any(_similar(fact, x) for x in ch.get("lore") or ()):
                return False
            lore = ch.setdefault("lore", [])
            lore.append(fact)
            while len(lore) > MAX_LORE:
                del lore[LORE_KEPT]                 # the oldest after the first ones (who it is stays)
            self._save_bond()
            return True

    def character_add(self, name, field, items):
        """Add to one of a character's lists (CHARACTER_LISTS: sample lines, favourite lines, its story threads, its
        recent lines), keeping the newest; or set its "role" (who the player is to it) or "scene" (the scene being
        played; "" ends it): one like the one kept already changes nothing. -> whether it changed."""
        if field not in ("role", "scene") and field not in CHARACTER_LISTS:
            raise ValueError("no character field %r" % field)
        key = self._key(name)
        with self.lock:
            ch = self.characters.get(key)
            if ch is None:
                return False
            if field in ("role", "scene"):
                value = " ".join(str(items or "").split())[:600 if field == "scene" else 200]
                old = ch.get(field) or ""
                if value == old or (value and old and _similar(value, old)):
                    if field == "scene" and value:
                        ch["scene_t"] = time.time()     # said again: still being played
                    return False
                ch[field] = value
                if field == "scene":
                    ch["scene_t"] = time.time()
            else:
                rows = ch.setdefault(field, [])
                new = [" ".join(str(x).split())[:300] for x in (items if isinstance(items, (list, tuple)) else [items])]
                new = [x for x in new if x and (field == "lines" or not any(_similar(x, y) for y in rows))]
                if not new:
                    return False
                rows.extend(new)
                del rows[:-CHARACTER_LISTS[field]]
            if field != "lines":                    # its recent lines are saved with the next other change
                self._save_bond()
            return True

    # ---- the story so far: chapters, eras, threads, the conversation ----------------------------------------
    def chapter_add(self, text, topics=(), t0=None, t1=None):
        """A chapter of the time together (written at reflection). Past MAX_CHAPTERS the oldest FOLD fold into an
        era: their texts' first sentences and their topics."""
        text = " ".join(str(text or "").split())[:1200]
        if not text:
            return None
        now = time.time()
        row = {"t0": float(t0 if t0 is not None else now), "t1": float(t1 if t1 is not None else now), "text": text,
               "topics": [" ".join(str(t).split())[:60] for t in topics or () if str(t).strip()][:12]}
        with self.lock:
            self.chapters.append(row)
            if len(self.chapters) > MAX_CHAPTERS:
                old, self.chapters = self.chapters[:FOLD], self.chapters[FOLD:]
                share = 1200 // len(old) - 1
                firsts = [re.split(r"(?<=[.!?])\s", c["text"], maxsplit=1)[0][:share] for c in old]
                topics = []
                for i in range(max((len(c["topics"]) for c in old), default=0)):     # round-robin: each its share
                    topics += [c["topics"][i] for c in old if i < len(c["topics"])]
                self.eras.append({"t0": old[0]["t0"], "t1": old[-1]["t1"], "text": " ".join(firsts)[:1200],
                                  "topics": list(dict.fromkeys(topics))[:24]})
                del self.eras[:-MAX_ERAS]
            self._recall_texts = None
            self._save_bond()
        return dict(row)

    def chapters_recent(self, n=3):
        with self.lock:
            return [dict(x) for x in self.chapters[-n:]]

    def story(self):
        """Every chapter and era (oldest first), for recall and the road-so-far recap."""
        with self.lock:
            return [dict(x, era=True) for x in self.eras] + [dict(x) for x in self.chapters]

    def thread_add(self, text, now=None):
        """An open thread between you (a promise, a question left hanging, a plan): kept until closed; the oldest
        goes past MAX_THREADS. -> its id ("t7"), or None (empty, or one like it is open)."""
        text = " ".join(str(text or "").split())[:200]
        with self.lock:
            if not text or any(_similar(text, x["text"]) for x in self.threads):
                return None
            tid = "t%d" % self.next_thread
            self.next_thread += 1
            self.threads.append({"id": tid, "text": text, "t": time.time() if now is None else now})
            del self.threads[:-MAX_THREADS]
            self._save_bond()
            return tid

    def thread_done(self, ids):
        """Close threads by id ("t7"). -> the texts closed."""
        ids = {str(i).strip().lower() for i in ids or ()}
        with self.lock:
            gone = [x["text"] for x in self.threads if x["id"].lower() in ids]
            if gone:
                self.threads = [x for x in self.threads if x["id"].lower() not in ids]
                self._save_bond()
            return gone

    def threads_open(self):
        with self.lock:
            return [dict(x) for x in self.threads]

    def convo_keep(self, entries):
        """The conversation's last lines, kept across restarts (Coach restores them at start)."""
        with self.lock:
            rows = [{k: e[k] for k in ("t", "who", "kind", "text", "chat") if k in e}
                    for e in list(entries) if e.get("t", 0) > self.cleared_t][-KEEP_CONVO:]
            if rows == self.convo:
                return False
            self.convo = rows
            self._save_bond()
            return True

    def convo_kept(self):
        with self.lock:
            return [dict(x) for x in self.convo]

    def characters_block(self):
        """The prompt's list of characters played before ('' when none): what "bring back <name>" can recall."""
        with self.lock:
            rows = sorted(self.characters.values(), key=lambda v: -(v.get("last") or 0))
        if not rows:
            return ""
        return "CHARACTERS YOU HAVE PLAYED FOR THIS PLAYER (bring one back with PERSONA: recall <name>):\n" + "\n".join(
            "- %s: %s" % (v["name"], v["brief"][:160]) for v in rows)

    def mentioned(self, ids):
        """Count notes the coach cited (used), for the Memory page."""
        with self.lock:
            hit = False
            for n in self.notes:
                if n["id"] in ids:
                    n["used"] = int(n.get("used") or 0) + 1
                    hit = True
            if hit:
                self._save()


# ---- the player's own words ---------------------------------------------------------------------------------------
def parse_command(question):
    """Clear memory requests in the player's words -> ("add", kind, text) / ("forget", which) / ("list",) / None.
    'remember to <do something>' is a reminder, not a memory, and is left alone."""
    q = " ".join((question or "").split()).strip(" .!?")
    ql = q.lower()
    if re.fullmatch(r"(?:what do you (?:remember|know) about me|what have you learned(?: about me)?|"
                    r"what do you remember|(?:show|list|read) (?:me )?(?:your|my) (?:memory|memories|notes))", ql):
        return ("list",)
    if "reminder" in ql:
        return None
    m = re.fullmatch(r"(?:please )?(?:forget|stop remembering|delete the note|remove the note)(?: (?:that|about))? (.+)", q, re.I)
    if m and not re.match(r"(?:it|about it)$", m.group(1), re.I):
        return ("forget", m.group(1).strip())
    m = re.fullmatch(r"(?:please |coach,? )?(?:remember|note|keep in mind|don'?t forget)(?: that|:)? (.+)", q, re.I)
    if m and not re.match(r"to\s", m.group(1), re.I):
        text = m.group(1).strip()
        return ("add", guess_kind(text), text[0].upper() + text[1:])
    m = re.fullmatch(r"(?:from now on|going forward|in the future)[, ]+(.+)", q, re.I)
    if m:
        text = m.group(1).strip()
        kind = "preference" if re.search(r"\b(don'?t|do not|never|stop|less|more|shorter|longer|only)\b", text, re.I) \
            else "request"
        return ("add", kind, text[0].upper() + text[1:])
    return None
