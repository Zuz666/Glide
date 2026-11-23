"""Model catalog for the dashboard's model selector: every model Anthropic (GET /v1/models, needs
ANTHROPIC_API_KEY), Google's Gemini API (GET /v1beta/models, needs GEMINI_API_KEY; ids "gemini:<id>") and
OpenRouter (GET /api/v1/models, public) offer, with prices per million tokens,
context size and tool support. Cached in data/live/models.json; refreshed from the dashboard or when a day old.

Prices: OpenRouter's list carries them (USD per token -> per MTok here). Anthropic's API doesn't, so Anthropic
models get the published list prices below (cache read/write included); a model missing from the table shows
no price.
"""
import json
from datetime import datetime, timezone
import threading
import time
import urllib.error
import urllib.request

import jsonfile
import openai_capabilities
import openai_compat

# $/MTok as of 2026-10-11: (id substring, input, output, cache read, cache write 5 min); first match wins.
# Cache reads are 0.1x input, except Opus/Sonnet 5.5 (0.05x) and Fable/Mythos 5.1 (0.025x); 1-hour writes are 2x.
ANTHROPIC_PRICES = (("fable", 10.0, 50.0, 0.25, 12.50), ("mythos", 10.0, 50.0, 0.25, 12.50),
                    ("opus-5-5", 4.0, 20.0, 0.20, 5.00), ("opus-4-1", 15.0, 75.0, 1.50, 18.75),
                    ("opus-4-0", 15.0, 75.0, 1.50, 18.75), ("opus", 5.0, 25.0, 0.50, 6.25),
                    ("sonnet-5-5", 2.0, 10.0, 0.10, 2.50), ("sonnet-5", 2.0, 10.0, 0.20, 2.50), ("sonnet-4", 3.0, 15.0, 0.30, 3.75),
                    ("sonnet", 2.0, 10.0, 0.20, 2.50), ("haiku-3", 0.8, 4.0, 0.08, 1.00),
                    ("haiku", 1.0, 5.0, 0.10, 1.25))
# Gemini API Standard tier, $/MTok as of 2026-10-09 (ai.google.dev pricing; prompts up to 200k tokens):
# (id prefix, input, output, cache read); longest matching prefix wins. Rates for 3.6/3.8 Flash rise on 2027-01-01.
GEMINI_PRICES = (("gemini-3.8-flash", 0.75, 3.75, 0.075), ("gemini-3.6-flash", 0.75, 3.75, 0.075),
                 ("gemini-3.5-flash-lite", 0.30, 2.50, 0.03), ("gemini-3.1-flash-lite", 0.25, 1.50, 0.025),
                 ("gemini-3.1-pro", 2.00, 12.00, 0.20), ("gemini-3-flash", 0.50, 3.00, 0.05),
                 ("gemini-2.5-pro", 1.25, 10.00, 0.125), ("gemini-2.5-flash-lite", 0.10, 0.40, 0.01),
                 ("gemini-2.5-flash", 0.30, 2.50, 0.03))
GEMINI_SKIP = ("tts", "image", "embedding", "live", "transcribe", "translate", "veo", "imagen", "aqa", "banana",
               "audio", "robotics", "computer-use")
GEMINI_MODELS = "https://generativelanguage.googleapis.com/v1beta/models"
# shown when the Anthropic key can't list models (ids the coach has used)
KNOWN_ANTHROPIC = (("claude-haiku-4-5-20251001", "Claude Haiku 4.5"), ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
                   ("claude-opus-5-5", "Claude Opus 5.5"), ("claude-fable-5-1", "Claude Fable 5.1"))
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
ANTHROPIC_MODELS = "https://api.anthropic.com/v1/models"
MAX_AGE_S = 24 * 3600


def provider(model):
    m = model or ""
    if m.startswith("gemini:"):
        return "gemini"
    if m.startswith("ep:"):
        return "endpoint"                              # a custom OpenAI-compatible endpoint (openai_compat.endpoints)
    return "openrouter" if "/" in m else "anthropic"


def fetch_endpoint(ep, key=None, getter=None):
    """[{id: "ep:<endpoint>:<model>", ...}] from an OpenAI-compatible GET <base_url>/models (no prices)."""
    getter = getter or _get
    d = getter(ep["base_url"] + "/models", {"authorization": "Bearer " + key} if key else {})
    out = []
    for m in d.get("data") or d.get("models") or ():
        mid = m.get("id") or m.get("name") if isinstance(m, dict) else None
        if mid:
            row = {"id": "ep:%s:%s" % (ep["id"], mid), "provider": "endpoint", "endpoint": ep["id"],
                   "name": "%s (%s)" % (mid, ep["label"]), "context": m.get("context_length"),
                   "tools": None, "price": None}
            if isinstance(m.get("shutdown_date"), str):
                row["shutdown_date"] = m["shutdown_date"]
            out.append(row)
    return out


def gemini_price(model):
    m = (model or "")
    m = m[len("gemini:"):] if m.startswith("gemini:") else m
    best = None
    for k, i, o, r in GEMINI_PRICES:
        if m.startswith(k) and (best is None or len(k) > len(best[0])):
            best = (k, i, o, r)
    return {"in": best[1], "out": best[2], "cache_read": best[3], "cache_write": None} if best else None


