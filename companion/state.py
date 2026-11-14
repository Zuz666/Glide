"""Live game state assembled from the Glide addon's strip frames, plus derived metrics."""
import os
import sys
import time
from collections import deque
from collections.abc import Callable

import strip_spec
from protocol import list_hash
from log import log

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if ROOT not in sys.path:
    sys.path.append(ROOT)       # the knowledge package: its values module (stdlib only) reads the strip's numbers
from knowledge.values import as_int  # noqa: E402


class GameState:
    XP_WINDOW = 900                # seconds of XP history behind xp_per_hour
    FRESH_SECONDS = 2
    STEP_MATCH_REASONS = {
        "missing": "Local source step unavailable",
        "mismatch": "Local source step differs",
        "unverified": "Source step identity unverified",
        "stale": "Game data is stale",
    }

    def __init__(self):
        self.core = {}
        self.pages = {}            # page id -> dict
        self._step_context = ((None, None, None, None), {})  # immutable pointer/class cohort: S pages + player evidence
        self.step_lookup: Callable[[dict], dict] | None = None
        self.quest_chunks = {}     # chunk index -> raw text
        self.quests = {}           # qid -> {"done": bool, "obj": [(have, need)]}
        # ring events are new when their id is above the highest seen; the addon counts from 1 again after a UI
        # reload (or relog), which shows as a ring whose ids are all below that (None = nothing seen yet)
        self.event_max = None
        self.events_missed = 0     # events that left the ring between two frames (ids skipped)
        self.addon_restarts = 0
        self.frames = 0
        self.last_frame = 0.0
        self.started = time.time()
        self.xp_gained = 0         # session total (across level-ups)
        self._prev_xp = None       # (level, xp, xpmax)
        self._xp_win = deque()     # (t, xp_gained) over the last XP_WINDOW seconds; only update() touches it
        self._xph = 0              # XP per hour over that window, worked out as each frame arrives
        self.step_since = time.time()
        self.step = None
        self.pos_hist = deque(maxlen=600)   # (t, map, x, y)
        self.buff_chunks = {}
        self.gear_chunks = {"W": {}, "I": {}, "R": {}}
        self.gear = {"W": [], "I": [], "R": []}      # worn, bags, quest reward choices
        self.gear_version = {"W": 0, "I": 0, "R": 0}
        self.buffs = {}            # key -> {"name", "expiry", "duration", "mine"}
        self.buffs_version = 0     # bumps whenever a complete, verified buff list arrives
        self.buff_override = {}    # key -> buff refreshed by a cast event while the aura list is frozen
        self.lost_buffs = {}       # key -> last info of buffs that left the list (to restore on recast)
        self.list_chunks = {"P": {}, "F": {}, "C": {}, "N": {}, "V": {}, "D": {}}   # list pages: letter -> idx -> page dict
        self.lists = {}            # letter -> {"items": [...], "page": first chunk's page} once complete and hashed
        self.lists_version = 0
        self.gauges = None         # measured from the strip's StatusBars (secret values)
        self.map_sizes = {}        # ui_map -> (width, height) in yards, from the addon
        self._scale = {}           # ui_map -> list of estimated heights (yards) from movement

    # -------------------------------------------------------------------------------------------
    def update(self, core, page, now=None):
        """Apply one decoded frame. Returns list of new ring events [(id, kind, arg)]."""
        now = now or time.time()
        prev = self.core
        restarts = self.addon_restarts
        ring = self._ring(core)
        reset = (self.addon_restarts != restarts
                 or (self.frames > 0 and now - self.last_frame >= self.FRESH_SECONDS)
                 or (bool(prev) and prev.get("cl") != core.get("cl")))
        pointer = (core.get("sh"), core.get("si"), core.get("st"), core.get("cl"))
        previous, step_pages = self._step_context
        if reset or pointer != previous:
            step_pages = {}
            # Revoke old evidence before publishing a new core, even when a restart repeats the same pointer.
            self._step_context = (pointer, step_pages)
            if reset or "sh" in core or "si" in core:
                for pid in ("S0", "S1", "S2"):
                    self.pages.pop(pid, None)
        if reset:
            self.pages.pop("B", None)
        self.core = core
        self.frames += 1
        self.last_frame = now
        if (prev.get("qn"), prev.get("qh")) != (core.get("qn"), core.get("qh")):
            self._rebuild_quests()          # the log changed: the chunks held may already make it up (a shorter log)
        if page.get("pg"):
            pid = page["pg"]
            if pid.startswith("Q"):
                self.quest_chunks[as_int(pid[1:], 0)] = page.get("q", "")
                self._rebuild_quests()
            elif pid[:1] in ("W", "I", "R") and pid[1:].isdigit():
                self.gear_chunks[pid[0]][int(pid[1:])] = page.get("g", "")
                self._rebuild_gear(pid[0])
            elif pid[:1] in self.list_chunks and pid[1:].isdigit():
                self._list_chunk(pid[0], int(pid[1:]), page)
            elif pid.startswith("A") and pid[1:].isdigit():
                self.buff_chunks[int(pid[1:])] = page.get("a", "")
                self._rebuild_buffs()
            else:
                self.pages[pid] = page
                if pid == "Z" and page.get("mm") and page.get("mw") and page.get("mh"):
                    try:
                        self.map_sizes[int(page["mm"])] = (float(page["mw"]), float(page["mh"]))
                    except ValueError:
                        pass
        if page.get("pg") in ("B", "S0", "S1", "S2"):
            step_pages = dict(step_pages)
            step_pages[page["pg"]] = dict(page)
        self._step_context = (pointer, step_pages)
        if core.get("rn") == "0" and self.gear["R"]:
            self._rebuild_gear("R")
        self._track_xp(now)
        self._track_step(now)
        mp, px, py = as_int(core.get("mp")), as_int(core.get("px")), as_int(core.get("py"))
        if mp and px is not None and py is not None:
            self._estimate_scale(now, mp, px / 100.0, py / 100.0)
            self.pos_hist.append((now, mp, px / 100.0, py / 100.0))
        new = []
        for i, kind, arg in ring:
            if self.event_max is None or i > self.event_max:
                new.append((i, kind, arg))
                if kind == "cst":
                    self._buff_cast(arg)
        if new:
            if self.event_max is not None and new[0][0] > self.event_max + 1:
                lost = new[0][0] - self.event_max - 1
                self.events_missed += lost
                log("link", "%d game event%s lost between frames (ids %d-%d)" % (
                    lost, "" if lost == 1 else "s", self.event_max + 1, new[0][0] - 1))
            self.event_max = new[-1][0]
        return new

    def _ring(self, core):
        """The frame's ring events, oldest first: [(id, kind, arg)]. A ring that is all below the highest id seen
        means the addon started counting again (UI reload, relog): every event in it is new."""
        ring = []
        for k in ("e3", "e2", "e1"):
            parts = (core.get(k) or "").split(":", 2)
            i = as_int(parts[0])
            if len(parts) == 3 and i is not None:
                ring.append((i, parts[1], parts[2]))
        if ring and self.event_max is not None and max(i for i, _, _ in ring) < self.event_max:
            self.addon_restarts += 1
            self.event_max = 0
            log("link", "the addon restarted (UI reload or relog): its event ids start over")
        return ring

    def _buff_cast(self, key):
        """The player cast one of their buffs (addon event). Forever freezes the aura list in combat,
        so assume a fresh full-duration buff until a list confirms it."""
        info = self.buffs.get(key) or self.lost_buffs.get(key)
        if not info or not info.get("duration"):
            return
        t = as_int(self.core.get("t"), 0)
        self.buff_override[key] = dict(info, expiry=(t + info["duration"]) % 1000000, cast_t=t)
        self.buffs_version += 1

    def _estimate_scale(self, now, mp, x, y):
        """Fallback when the addon can't report map size: running speed (yd/s) vs map % moved.
        Assumes the zone map's usual 3:2 width:height ratio."""
        if mp in self.map_sizes or not self.pos_hist:
            return
        t0, m0, x0, y0 = self.pos_hist[-1]
        sp = as_int(self.core.get("sp"), 0) / 10.0
        dt = now - t0
        if m0 != mp or not (0.15 < dt < 2) or sp < 3 or self.core.get("tx") == "1":
            return
        dx, dy = x - x0, y - y0
        d = (2.25 * dx * dx + dy * dy) ** 0.5
        if d < 0.05:
            return
        est = self._scale.setdefault(mp, [])
        est.append(100.0 * sp * dt / d)
        del est[:-60]

    def gauges_valid(self):
        v = self.core.get("gv") or ""
        try:
            return self.gauges is not None and tuple(int(x) for x in v.split(".")[:2]) >= (0, 6)
        except ValueError:
            return False

    def _gauge(self, core_key, gauge_key):
        v = as_int(self.core.get(core_key))
        if v is not None and v >= 0:
            return v
        bit = {"hp": 1, "power": 2, "target_hp": 4}[gauge_key]
        if self.gauges_valid() and (as_int(self.core.get("gk"), 0) or 0) & bit:
            return self.gauges.get(gauge_key)
        return v

    def map_size(self, mp):
        if mp in self.map_sizes:
            return self.map_sizes[mp]
        est = self._scale.get(mp)
        if est and len(est) >= 8:
            h = sorted(est)[len(est) // 2]
            return (1.5 * h, h)
        return None

    def player_geo(self):
        if not self.pos_hist:
            return None
        _, mp, x, y = self.pos_hist[-1]
        f = as_int(self.core.get("f"))
        return {"map": mp, "x": x, "y": y, "facing": f / 100.0 if f is not None else None,
                "map_size": self.map_size(mp)}

    def _rebuild_buffs(self):
        """Accept the buff list only when all chunks are present and match the addon's hash."""
        n = as_int(self.core.get("an"), 0) or 0
        if n == 0 or any(i not in self.buff_chunks for i in range(n)):
            return
        joined = ",".join(self.buff_chunks[i] for i in range(n))
        if self._hash(joined) != as_int(self.core.get("ah"), -1):
            return
        buffs = {}
        joined_flags = "".join(e.split(":", 4)[3] for e in joined.split(",") if e.count(":") >= 4)
        for e in joined.split(","):
            p = e.split(":", 4)
            if len(p) == 5:
                # flags: m = cast by the player, k = the player can cast it (0.6.2+; before that, m only)
                b = {"name": p[4], "expiry": as_int(p[1], 0), "duration": as_int(p[2], 0), "mine": "m" in p[3],
                     "castable": ("k" in p[3]) if "k" in joined_flags else ("m" in p[3])}
                old = self.buffs.get(p[0])
                if old and old["name"] == b["name"] and b["expiry"] and abs(self._tdiff(b["expiry"], old["expiry"])) <= 2:
                    b["expiry"] = old["expiry"]          # the addon rounds to 2 s; ignore the flicker
                buffs[p[0]] = b
        fresh = self.core.get("ak") != "1" and self.core.get("c") != "1"
        t = as_int(self.core.get("t"), 0)
        for k, o in list(self.buff_override.items()):
            b = buffs.get(k)
            if b and (not b["expiry"] or self._tdiff(b["expiry"], o["expiry"]) >= -4):
                del self.buff_override[k]                  # the list caught up with the recast
            elif self._tdiff(t, o["expiry"]) > 0 or (fresh and not b and self._tdiff(t, o["cast_t"]) > 4):
                del self.buff_override[k]                  # expired, or a fresh list says it's gone
        if buffs != self.buffs:
            for k, b in self.buffs.items():
                if k not in buffs:
                    self.lost_buffs[k] = b
            self.buffs = buffs
            self.buffs_version += 1

    LIST_KEYS = {"P": "p", "F": "f", "C": "c", "N": "t", "V": "v", "D": "d"}

    def _list_chunk(self, letter, idx, page):
        """List pages carry their own chunk count n and hash h: complete when chunks 0..n-1 share h and the joined
        text hashes to it."""
        chunks = self.list_chunks[letter]
        h, n = as_int(page.get("h")), as_int(page.get("n"), 0) or 0
        for k in [k for k, v in chunks.items() if as_int(v.get("h")) != h or k >= n]:
            del chunks[k]
        chunks[idx] = page
        if n == 0 or any(i not in chunks for i in range(n)):
            return
        key = self.LIST_KEYS[letter]
        joined = ",".join(chunks[i].get(key, "") for i in range(n))
        if self._hash(joined) != h:
            return
        items = [e for e in joined.split(",") if e]
        cur = self.lists.get(letter)
        if cur is None or cur["items"] != items or any(cur["page"].get(k) != chunks[0].get(k) for k in ("pl", "v")):
            self.lists[letter] = {"items": items, "page": dict(chunks[0])}
            self.lists_version += 1

    def training(self):
        """{"missing": [spell ids], "for_level", "checked"} from page P (RestedXP's training list), or None."""
        lst = self.lists.get("P")
        if not lst:
            return None
        return {"missing": [int(i) for i in lst["items"] if i.isdigit()], "for_level": as_int(lst["page"].get("pl")),
                "checked": as_int(lst["page"].get("k"), 0), "unchecked": as_int(lst["page"].get("u"), 0)}

    def flight_paths(self):
        lst = self.lists.get("F")
        return None if lst is None else [int(i) for i in lst["items"] if i.isdigit()]

    def consumables(self):
        lst = self.lists.get("C")
        if lst is None:
            return None
        out = []
        for e in lst["items"]:
            p = e.split(":", 4)
            if len(p) == 5:
                out.append({"kind": p[0], "id": as_int(p[1]), "count": as_int(p[2], 0), "req": as_int(p[3]), "name": p[4]})
        return out

    def talents(self):
        """[{"pos": "tab tier column" e.g. "131", "rank", "max", "name"}] from list N (addon 0.13), or None."""
        lst = self.lists.get("N")
        if lst is None:
            return None
        out = []
        for e in lst["items"]:
            p = e.split(":", 3)
            if len(p) == 4 and len(p[0]) == 3 and p[0].isdigit():
                out.append({"pos": p[0], "rank": as_int(p[1], 0), "max": as_int(p[2], 0), "name": p[3]})
        return out

    def rxp_skipped(self):
        """RestedXP step indices the player skipped in the current guide (list V, addon 0.13), or None."""
        lst = self.lists.get("V")
        return None if lst is None else sorted(int(i) for i in lst["items"] if i.isdigit())

    def vendor_treasure(self):
        """{"vendor": index in addon/Glide/Data/VendorTreasures.lua, "items": [parse_item dicts]} while the player is
        near one (list D, addon 0.13), else None."""
        lst = self.lists.get("D")
        v = as_int((lst or {}).get("page", {}).get("v"), 0) if lst else 0
        if not v:
            return None
        return {"vendor": v, "items": [x for x in (self.parse_item(e) for e in lst["items"] if e) if x]}

    def rxp_settings(self):
        """{"ssf", "group_quests"} as RestedXP has them (page X, addon 0.13), or None."""
        x = self.pages.get("X") or {}
        if "ss" not in x:
            return None
        return {"ssf": x.get("ss") == "1", "group_quests": x.get("gq") == "1"}

    def addon_errors(self):
        """{"count", "last"}: the errors the addon caught since its UI loaded (core er, 0.15.3) and the last one's
        text (page E; None until it arrives), or None when it reports none."""
        n = as_int(self.core.get("er"), 0) or 0
        if n <= 0:
            return None
        return {"count": n, "last": self.pages.get("E", {}).get("ex") or None}

    def corpse(self):
        c = self.core
        if c.get("d") in (None, "0") or not c.get("cx") or not c.get("cy"):
            return None
        return {"x": as_int(c["cx"], 0) / 100.0, "y": as_int(c["cy"], 0) / 100.0}

    @staticmethod
    def _flag(v):
        return None if v in (None, "") else v == "1"

    _hash = staticmethod(list_hash)

    @staticmethod
    def parse_item(e):
        p = e.split(":", 10)
        if len(p) < 10:
            return None
        stats = {}
        for part in (p[10] if len(p) > 10 else "").split("/"):
            code = part.rstrip("0123456789.-")
            try:
                stats[code] = float(part[len(code):])
            except ValueError:
                pass
        return {"key": p[0], "id": as_int(p[1]), "loc": p[2], "cls": as_int(p[3]), "sub": as_int(p[4]),
                "ilvl": as_int(p[5], 0), "quality": as_int(p[6], 0), "req": as_int(p[7], 0), "usable": p[8] != "0",
                "name": p[9], "stats": stats}

    def _rebuild_gear(self, kind):
        lk = {"W": ("wn", "wh"), "I": ("in", "ih"), "R": ("rn", "rh")}[kind]
        n = as_int(self.core.get(lk[0]), 0) or 0
        chunks = self.gear_chunks[kind]
        if kind == "R" and n == 0:
            if self.gear["R"]:
                self.gear["R"] = []
                self.gear_version["R"] += 1
            return
        if n == 0 or any(i not in chunks for i in range(n)):
            return
        joined = ",".join(chunks[i] for i in range(n))
        if self._hash(joined) != as_int(self.core.get(lk[1]), -1):
            return
        items = [x for x in (self.parse_item(e) for e in joined.split(",") if e) if x]
        if items != self.gear[kind]:
            self.gear[kind] = items
            self.gear_version[kind] += 1

    def buff_remaining(self, b):
        """Seconds left on a buff (None = no expiry)."""
        if not b.get("expiry"):
            return None
        t = as_int(self.core.get("t"), 0)
        d = (b["expiry"] - t) % 1000000
        return d - 1000000 if d > 500000 else d

    def skills(self):
        """Trained weapon/armor skills from the addon: item subclass ids, or None when unknown."""
        def bits(v):
            m = as_int(v)
            return None if not m else {i for i in range(32) if m >> i & 1}
        ranks = [r for r in (self.pages.get("K", {}).get("sk") or "").split(",") if r]
        return {"weapons": bits(self.core.get("sw")), "armor": bits(self.core.get("sa")), "ranks": ranks}

    def coach_flags(self):
        return strip_spec.coach_flags(as_int(self.core.get("cf")))

    @staticmethod
    def _tdiff(a, b):
        """a - b on the addon's wrapping 0..999999 clock."""
        d = (a - b) % 1000000
        return d - 1000000 if d > 500000 else d

    def buff_list(self):
        out = []
        for k, b in dict(self.buffs, **self.buff_override).items():
            out.append({"id": k, "name": b["name"], "mine": b["mine"], "castable": b.get("castable", b["mine"]),
                        "duration": b["duration"],
                        "remaining": self.buff_remaining(b)})
        return sorted(out, key=lambda x: (x["remaining"] is None, x["remaining"] or 0))

    def _rebuild_quests(self):
        """Accept the quest log only when all its chunks are present and match the addon's hash (core qn/qh), like
        buffs and gear: a chunk of the new log next to a stale one of the old would mix the two."""
        n = as_int(self.core.get("qn"), 0) or 0
        for k in list(self.quest_chunks):
            if k >= max(n, 1):
                del self.quest_chunks[k]
        if n == 0 or any(i not in self.quest_chunks for i in range(n)):
            return
        joined = ",".join(self.quest_chunks[i] for i in range(n))
        if self._hash(joined) != as_int(self.core.get("qh"), -1):
            return
        out = {}
        for k in range(n):
            for e in self.quest_chunks[k].split(","):
                parts = e.split(":")
                if len(parts) >= 2 and parts[0].isdigit():
                    objs = []
                    if len(parts) > 2 and parts[2]:
                        nums = parts[2].split("/")
                        for i in range(0, len(nums) - 1, 2):
                            objs.append((as_int(nums[i], 0), as_int(nums[i + 1], 0)))
                    out[int(parts[0])] = {"done": parts[1] == "1", "obj": objs}
        self.quests = out

    def _track_xp(self, now):
        lvl, xp, mx = as_int(self.core.get("l")), as_int(self.core.get("x")), as_int(self.core.get("X"))
        if lvl is None or xp is None:
            return
        if self._prev_xp:
            pl, px, pm = self._prev_xp
            if lvl == pl and xp >= px:
                self.xp_gained += xp - px
            elif lvl > pl and pm:
                self.xp_gained += (pm - px) + xp
        self._prev_xp = (lvl, xp, mx)
        # the rate is worked out here, on the capture thread, so summary() (read from several threads, several
        # times a second) never walks a deque this thread is changing
        win = self._xp_win
        win.append((now, self.xp_gained))
        while now - win[0][0] > self.XP_WINDOW:
            win.popleft()
        dt = now - win[0][0]
        self._xph = int((win[-1][1] - win[0][1]) * 3600 / dt) if dt >= 60 else 0

    def _track_step(self, now):
        st = self.core.get("st")
        if st != self.step:
            self.step = st
            self.step_since = now

    # -------------------------------------------------------------------------------------------
    def xp_per_hour(self):
        """XP per hour over the last XP_WINDOW seconds of frames (0 under a minute of data)."""
        return self._xph

    def time_to_level(self):
        rate = self.xp_per_hour()
        x, mx = as_int(self.core.get("x")), as_int(self.core.get("X"))
        if not rate or x is None or not mx or x >= mx:
            return None
        return (mx - x) / rate * 3600

    def _step_page(self, pid, core=None):
        c = self.core if core is None else core
        if "sh" not in c and "si" not in c:
            return self.pages.get(pid)
        pointer, pages = self._step_context
        if pointer != (c.get("sh"), c.get("si"), c.get("st"), c.get("cl")):
            return None
        return pages.get(pid)

    def step_info(self, k=0, core=None):
        """Parsed current-cohort RXP step page S<k>: {'index', 'elements': [...], 'text'}."""
        c = self.core if core is None else core
        p = self._step_page("S%d" % k, c)
        if not p or not p.get("i"):
            return None
        if "sh" in c or "si" in c:
            step = as_int(c.get("st"))
            if step is None or as_int(p.get("i")) != step + k:
                return None
        els = []
        for e in (p.get("el") or "").split(","):
            parts = e.split(":")
            if parts[0] == "go" and len(parts) >= 4:
                els.append({"tag": "goto", "map": as_int(parts[1]), "x": float(parts[2] or 0), "y": float(parts[3] or 0)})
            elif len(parts) >= 2 and parts[1].rstrip("!").isdigit():
                els.append({"tag": parts[0], "quest": int(parts[1].rstrip("!")), "done": parts[1].endswith("!")})
            elif parts[0]:
                els.append({"tag": parts[0]})
        return {"index": as_int(p.get("i")), "elements": els, "text": p.get("tx", "")}

    def goto_target(self):
        """The waypoint RestedXP's arrow points at (core aw, addon 0.13.1), else the step's first goto. A step with
        several gotos lists them all, and the arrow moves on to the next as each is reached."""
        aw = (self.core.get("aw") or "").split(":")
        if len(aw) == 3:
            try:
                return {"tag": "goto", "map": as_int(aw[0]), "x": float(aw[1]), "y": float(aw[2]), "arrow": True}
            except ValueError:
                pass
        s = self.step_info(0)
        if not s:
            return None
        for e in s["elements"]:
            if e["tag"] == "goto":
                return e
        return None

    def distance_to_goto(self):
        g = self.goto_target()
        if not g or not self.pos_hist:
            return None
        _, mp, x, y = self.pos_hist[-1]
        if g.get("map") and g["map"] != mp:
            return None
        return ((g["x"] - x) ** 2 + (g["y"] - y) ** 2) ** 0.5

    def vitals(self):
        """The few summary keys read on every frame (banter.py's close calls and big kills), without the whole
        summary: combat, dead, hp and the target's name, level, classification and whether it died."""
        c = self.core
        t = self.pages.get("T", {})
        return {"combat": c.get("c") == "1", "dead": as_int(c.get("d"), 0), "hp": self._gauge("h", "hp"),
                "target": {"name": t.get("tn"), "level": as_int(c.get("tl")), "dead": c.get("td") == "1",
                           "classification": t.get("tc") or None} if c.get("tg") is not None else None}

    def summary(self):
        c = self.core
        b = self.pages.get("B", {})
        player = self._step_page("B", c) or {}
        z = self.pages.get("Z", {})
        g = self.pages.get("G", {})
        t = self.pages.get("T", {})
        pos = self.pos_hist[-1] if self.pos_hist else None
        s: dict = {
            "name": b.get("nm"), "class": c.get("cl") or player.get("cl"),
            "race": player.get("ra"), "faction": player.get("fa"),
            "bind": b.get("bd"),
            "level": as_int(c.get("l")), "xp": as_int(c.get("x")), "xp_max": as_int(c.get("X")),
            "rested": as_int(c.get("r"), 0), "hp": self._gauge("h", "hp"), "power": self._gauge("m", "power"),
            "power_type": (c.get("pt") or "").lower() or None,
            "combat": c.get("c") == "1", "dead": as_int(c.get("d"), 0),
            "map": as_int(c.get("mp")), "x": pos[2] if pos else None, "y": pos[3] if pos else None,
            "zone": z.get("zn"), "subzone": z.get("sz"),
            "money": as_int(c.get("g")), "free_bags": as_int(c.get("b")), "durability": as_int(c.get("du")),
            "hearth_cd": as_int(c.get("hs")), "resting": c.get("rs") == "1", "mounted": c.get("mo") == "1",
            "on_taxi": c.get("tx") == "1", "kills": as_int(c.get("kc"), 0),
            "height": as_int(c.get("wz")) / 2.0 if as_int(c.get("wz")) is not None else None,
            "fly_to": c.get("fy") or None, "taxi_to": c.get("fd") or None,
            "target": {"npc": c.get("tg"), "name": t.get("tn"), "level": as_int(c.get("tl")),
                       "hp": self._gauge("th", "target_hp"), "reaction": as_int(c.get("tr")), "dead": c.get("td") == "1",
                       "classification": t.get("tc") or None,
                       "tapped_by_others": self._flag(c.get("tt")), "quest_mob": self._flag(c.get("tq"))}
            if c.get("tg") is not None else None,
            "guide": g.get("gn"), "guide_group": g.get("gg"), "step": as_int(c.get("st")),
            "steps": [self.step_info(k, c) for k in range(3)],
            "quests": self.quests,
            "coach_flags": self.coach_flags(),
            "skills": self.skills(),
            "talent_points": as_int(c.get("tp")), "training": self.training(), "flight_paths": self.flight_paths(),
            "talents": self.talents(), "rxp_skipped": self.rxp_skipped(), "rxp_settings": self.rxp_settings(),
            "vendor_treasure": self.vendor_treasure(),
            "consumables": self.consumables(), "corpse": self.corpse(), "res_sickness_s": as_int(c.get("rz")),
            "buffs": self.buff_list(), "buffs_hidden": self.core.get("ak") == "1",
            "buffs_stale": self.core.get("ak") == "1" or self.core.get("c") == "1",
            "addon_version": self.core.get("gv"), "addon_errors": self.addon_errors(),
            "xp_per_hour": self.xp_per_hour(), "ttl_seconds": self.time_to_level(),
            "xp_session": self.xp_gained, "session_minutes": int((time.time() - self.started) / 60),
            "on_step_seconds": int(time.time() - self.step_since),
            "dist_to_goto": self.distance_to_goto(),
            "arrow": self.goto_target(),
            "fresh": time.time() - self.last_frame < self.FRESH_SECONDS,
        }
        if "sh" in c or "si" in c:
            s["step_source_index"] = as_int(c.get("si"))
            s["step_fingerprint"] = c.get("sh") or None
        if s["step"] and (self.step_lookup is not None or "sh" in c or "si" in c):
            match: dict = {"status": "unverified"}
            if not s["fresh"]:
                match = {"status": "stale"}
            elif s.get("step_fingerprint") and (s.get("step_source_index") or 0) > 0 and self.step_lookup is not None:
                match = self.step_lookup(s)
            s["step_match"] = match["status"]
            s["step_match_reason"] = self.STEP_MATCH_REASONS.get(match["status"])
            if match["status"] == "verified":
                current = dict(s["steps"][0] or {"index": s["step"], "elements": [], "text": ""})
                entry = match["step"]
                if entry["text"]:
                    current["text"] = entry["text"]
                current["notes"] = entry["notes"]
                s["steps"][0] = current
        return s
