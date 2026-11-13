"""The engine's settings as settings_manifest.json describes them: each one's default, range and kind, written once
for the engine, the dashboard (tuning.py) and the desktop Settings page.

    opt = Settings("llm", cfg["llm"])        # a config section, the same dict the dashboard changes live
    opt.get("lookahead_steps")               # the value in range (40 when config.json doesn't set it)

A value out of its range (a hand edit) reads as the nearest end of it, one of the wrong kind as the default, so the
coach does what the dashboard and the Settings page show.
"""
import json
import os
from typing import Any

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings_manifest.json")
with open(MANIFEST_PATH, encoding="utf-8") as _fh:
    ENTRIES = json.load(_fh)["settings"]       # "llm.lookahead_steps" -> {"default", "min", "max", "kind", ...}


# settings an older version had and this one doesn't: dropped from a loaded config, and from config.json when the
# engine starts (glideconfig.drop_retired), so nothing of them is left to read
RETIRED = ("llm.sidekick",)


def default(key):
    return ENTRIES[key]["default"]


def clamp(key, value) -> Any:
    """`value` as setting `key` takes it: the default when it is unset or of the wrong kind, numbers kept in range."""
    e = ENTRIES[key]
    d, kind = e["default"], e["kind"]
    if value is None:
        return json.loads(json.dumps(d)) if kind == "object" else d
    if kind == "bool":
        return bool(value)
    if kind == "text":
        return value if isinstance(value, str) else str(value)
    if kind == "list":
        return value if isinstance(value, list) else d
    if kind == "object":
        return value if isinstance(value, dict) else json.loads(json.dumps(d))
    try:
        if kind == "int":
            v = int(round(float(value)))
        else:                                  # "number": an int stays an int, a float a float
            v = value if isinstance(value, (int, float)) and not isinstance(value, bool) else float(value)
    except (TypeError, ValueError, OverflowError):
        return d
    if v != v:                                 # NaN
        return d
    if e.get("min") is not None and v < e["min"]:
        v = e["min"]
    if e.get("max") is not None and v > e["max"]:
        v = e["max"]
    return v


def defaults():
    """The engine's default config (app.py DEFAULTS): {section: {name: default}}, nested by the key's dots."""
    out = {}
    for key, e in ENTRIES.items():
        if e.get("in_defaults", True):
            *path, name = key.split(".")
            node = out
            for p in path:
                node = node.setdefault(p, {})
            node[name] = json.loads(json.dumps(e["default"]))    # a copy: lists aren't shared between configs
    return out


def clamp_config(cfg):
    """Clamp every setting a loaded config (app.load_config) sets, in place, so reads without Settings agree."""
    for key in ENTRIES:
        *path, name = key.split(".")
        node = cfg
        for p in path:
            node = node.get(p) if isinstance(node, dict) else None
        if isinstance(node, dict) and name in node:
            node[name] = clamp(key, node[name])
    return cfg


def live_keys(section="llm"):
    """The names in `section` the coach takes at once (the dashboard's settings, Save in the desktop app)."""
    prefix = section + "."
    return [k[len(prefix):] for k, e in ENTRIES.items() if k.startswith(prefix) and e.get("live")]


class Settings:
    """One config section read through the manifest. `values` is held, not copied: live changes show at once."""

    def __init__(self, section, values):
        self.section, self.values = section, values

    def get(self, name) -> Any:
        return clamp(self.section + "." + name, self.values.get(name))
