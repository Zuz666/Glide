#!/usr/bin/env python3
"""The addon's golden wire frames still decode to the recorded engine state, without game databases."""
import json
import os
import subprocess
import sys

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")
sys.path.insert(0, HERE)
import golden  # noqa: E402

with open(os.path.join(HERE, "snapshots", "engine.json"), encoding="utf-8") as fh:
    expected = json.load(fh)["addon"]
# Match the real probe's serialization and fixed hash seed, including stringified sets.
script = "import json; from probe_engine import addon_replay; print(json.dumps(addon_replay(), default=str))"
actual = json.loads(subprocess.check_output(
    [sys.executable, "-c", script], cwd=HERE, env=golden.ENV, text=True, encoding="utf-8", timeout=30))
difference = golden.first_difference(expected, actual)
assert difference is None, "Addon-to-engine golden replay changed: " + str(difference)
print("OK: addon golden frames decode to the recorded engine state without game databases")
