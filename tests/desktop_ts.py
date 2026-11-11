"""The desktop app's TypeScript for the tests that run it under Node, compiled once and shared by every test and run.

    node                 the node executable, or None (the tests that need it skip)
    available()          node and desktop/node_modules are there
    renderer()           a folder with renderer/<module>.js and shared/*.js, ES modules: every module in RENDERER
                         compiled together, with the flags each of those tests used to pass tsc on its own
    main()               a folder with main/**/*.js and shared/*.js, CommonJS: tsconfig.main.json, as npm run build
                         compiles it (without source maps)
    compiled(name, files, args)
                         files ({name: text}) written to a folder and compiled with tsc's args (no --outDir): for a
                         module compiled beside stand-ins (test_narrator.py)
    url(path)            the file:/// URL to import() a path (Windows paths too)

A compile goes to <temp>/glide-tsc/<name>-<hash>, the hash covering the installed packages (TypeScript, the type
packages), the arguments and the text of every source the compile can read, so it happens once per content: a test run
compiles the renderer modules and the main process once (not once per test), a later run with the same sources not at
all, and any change to a source compiles again. Tests started together (run_all) wait for the first one's compile
through a lock file (the OS lets go of it if that test dies); a compile goes to a scratch folder that is renamed into
place only when tsc succeeded, so a half-written one is never used. Nothing writes into a finished compile: a test
that needs files beside the output copies it or writes them in its own folder. A failed compile raises AssertionError
with tsc's output, as the tests did."""
import contextlib
import glob
import hashlib
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
DESK = os.path.join(ROOT, "desktop")
MODULES = os.path.join(DESK, "node_modules")
TSC = os.path.join(MODULES, "typescript", "bin", "tsc")
CACHE = os.path.join(tempfile.gettempdir(), "glide-tsc")
KEEP_DAYS = 2                       # a compile no test has used for this long is deleted
node = shutil.which("node")

# the renderer modules tests run, compiled together (each test used to compile its own with these same flags; the
# emitted JavaScript is byte for byte the same). A test that runs another renderer module adds it here.
RENDERER = ("rxp-model", "rxp-geometry", "rxp-icons", "rxp-narration", "rxp-plan", "rxp-prep", "rxp-quests",
            "rxp-route", "settings-schema", "settings/page", "memory-search")
RENDERER_ARGS = ["--target", "ES2023", "--module", "ES2022", "--moduleResolution", "bundler", "--strict",
                 "--skipLibCheck"]


def available():
    return bool(node) and os.path.exists(TSC)


def url(path):
    return pathlib.Path(os.path.abspath(path)).as_uri()


def _tsc():
    """The native compiler itself, found as typescript/lib/getExePath.js finds it (typescript/bin/tsc is a Node script
    that only starts it: a Node start and, on Windows, a process fewer per compile), else that script."""
    plat = {"win32": "win32", "darwin": "darwin"}.get(sys.platform, "linux")
    mach = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(mach, mach)
    exe = os.path.join(MODULES, "@typescript", "typescript-%s-%s" % (plat, arch), "lib",
                       "tsc.exe" if sys.platform == "win32" else "tsc")
    if os.path.isfile(exe) and len(exe) < 248:
        return [exe]
    return [node or "node", TSC]


def _installed():
    """What decides how the same sources compile: the installed packages (npm's record of node_modules, which names
    every package's version: TypeScript, its native compiler, @types/node, electron's types) and the compiler used."""
    h = hashlib.sha256()
    for f in (os.path.join(MODULES, ".package-lock.json"), os.path.join(MODULES, "typescript", "package.json")):
        try:
            with open(f, "rb") as fh:
                h.update(fh.read())
        except OSError:
            h.update(b"-")
        h.update(b"\0")
    h.update(b"native" if len(_tsc()) == 1 else b"launcher")
    return h.hexdigest()


def _digest(*parts, files=()):
    h = hashlib.sha256()
    for p in parts:
        h.update(repr(p).encode("utf-8") + b"\0")
    for f in files:
        h.update(os.path.relpath(f, ROOT).replace(os.sep, "/").encode("utf-8") + b"\0")
        with open(f, "rb") as fh:
            h.update(fh.read() + b"\0")
    return h.hexdigest()[:20]


