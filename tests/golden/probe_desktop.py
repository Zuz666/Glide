"""Desktop contracts: IPC channels must match the renderer allowlist; saved app settings retain their merge and
write-back behavior. UI wording, copied schema data and internal module export lists are not behavioral contracts.
Builds desktop/dist first (npm run build)."""
TIER = "portable"     # golden.py tier: the desktop sources (needs npm and desktop/node_modules)
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))


def run():
    desk = os.path.join(ROOT, "desktop")
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm or not os.path.isdir(os.path.join(desk, "node_modules")):
        return {"skipped": "needs npm and desktop/node_modules"}
    r = subprocess.run([npm, "run", "build"], cwd=desk, capture_output=True, text=True, shell=os.name == "nt")
    if r.returncode:
        raise RuntimeError("build failed: %s" % (r.stdout + r.stderr)[-2000:])
    out = os.path.join(tempfile.mkdtemp(), "desktop.json")
    r = subprocess.run(["node", os.path.join(HERE, "desktop_probe.cjs"), os.path.join(desk, "dist"), out],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode or not os.path.exists(out):
        raise RuntimeError("desktop_probe failed: %s" % (r.stderr or r.stdout)[-2000:])
    return json.load(open(out, encoding="utf-8"))


if __name__ == "__main__":                 # python probe_desktop.py OUT.json
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump(run(), fh, indent=1, sort_keys=True, ensure_ascii=False)
