"""Live-tunable coach settings (POST /settings: the desktop app's Settings and model picker, and voice toggles): how
many upcoming RestedXP steps questions get, the feature switches and the models, and what each setting costs (the
state's "tuning", which no UI shows today since the dashboard's settings panel went; it stays in the state, contract
3, and is recomputed only when what it reads changes: SnapshotCache).

The cost shown is the extra input the route lookahead adds: steps x tokens per step (measured on the steps the coach
last sent), times the API rounds a question takes (a tool loop re-sends the whole conversation each round; measured
per question kind), times the model's input price. The lookahead sits in the user message, after the cached system
prompt, so it is billed at the full input price.
"""
import os
import time

import glideconfig
import models
import settings
import reasoning
import openai_capabilities
import openai_compat

# what the dashboard (and push-to-talk) may change: the live settings in settings_manifest.json
_LIVE = {k: settings.ENTRIES["llm." + k] for k in settings.live_keys()}
SETTINGS = {k: (e["default"], e["min"], e["max"]) for k, e in _LIVE.items() if e["kind"] == "int"}  # (default, min, max)
TOGGLES = {k: e["default"] for k, e in _LIVE.items() if e["kind"] == "bool"}       # on/off features
MODEL_KEYS = ("fast_model", "deep_model")
REASONING_KEYS = ("fast_reasoning_effort", "deep_reasoning_effort")
TEXTS = {k for k, e in _LIVE.items() if e["kind"] == "text" and k not in MODEL_KEYS}  # free text (the persona)
MAX_TEXT = 600
DEFAULT_TOKENS_PER_STEP = 52.0     # measured on Dun Morogh steps (knowledge layer route_steps)
DEFAULT_ROUNDS = {"voice": 1.7, "typed": 3.0}
DEFAULT_RATE = {"voice": 10.6, "typed": 1.0}   # questions per hour from the 10.4 h of logs (before any are asked)


def input_price(model, catalog=None):
    """$/MTok input for a model: the catalog's price (OpenRouter list, Anthropic table), else the Anthropic table."""
    if catalog is not None:
        p = catalog.price(model)
        if p and p.get("in") is not None:
            return p["in"]
    p = models.static_price(model)
    return p["in"] if p else 2.0


import re  # noqa: E402

MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{2,119}$")
# tokens per hour per model path when this session hasn't measured enough yet (10.4 h of logs, 2026-10-05):
# fast = callouts (~50/h) + spoken questions (~10.6/h); deep = typed questions (~1/h) + pace reviews (~3.3/h)
DEFAULT_PROFILE = {"fast": {"cache_read": 362600, "uncached": 131100, "output": 3000},
                   "deep": {"cache_read": 78400, "uncached": 36700, "output": 1100}}


def profile(coach, min_hours=0.25, min_calls=5):
    """Tokens per hour on each model path: this session's measured usage once there is enough of it."""
    ps = getattr(coach, "path_stats", None) or {}
    hours = (time.time() - (ps.get("since") or time.time())) / 3600.0
    out = {}
    for path in ("fast", "deep"):
        st = ps.get(path) or {}
        if hours >= min_hours and (st.get("calls") or 0) >= min_calls:
            out[path] = {k: int((st.get(k) or 0) / hours) for k in ("cache_read", "uncached", "output")}
            out[path]["measured"] = True
            out[path]["cost_per_h"] = round((st.get("cost_usd") or 0) / hours, 4)
        else:
            out[path] = dict(DEFAULT_PROFILE[path], measured=False)
    return out


def clamp(key, value):
    return settings.clamp("llm." + key, value)


def rate_per_hour(times, kind, now=None, window=3600, minimum=600):
    """Questions of `kind` per hour over the last hour (at least 10 minutes of observation); None before any."""
    now = now or time.time()
    ts = [t for t, k in times if k == kind and now - t <= window]
    if not times:
        return None
    first = min(t for t, _ in times)
    span = max(minimum, min(window, now - first))
    return round(len(ts) * 3600.0 / span, 1)


