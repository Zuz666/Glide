"""config.json, the player's settings file in the engine home: the one reader and writer of it for the engine, the
knowledge build and the tools (knowledge/ and tools/ put companion/ on sys.path to import it).

    glideconfig.load()                                 the engine's settings ("companion"): defaults merged, clamped
    glideconfig.wow_dir()                              the WoW client folder ("wow_dir"), or None
    glideconfig.patch(("companion", "llm"), {...})     merge values into one section and save
    glideconfig.drop_retired()                         take the settings no longer used out of the file

Readers never raise: a missing or unreadable file reads as empty (so the defaults). A writer re-reads the file first,
merges, and writes it whole through a temporary file; it leaves a file it can't read alone (saving over it would
lose everything else in it). Who writes it when: docs/REFACTOR-CONTRACTS.md, contract 5.
"""
import copy
import os
import threading

import jsonfile
import settings

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_write_lock = threading.Lock()          # the engine's own writers (dashboard picks, the app's saves) take turns


def path(root=None):
    return os.path.join(root or ROOT, "config.json")


def read(root=None):
    """(the file's object, None), or ({}, why it can't be read). A missing file is ({}, None)."""
    p = path(root)
    if not os.path.exists(p):
        return {}, None
    try:
        data = jsonfile.read(p)                 # Notepad may save it with a BOM: fine
    except (OSError, ValueError) as e:
        return {}, str(e)
    if not isinstance(data, dict):
        return {}, "not a JSON object"
    return data, None


def load(root=None, log=None):
    """The engine's settings: every default (settings_manifest.json) with config.json's "companion" sections merged
    over them (one level deep) and clamped to their ranges. An unreadable file means the defaults, and a line for
    `log` saying why (the file stays as it is)."""
    cfg = settings.defaults()
    data, bad = read(root)
    if bad and log:
        log("[config] can't read %s (%s): using the default settings until it is fixed" % (path(root), bad))
    user = data.get("companion")
    for k, v in (user.items() if isinstance(user, dict) else ()):
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    for key in settings.RETIRED:               # (settings no longer used: nothing of them is read)
        *where, name = key.split(".")
        node = cfg
        for p in where:
            node = node.get(p) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(name, None)
    return settings.clamp_config(cfg)          # a hand edit out of range reads as the nearest end of it


def drop_retired(root=None):
    """Take the settings no longer used (settings.RETIRED) out of config.json's "companion" sections, if it has any
    (written only then). -> the keys taken out. A file that can't be read is left alone."""
    data, bad = read(root)
    if bad:
        return []
    user = data.get("companion")
    gone = []
    for key in settings.RETIRED:
        *where, name = key.split(".")
        node = user
        for p in where:
            node = node.get(p) if isinstance(node, dict) else None
        if isinstance(node, dict) and name in node:
            patch(("companion",) + tuple(where), {name: None}, root)
            gone.append(key)
    return gone


def wow_dir(root=None):
    """The WoW client flavour folder set in config.json ("wow_dir"), or None (not set, or the file is missing or
    unreadable). Whether the folder exists is the caller's business."""
    w = read(root)[0].get("wow_dir")
    return w if isinstance(w, str) and w else None


def merge(base, changes):
    """Merge `changes` into `base` in place, as the desktop app's deepMerge does: None removes a key, an object
    merges into an object, anything else replaces."""
    for k, v in changes.items():
        if v is None:
            base.pop(k, None)
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


def _llm(config):
    companion = config.get("companion")
    llm = companion.get("llm") if isinstance(companion, dict) else None
    return llm if isinstance(llm, dict) else {}


def patch(section, values, root=None):
    """Merge `values` into config.json under `section` (a tuple of keys; () for the whole file) and save it,
    keeping everything else. Re-reads the file first (the desktop app may have written it); raises ValueError,
    leaving the file as it is, when it can't be read. Returns the saved object."""
    p = path(root)
    with _write_lock:
        data, bad = read(root)
        if bad:
            raise ValueError("%s can't be read (%s), so the change wasn't saved and the file was left as it is"
                             % (p, bad))
        old_llm = _llm(data)
        old_models = {role: old_llm.get(role + "_model") for role in ("fast", "deep")}
        root_changes = values
        for name in reversed(section):
            root_changes = {name: root_changes}
        node = data
        for k in section:
            if not isinstance(node.get(k), dict):
                node[k] = {}
            node = node[k]
        merge(node, values)
        # Save-and-restart bypasses live tuning; never carry the old model's implicit effort into the new one.
        next_llm, requested = _llm(data), _llm(root_changes)
        for role, previous in old_models.items():
            effort = role + "_reasoning_effort"
            if previous != next_llm.get(role + "_model") and effort not in requested:
                next_llm.pop(effort, None)
        jsonfile.write_atomic(p, data, end="\n", indent=2)
        return data
