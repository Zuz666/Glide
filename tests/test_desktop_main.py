#!/usr/bin/env python3
"""The desktop app's main process (the tests' shared compile, desktop_ts.py) copied into a temp folder and run under
Node without Electron (desktop_main_check.cjs): a Python that can't be launched leaves the coach "crashed" with the reason and lets the app
quit (it used to hang in "starting" and stop() never returned); a changed engine home gets the engine code when the
coach starts; code folders Glide didn't install are never deleted; an unreadable config.json is never saved over."""
import os
import shutil
import subprocess
import sys
import tempfile

import desktop_ts

HERE = os.path.dirname(os.path.abspath(__file__))

node = desktop_ts.node
if not node or not desktop_ts.available():
    print("SKIP: needs node and desktop/node_modules")
    sys.exit(77)
tmp = tempfile.mkdtemp(prefix="glide-desktop-main-")
try:
    # the check runs the main process from <tmp>/desktop/dist/main (paths.devRoot is <tmp>) and writes beside it
    shutil.copytree(desktop_ts.main(), os.path.join(tmp, "desktop", "dist"))
    env = os.environ.copy()
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY"):
        env[key] = ""
    r = subprocess.run([node, os.path.join(HERE, "desktop_main_check.cjs"), tmp], capture_output=True, text=True, encoding="utf-8",
                       timeout=120, env=env)
    print(r.stdout + r.stderr)
    assert r.returncode == 0 and "ok" in r.stdout, "desktop_main_check failed"
finally:
    shutil.rmtree(tmp, ignore_errors=True)