def snapshot(coach, review_minutes=15):
    """Everything the dashboard panel needs: current values, limits, and the cost inputs per path."""
    cfg = coach.cfg
    tps = getattr(coach, "look_tokens_per_step", None) or DEFAULT_TOKENS_PER_STEP
    rounds = getattr(coach, "q_rounds", {}) or {}
    times = list(getattr(coach, "q_times", ()) or ())
    out = {"settings": {k: {"value": clamp(k, cfg.get(k)), "min": lo, "max": hi, "default": d}
                        for k, (d, lo, hi) in SETTINGS.items()},
           "tokens_per_step": round(tps, 1), "measured": getattr(coach, "look_tokens_per_step", None) is not None,
           "convo": {"tokens": round(getattr(coach, "convo_tokens", None) or 0),
                     "measured": getattr(coach, "convo_tokens", None) is not None,
                     "turns_now": sum(1 for e in getattr(coach, "convo", ()) or ()
                                      if e.get("who") == "player" and time.time() - e.get("t", 0)
                                      <= 60 * clamp("conversation_minutes", cfg.get("conversation_minutes")))},
           "paths": {}, "toggles": {k: settings.clamp("llm." + k, cfg.get(k)) for k in TOGGLES}}
    catalog = getattr(coach, "catalog", None)
    for kind, model_key, _key in (("voice", "fast_model", "voice_lookahead_steps"),
                                 ("typed", "deep_model", "lookahead_steps")):
        model = cfg.get(model_key)
        r = rate_per_hour(times, kind)
        out["paths"][kind] = {"model": model, "price_in": input_price(model, catalog),
                              "rounds": round(rounds.get(kind) or DEFAULT_ROUNDS[kind], 2),
                              "rate_per_h": r if r is not None else DEFAULT_RATE[kind],
                              "rate_measured": r is not None}
    # pace reviews take the typed path too (deep model, same lookahead)
    out["paths"]["typed"]["reviews_per_h"] = round(60.0 / max(1, review_minutes), 1) if review_minutes else 0
    return out


class SnapshotCache:
    """snapshot(), recomputed only when the settings, models or measurements it reads change, or at least once a
    `ttl` (its question rates and the conversation's recent turns age with the clock)."""

    def __init__(self, ttl=60.0):
        self.ttl, self._key, self._t, self._snap = ttl, None, 0.0, None

    def get(self, coach, review_minutes=15):
        key = _inputs(coach, review_minutes)
        if key != self._key or time.time() - self._t >= self.ttl:
            self._snap, self._key, self._t = snapshot(coach, review_minutes), key, time.time()
        return self._snap


def _inputs(coach, review_minutes):
    """What snapshot() reads from the coach, as a comparable tuple."""
    cfg = coach.cfg
    times = getattr(coach, "q_times", ()) or ()
    convo = getattr(coach, "convo", ()) or ()
    return (tuple(cfg.get(k) for k in (*SETTINGS, *TOGGLES, *MODEL_KEYS)), review_minutes,
            getattr(coach, "look_tokens_per_step", None), tuple(sorted((getattr(coach, "q_rounds", {}) or {}).items())),
            len(times), times[-1] if times else None, getattr(coach, "convo_tokens", None), len(convo),
            convo[-1].get("t") if convo else None, id(getattr(coach, "catalog", None)))


def validate_model_choice(model, cfg, catalog=None):
    """Reject malformed IDs and excluded official OpenAI specialty/shut-down models before saving."""
    if not isinstance(model, str) or not MODEL_RE.match(model.strip()):
        raise ValueError("not a model id: %r" % (model,))
    clean = model.strip()
    if openai_compat.is_endpoint(clean):
        entry = openai_capabilities.catalog_raw_entry(catalog, clean)
        contract = openai_capabilities.describe_endpoint_model(clean, cfg, entry)
        if contract.suitability == "excluded":
            raise ValueError("%s cannot be used as a coach model (%s)." % (clean, contract.reason))


def apply(cfg, changes, config_path=None, catalog=None):
    """Apply dashboard changes to the live coach config (cfg = the coach's "llm" dict) and save them in config.json
    under companion.llm, keeping everything else in the file. Returns the values applied."""
    changes = dict(changes or {})
    prospective = dict(cfg, **changes)
    for model_key, effort_key in zip(MODEL_KEYS, REASONING_KEYS):
        if model_key in changes:
            validate_model_choice(changes[model_key], prospective, catalog=catalog)
        model = str(prospective.get(model_key) or settings.default("llm." + model_key)).strip()
        if effort_key in changes:
            reasoning.validate(model, prospective, changes[effort_key],
                               openai_capabilities.catalog_raw_entry(catalog, model))
        elif model_key in changes and model != cfg.get(model_key, settings.default("llm." + model_key)):
            if cfg.get(effort_key, "auto") != "auto":
                changes[effort_key] = "auto"     # the previous role override belonged to another model
    applied = {}
    for k, v in (changes or {}).items():
        if k in SETTINGS:
            applied[k] = clamp(k, v)
        elif k in TOGGLES:
            if not isinstance(v, bool):
                raise ValueError("%s takes true or false, not %r" % (k, v))
            applied[k] = v
        elif k in MODEL_KEYS:
            applied[k] = v.strip()
        elif k in TEXTS:
            if not isinstance(v, str):
                raise ValueError("%s takes text, not %r" % (k, v))
            applied[k] = " ".join(v.split())[:MAX_TEXT]
    if applied and config_path:
        save(config_path, applied)
    cfg.update(applied)                         # validation and persistence succeeded before live state changes
    return applied


def save(config_path, values):
    """Save `values` under companion.llm in config.json (glideconfig.patch: re-read, merged, written whole), keeping
    everything else in it. A config.json that can't be read raises ValueError (and stays as it is): saving over it
    would lose the rest of the file."""
    glideconfig.patch(("companion", "llm"), values, root=os.path.dirname(os.path.abspath(config_path)))