def _sources(*subdirs):
    return sorted(f for d in subdirs for extension in ("*.ts", "*.mts")
                  for f in glob.glob(os.path.join(DESK, "src", d, "**", extension), recursive=True))


@contextlib.contextmanager
def _locked(path, wait=600):
    """An exclusive lock on the file at path, held while the block runs (and let go by the OS if the process dies)."""
    fh = open(path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            deadline = time.time() + wait
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.02)
            try:
                yield
            finally:
                fh.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
    finally:
        fh.close()


def _touch(folder):
    """Mark a finished compile as in use (not pruned); False when it isn't there (or isn't finished)."""
    try:
        os.utime(os.path.join(folder, ".ok"))
        return True
    except OSError:
        return False


def _prune(keep):
    """Delete compiles (and scratch folders) nothing has used for KEEP_DAYS: the marker first, so a folder that can't
    be deleted whole (a file open on Windows) is never taken for a finished compile."""
    cutoff = time.time() - KEEP_DAYS * 86400
    for entry in os.listdir(CACHE):
        p = os.path.join(CACHE, entry)
        if p == keep or entry.endswith(".lock") or not os.path.isdir(p):
            continue
        try:
            ok = os.path.join(p, ".ok")
            if os.stat(ok if os.path.exists(ok) else p).st_mtime > cutoff:
                continue
            with contextlib.suppress(FileNotFoundError):
                os.remove(ok)
            shutil.rmtree(p, ignore_errors=True)
            with contextlib.suppress(OSError):
                os.remove(p + ".lock")
        except OSError:
            pass


def _cached(name, key, build, esm):
    """The folder for this compile, building it (build(out) -> CompletedProcess of tsc) when it isn't there."""
    final = os.path.join(CACHE, "%s-%s" % (name, key))
    if _touch(final):
        return final
    os.makedirs(CACHE, exist_ok=True)
    with _locked(final + ".lock"):
        if _touch(final):                                  # another test compiled it while this one waited
            return final
        scratch = tempfile.mkdtemp(prefix="%s-tmp-" % name, dir=CACHE)
        try:
            out = os.path.join(scratch, "out")
            r = build(out)
            assert r.returncode == 0, "tsc failed (%s): %s" % (name, (r.stdout + r.stderr)[-2000:])
            if esm:
                with open(os.path.join(out, "package.json"), "w", encoding="utf-8") as fh:
                    fh.write('{"type": "module"}')
            with open(os.path.join(out, ".ok"), "w", encoding="utf-8"):
                pass
            if os.path.exists(final):                      # a compile that was deleted only in part
                shutil.rmtree(final, ignore_errors=True)
            for attempt in range(50):                      # Windows: a virus scan may still hold a new file
                try:
                    os.rename(out, final)
                    break
                except PermissionError:
                    if attempt == 49:
                        raise
                    time.sleep(0.1)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        _prune(final)
    return final


def _run(args, cwd):
    return subprocess.run(_tsc() + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)


def renderer():
    srcs = [os.path.join(DESK, "src", "renderer", m + ".ts") for m in RENDERER]
    key = _digest(_installed(), RENDERER, RENDERER_ARGS, files=_sources("renderer", "shared"))
    return _cached("renderer", key, lambda out: _run(srcs + RENDERER_ARGS + ["--rootDir", os.path.join(DESK, "src"),
                                                                              "--outDir", out], ROOT), esm=True)


def main():
    cfg = os.path.join(DESK, "tsconfig.main.json")
    args = ["-p", cfg, "--sourceMap", "false"]
    key = _digest(_installed(), args, files=[cfg, os.path.join(DESK, "package.json")] + _sources("main", "shared"))
    return _cached("main", key, lambda out: _run(args + ["--outDir", out], DESK), esm=False)


def compiled(name, files, args, esm=True):
    key = _digest(_installed(), sorted(files.items()), args)

    def build(out):
        src = os.path.join(os.path.dirname(out), "src")
        os.makedirs(src)
        for f, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(src, f)), exist_ok=True)
            with open(os.path.join(src, f), "w", encoding="utf-8") as fh:
                fh.write(text)
        return _run(args + ["--outDir", out], src)
    return _cached(name, key, build, esm=esm)