def fetch_gemini(key, url=GEMINI_MODELS, getter=None):
    """[{id: "gemini:<id>", ...}] text models from the Gemini API that can generate content; [] without a key."""
    if not key:
        return []
    getter = getter or _get
    out, token = [], None
    for _ in range(20):
        d = getter(url + "?pageSize=1000" + ("&pageToken=" + token if token else ""), {"x-goog-api-key": key})
        for m in d.get("models") or ():
            mid = (m.get("name") or "").split("/", 1)[-1]
            if not mid.startswith("gemini") or "generateContent" not in (m.get("supportedGenerationMethods") or ()):
                continue
            if any(w in mid for w in GEMINI_SKIP):
                continue
            out.append({"id": "gemini:" + mid, "provider": "gemini", "name": m.get("displayName") or mid,
                        "context": m.get("inputTokenLimit"), "tools": True, "price": gemini_price(mid)})
        token = d.get("nextPageToken")
        if not token:
            break
    return out


def anthropic_price(model):
    m = (model or "").lower()
    for k, i, o, r, w in ANTHROPIC_PRICES:
        if k in m:
            return {"in": i, "out": o, "cache_read": r, "cache_write": w}
    return None

def static_price(model, cfg=None):
    """First-party published price dict when known statically for anthropic, gemini, or official_openai."""
    prov = provider(model)
    if prov == "anthropic":
        return anthropic_price(model)
    if prov == "gemini":
        return gemini_price(model)
    if prov == "endpoint":
        return openai_capabilities.describe_endpoint_model(model, cfg).price
    return None


def _get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=dict(headers or {}, **{"user-agent": "glide-companion"}))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_anthropic(key, base=ANTHROPIC_MODELS, getter=_get):
    """[{id, provider, name, ...}] from Anthropic's model list (all pages); [] without a key."""
    if not key:
        return []
    out, after = [], None
    for _ in range(20):
        url = base + "?limit=1000" + ("&after_id=" + after if after else "")
        d = getter(url, {"x-api-key": key, "anthropic-version": "2023-06-01"})
        for m in d.get("data") or ():
            if m.get("lifecycle") == "retired":
                continue
            out.append({"id": m["id"], "provider": "anthropic", "name": m.get("display_name") or m["id"],
                        "context": m.get("max_input_tokens"), "tools": True, "created": m.get("created_at"),
                        "deprecated": m.get("lifecycle") == "deprecated", "price": anthropic_price(m["id"]),
                        "capabilities": m.get("capabilities")})
        if not d.get("has_more") or not d.get("last_id"):
            break
        after = d["last_id"]
    return out


def _per_m(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f * 1e6, 4) if f >= 0 else None


def fetch_openrouter(key=None, url=OPENROUTER_MODELS, getter=_get):
    """[{id, provider, name, ...}] from OpenRouter's public model list (prices per MTok)."""
    d = getter(url, {"authorization": "Bearer " + key} if key else {})
    out = []
    for m in d.get("data") or ():
        p = m.get("pricing") or {}
        price = {"in": _per_m(p.get("prompt")), "out": _per_m(p.get("completion")),
                 "cache_read": _per_m(p.get("input_cache_read")), "cache_write": _per_m(p.get("input_cache_write"))}
        if price["in"] is None:
            price = None
        params = m.get("supported_parameters")
        arch = m.get("architecture") or {}
        if "text" not in (arch.get("output_modalities") or ["text"]):
            continue                                   # image/audio generators can't coach
        out.append({"id": m["id"], "provider": "openrouter", "name": m.get("name") or m["id"],
                    "context": m.get("context_length"), "created": m.get("created"),
                    "tools": ("tools" in params) if isinstance(params, list) else None, "price": price,
                    "reasoning": m.get("reasoning")})
    return out


