"""Engine golden master, two sections. "sim": the simulator (fixed seed, fake clock) drives the real game-state
decoder, rules, offline coach and knowledge panel; every observable output is recorded. "addon": the payloads the
addon golden recorded (snapshots/addon.json) replayed through GameState, and what summary() reads from each, so one
golden pins what the addon sends and this one how the engine reads it. Used by tests/golden/golden.py to pin
behaviour during the refactor. No network, no voice, no API keys (the coach answers offline)."""
TIER = "machine"     # golden.py tier: reads data/glide.db and data/kb.db
import json
import os
import random
import sys
import time

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
FRAMES = int(os.environ.get("GOLDEN_ENGINE_FRAMES", "2500"))
VOLATILE = {"session_minutes", "on_step_seconds"}       # wall-clock readings (the fake clock pins them anyway)


class _Stop(Exception):
    pass


class FakeClock:
    """time.time / time.sleep / time.monotonic on one deterministic clock; sleep advances it."""

    def __init__(self, t0=1791300000.0):
        self.now = t0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += max(0.0, s)

    def monotonic(self):
        return self.now


def frames():
    """[(seq, payload, clock)] from the simulator: the first FRAMES frames it sends, reproducibly."""
    sys.path.insert(0, os.path.join(ROOT, "companion"))
    import sim
    from glidedb import GlideDB
    clock = FakeClock()
    real = (time.time, time.sleep, time.monotonic)
    time.time, time.sleep, time.monotonic = clock.time, clock.sleep, clock.monotonic
    random.seed(20261025)
    sim.ADDON_VERSION = "0.15.0"           # fixed: an addon version bump must not move this golden
    sim.SPEED = 20.0                       # no pixel round trip for most frames (the protocol has its own tests)
    out = []

    class App:
        k = GlideDB(os.path.join(ROOT, "data", "glide.db"))
        attempts = decoded = 0

        def on_frame(self, seq, payload):
            out.append((seq, payload, clock.now))
            if len(out) >= FRAMES:
                raise _Stop

    try:
        sim.run(App(), {"cols": 96, "rows": 8})
    except _Stop:
        pass
    finally:
        time.time, time.sleep, time.monotonic = real
    return out


def addon_replay():
    """The addon golden's payloads through GameState: per frame the ring events it yields and the summary."""
    sys.path.insert(0, os.path.join(ROOT, "companion"))
    import protocol
    from state import GameState
    snap = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots", "addon.json"),
                          encoding="utf-8"))
    clock = FakeClock()
    real = (time.time, time.sleep, time.monotonic)
    time.time, time.sleep, time.monotonic = clock.time, clock.sleep, clock.monotonic
    try:
        gs, out = GameState(), []
        for f in snap["frames"]:
            clock.now += 0.2                   # the strip's 5 frames a second
            core, page = protocol.parse_payload(f["payload"])
            ring = gs.update(core, page)
            out.append({"at": f["at"], "ring": [list(r) for r in ring],
                        "summary": {k: v for k, v in gs.summary().items() if k not in VOLATILE}})
        return out
    finally:
        time.time, time.sleep, time.monotonic = real


def run():
    return {"sim": sim_run(), "addon": addon_replay()}


def sim_run():
    """-> list of records, one per frame with anything observable, plus periodic panels."""
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "OPENCODE_API_KEY"):
        os.environ[k] = ""
    fr = frames()
    sys.path.insert(0, os.path.join(ROOT, "companion"))
    import protocol
    import rules as rules_mod
    from app import DEFAULTS
    from coach import Coach, load_knowledge_layer
    from gear import Gear
    from glidedb import GlideDB
    from state import GameState
    clock = FakeClock(fr[0][2] if fr else 1791300000.0)
    real = (time.time, time.sleep, time.monotonic)
    time.time, time.sleep, time.monotonic = clock.time, clock.sleep, clock.monotonic
    try:
        kb, _api, _why = load_knowledge_layer(os.path.join(ROOT, "data", "kb.db"))
        msgs = []
        coach = Coach(dict(DEFAULTS["llm"]), GlideDB(os.path.join(ROOT, "data", "glide.db")), msgs.append,
                      kb=kb, offline_ok=True)
        rules = rules_mod.Rules(dict(DEFAULTS["rules"]))
        rules.kb_fn = lambda: coach.pack
        rules.gear = Gear(DEFAULTS["gear"])
        gs = GameState()
        recs = []
        for n, (_seq, payload, t) in enumerate(fr):
            clock.now = t
            core, page = protocol.parse_payload(payload)
            ring = gs.update(core, page)
            if n == 0:
                ring = []                  # the first frame's ring events are old (app.py does the same)
            trig = rules.check(gs, ring)
            flags = gs.coach_flags()
            allowed = [x for x in trig if rules_mod.allowed(x.kind, flags)]
            coach.push([], gs)             # state + bookkeeping, no thread
            before = len(msgs)
            if allowed:
                coach._respond([x for x in allowed if x.kind != "quest_accepted"]) \
                    if any(x.kind != "quest_accepted" for x in allowed) else None
            rec = {}
            if ring:
                rec["ring"] = [list(r) for r in ring]
            if trig:
                rec["triggers"] = [[x.kind, x.facts] for x in trig]
            if len(msgs) > before:
                rec["messages"] = [{k: m.get(k) for k in ("kind", "text", "speak", "urgent") if k in m}
                                   for m in msgs[before:]]
            if n % 50 == 0:
                s = gs.summary()
                rec["summary"] = {k: v for k, v in s.items() if k not in VOLATILE}
                if coach.pack is not None:
                    try:
                        rec["panel"] = coach.pack.panel(s)
                    except Exception as e:  # noqa: BLE001 - an error is an output too
                        rec["panel"] = "error: %r" % e
            if rec:
                rec["n"] = n
                recs.append(rec)
        return recs
    finally:
        time.time, time.sleep, time.monotonic = real


if __name__ == "__main__":                 # python probe_engine.py OUT.json (the engine prints its own log lines)
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump(run(), fh, indent=1, default=str, ensure_ascii=False)
