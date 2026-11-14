"""KnowledgePack: the knowledge layer bound to one kb.db, with the caches that keep a live callout fast.

The pack is a facade over its parts (one mixin each): resolve (the live summary to a guide, route and step;
briefs), lookup (places, names, creature cards, quest rows), panel (the live block and the dashboard panel),
extras (the addon's extra pages), facts (grounding). The pack owns every cache; the parts only use them."""
import threading

from knowledge import store
from knowledge.api import tools
from knowledge.api._layer import E, IMPORT_ERROR, get_data
from knowledge.api.extras import ExtrasMixin
from knowledge.api.facts import FactsMixin
from knowledge.api.guides import GuideResolver, RXP_KEYS, player_from
from knowledge.api.lookup import LookupMixin
from knowledge.api.panel import PanelMixin
from knowledge.api.resolve import ResolveMixin
from knowledge.step_identity import StepIdentityIndex


class KnowledgePack(ResolveMixin, LookupMixin, PanelMixin, ExtrasMixin, FactsMixin):
    """See the module docstring. Caches are plain dicts guarded by a lock for writes; the coach thread computes,
    the dashboard and capture threads only read cached data (panel(), decay_info())."""

    def __init__(self, kb, rxp=None, geo=None):
        if isinstance(kb, str):
            kb = store.open_ro(kb)
        self.kb = kb
        self.rxp = {k: v for k, v in (rxp or {}).items() if k in RXP_KEYS and v is not None}
        self.geo = geo
        self.ok, self.reason = True, None
        if IMPORT_ERROR:
            self.ok, self.reason = False, "knowledge layer failed to import (%s)" % IMPORT_ERROR
        elif kb is None or not getattr(kb, "ok", False):
            self.ok, self.reason = False, "knowledge base missing (data/kb.db): run build-kb.cmd"
        elif store.schema_version(kb) != store.SCHEMA_VERSION:
            self.ok, self.reason = False, "rebuild needed (schema %s, engine expects %s)" % (
                store.schema_version(kb), store.SCHEMA_VERSION)
        self._lock = threading.RLock()
        self.guides = GuideResolver(kb) if self.ok else None
        self._res, self._briefs, self._steps, self._sim, self._eta = {}, {}, {}, {}, {}
        self._cards, self._qrows, self._decay, self.guide_summaries, self._sf, self._names = {}, {}, {}, {}, {}, {}
        self._glossary = None
        self._rules = None
        self._panel = None
        self._status = None
        self._step_identity: StepIdentityIndex | None = None

    # --------------------------------------------------------------------------------------------- basics
    @property
    def data(self):
        return get_data(self.kb)

    def cache_put(self, cache, key, value, limit):
        """Store value in one of the pack's caches (emptied first when it holds more than limit)."""
        with self._lock:
            if len(cache) > limit:
                cache.clear()
            cache[key] = value
        return value

    def verified_step(self, summary):
        """Full authored current-step text after an exact source ordinal/block hash match."""
        if not self.ok:
            return {"status": "missing"}
        player = player_from(summary, self.rxp) or {}
        # A guide-name faction guess is not evidence for choosing an inline source-step branch.
        player["faction"] = str(summary["faction"]).capitalize() if summary.get("faction") else None
        with self._lock:
            if self._step_identity is None:
                try:
                    rows = self.kb.q("SELECT key, name, grp, text FROM rxp_guide")
                finally:
                    self.kb.close()  # the in-memory index must not pin a replaced pack on the capture thread
                self._step_identity = StepIdentityIndex(rows)
            return self._step_identity.resolve(summary, player)

    def warm(self):
        """Load the lazily built tables (guide names, quests, creatures, maps) so the first callout is fast.
        Call it from the coach thread."""
        if not self.ok:
            return
        try:
            self.guides._impl()
            d = self.data
            d.quest(1)
            d.creature(1)
            d.map_name(1426)
            self.rules()
        except Exception:  # noqa: BLE001
            pass

    def rules(self):
        """The heuristic rules this KB was built with (its heuristic table, compiled once); rules.json is a build
        input. A KB without rows (an old or partial build) falls back to rules.json."""
        rules = self._rules
        if rules is None:
            rules = []
            for r in self.kb.q("SELECT * FROM heuristic ORDER BY rowid") if self.ok else ():
                rule = {"id": r["id"], "title": r.get("title"), "tags": store.jload(r.get("tags"), []) or [],
                        "applies": store.jload(r.get("applies"), {}) or {}, "advice": r.get("advice"),
                        "rationale": r.get("rationale"), "source": store.jload(r.get("source"), []) or [],
                        "conf": r.get("conf"), "verified": r.get("verified") or 0}
                if r.get("priority") is not None:
                    rule["priority"] = r["priority"]
                try:
                    rules.append(E.Rule(rule))
                except (ValueError, TypeError, AttributeError):
                    continue                  # a predicate this engine can't compile: leave the rule out
            if not rules and not IMPORT_ERROR:
                rules = E.load_rules()
            self._rules = rules
        return rules

    def status(self):
        """{"ok", "built", "guides", "briefs", "heuristics", "path"} or {"ok": False, "reason"} (cached)."""
        if not self.ok:
            return {"ok": False, "reason": self.reason}
        if self._status is None:
            counts = store.jload(self.kb.meta("counts"), {}) or {}
            self._status = {"ok": True, "built": self.kb.meta("built"), "guides": counts.get("rxp_guide"),
                            "briefs": counts.get("step_brief"), "heuristics": counts.get("heuristic"),
                            "quests": counts.get("quest"), "path": getattr(self.kb, "path", None)}
        return self._status

    def guide_name(self, gk):
        """The guide's name as RestedXP shows it (the key's name part when the guide is unknown)."""
        row = self.data.guide_rows.get(gk) or {}
        return row.get("name") or (gk or "").split("||")[-1]

    def tool_definitions(self):
        return tools.tool_definitions()


def verify_facts(pack_or_kb, ids):
    """(known, unknown) fact ids; accepts a KnowledgePack, a store.KB or a path."""
    pack = pack_or_kb if isinstance(pack_or_kb, KnowledgePack) else KnowledgePack(pack_or_kb)
    return pack.verify_facts(ids)