class Catalog:
    """Thread-safe model list with a disk cache. entries() never touches the network; refresh() does."""

    def __init__(self, path, anthropic_key=None, openrouter_key=None, fetchers=None, gemini_key=None, endpoints=None):
        self.path = path
        self.keys = {"anthropic": anthropic_key, "gemini": gemini_key, "openrouter": openrouter_key}
        self.endpoints = endpoints or {}                 # {id: endpoint dict + "key"}: custom OpenAI-compatible APIs
        self.fetchers = dict({"anthropic": fetch_anthropic, "gemini": fetch_gemini, "openrouter": fetch_openrouter},
                             **(fetchers or {}))
        self.lock = threading.Lock()
        self.data = {"updated": None, "models": [], "errors": {}}
        try:
            with open(path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            pass
        self._index()

    def _ep_cfg(self):
        return {"endpoints": list(self.endpoints.values())}

    def _project_entry(self, m, ep_cfg):
        mid = m.get("id")
        if m.get("provider") != "endpoint" or not openai_compat.is_endpoint(mid):
            return m
        contract = openai_capabilities.describe_endpoint_model(mid, ep_cfg, m)
        if contract.scope != "official_openai":
            return {k: v for k, v in m.items() if k != "unverified"} if "unverified" in m else m
        if contract.suitability == "excluded":
            return None
        out = {k: v for k, v in m.items() if k != "unverified"}
        if contract.suitability == "coach":
            if out.get("context") is None and contract.context is not None:
                out["context"] = contract.context
            if out.get("price") is None and contract.price is not None:
                out["price"] = contract.price
        elif contract.suitability == "unclassified":
            out["unverified"] = True
        return out

    def _projection_key(self):
        models_list = self.data.get("models")
        eps = tuple(sorted((str(k), str((v or {}).get("base_url") or ""))
                           for k, v in (self.endpoints or {}).items() if isinstance(v, dict)))
        return (id(self.data), id(models_list), len(models_list or ()), eps,
                datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    def _index(self):
        raw = list(self.data.get("models") or ())
        self.raw_by_id = {m["id"]: m for m in raw if isinstance(m, dict) and "id" in m}
        ep_cfg = self._ep_cfg()
        self.projected = [p for m in raw if isinstance(m, dict) for p in (self._project_entry(m, ep_cfg),) if p is not None]
        self.by_id = {m["id"]: m for m in self.projected}
        self._proj_key = self._projection_key()

    def _ensure_projected(self):
        if getattr(self, "_proj_key", None) != self._projection_key():
            self._index()
    def refresh(self):
        """Fetch both lists (a failing provider keeps its old entries); returns the status dict."""
        old = self.data.get("models") or []
        models, errors = [], {}
        for prov in ("anthropic", "gemini", "openrouter"):
            try:
                got = self.fetchers[prov](self.keys.get(prov))
            except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                errors[prov] = "%s: %s" % (type(e).__name__, e)
                got = [m for m in old if m.get("provider") == prov]
            if prov == "gemini" and not got and not self.keys.get("gemini"):
                errors.setdefault("gemini", "no GEMINI_API_KEY")
            if prov == "anthropic" and not got:
                got = [{"id": i, "provider": "anthropic", "name": n, "tools": True, "price": anthropic_price(i),
                        "context": None, "listed": False} for i, n in KNOWN_ANTHROPIC]
                if not self.keys.get("anthropic"):
                    errors.setdefault("anthropic", "no ANTHROPIC_API_KEY: showing known models")
            models += got
        for eid, ep in self.endpoints.items():
            try:
                got = self.fetchers.get("endpoint", fetch_endpoint)(ep, ep.get("key"))
            except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
                errors["ep:" + eid] = "%s: %s" % (type(e).__name__, e)
                got = [m for m in old if m.get("endpoint") == eid]
            models += got
        with self.lock:
            self.data = {"updated": time.time(), "models": models, "errors": errors}
            self._index()
        try:
            jsonfile.write_atomic(self.path, self.data)
        except OSError:
            pass
        return self.status()

    def refresh_if_old(self):
        if not self.data.get("updated") or time.time() - self.data["updated"] > MAX_AGE_S:
            threading.Thread(target=self.refresh, daemon=True).start()

    def status(self):
        self._ensure_projected()
        d = self.data
        return {"updated": d.get("updated"), "errors": d.get("errors") or {}, "count": len(self.projected),
                "keys": {k: bool(v) for k, v in self.keys.items()}, "endpoints": sorted(self.endpoints)}

    def entries(self):
        self._ensure_projected()
        return list(self.projected)

    def get(self, model):
        self._ensure_projected()
        return self.by_id.get(model)

    def raw_get(self, model):
        self._ensure_projected()
        return self.raw_by_id.get(model) or self.by_id.get(model)

    def price(self, model, cfg=None):
        """{"in", "out", "cache_read", "cache_write"} $/MTok, or None."""
        e = self.get(model)
        if e and e.get("price"):
            return e["price"]
        ep_cfg = cfg if cfg is not None else {"endpoints": list(self.endpoints.values())}
        return static_price(model, ep_cfg)
    def supports_tools(self, model):
        e = self.get(model)
        return True if e is None or e.get("tools") is None else bool(e["tools"])


def cost_usd(price, usage):
    """Dollars for one API response's usage under a price dict (None when unknown)."""
    if not price or price.get("in") is None:
        return None
    u = usage or {}
    pin, pout = price["in"], price.get("out") or 0
    pread = price.get("cache_read") if price.get("cache_read") is not None else pin
    pwrite = price.get("cache_write") if price.get("cache_write") is not None else pin
    written = u.get("cache_creation_input_tokens") or 0
    hour = min(written, ((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens")) or 0)   # Anthropic 1h: 2x
    ttl_12h = min(written - hour, u.get("cache_creation_12h_input_tokens") or 0)                   # OpenAI 12h: 2.5x
    std_written = written - hour - ttl_12h
    return ((u.get("input_tokens") or 0) * pin + (u.get("output_tokens") or 0) * pout
            + (u.get("cache_read_input_tokens") or 0) * pread
            + std_written * pwrite + hour * pin * 2 + ttl_12h * pin * 2.5) / 1e6
