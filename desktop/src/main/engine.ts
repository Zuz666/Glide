// Runs the Python companion ("the engine") as a child process: companion/app.py --app --port N.
//
// The engine serves the dashboard and an event stream on 127.0.0.1:N; the app's windows load / subscribe to it.
// It prints "GLIDE_READY port=N app=V" once it listens, and stops when its stdin closes (so it never outlives the app).
// The app's version goes in the env (GLIDE_APP_VERSION): the engine has none of its own, so its Status page and state
// ("versions") name the app's.
// Each start gets a new random token (env GLIDE_TOKEN, not on the command line): the engine takes POSTs only with it
// (header X-Glide-Token), so a web page open in a browser can't drive the coach.
// A first start without data/kb.db builds glide.db and kb.db first, like start-glide.cmd did.
import { ChildProcess, spawn, SpawnOptions } from "child_process";
import crypto from "crypto";
import { EventEmitter } from "events";
import fs from "fs";
import net from "net";
import path from "path";
import { app } from "electron";
import type { EngineRunState, LogAttribution, LogLine } from "../shared/ipc";
import { formatLogText, normalizeLogAttribution, parseLogEnvelope } from "../shared/logging.mjs";
import type { LogFile } from "./logfile";
import { pylibDir, pythonCommand, syncEngine, whisperDirs } from "./paths";

export interface EngineOptions {
  home: string;                         // engine home: companion/, knowledge/, tools/, data/, config.json
  env: Record<string, string>;          // API keys and the like
  sim?: boolean;
  noPtt?: boolean;
  noArchive?: boolean;
  noKb?: boolean;
  prepare?: () => Promise<void>;        // app-owned config synchronization, before Python can own the file
}

const MAX_LOG = 3000;

/** companion/app.py's switches for the options that have one. */
const SWITCHES: [keyof EngineOptions, string][] = [["sim", "--sim"], ["noPtt", "--no-ptt"], ["noArchive", "--no-archive"], ["noKb", "--no-kb"]];

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.unref();
    s.on("error", reject);
    s.listen(0, "127.0.0.1", () => {
      const port = (s.address() as net.AddressInfo).port;
      s.close(() => resolve(port));
    });
  });
}

export class Engine extends EventEmitter {
  state: EngineRunState = "stopped";
  port = 0;
  token = "";
  detail = "";
  log: LogLine[] = [];
  /** Where every log line is kept on disk too (main.ts: userData/logs); none in tests. */
  file: LogFile | null = null;
  private proc: ChildProcess | null = null;
  private opts: EngineOptions | null = null;
  private wantRunning = false;
  private restarts: number[] = [];
  private restartTimer: NodeJS.Timeout | null = null;

  get url(): string | null {
    return this.state === "running" ? `http://127.0.0.1:${this.port}` : null;
  }

  private setState(s: EngineRunState, detail = ""): void {
    this.state = s;
    this.detail = detail;
    this.emit("state", { state: s, detail, port: this.port, url: this.url, token: this.token });
  }

  private addLog(stream: LogLine["stream"], text: string, attribution?: LogAttribution): void {
    const metadata = normalizeLogAttribution(attribution);
    for (const line of metadata ? [text] : text.split(/\r?\n/)) {
      if (!line.trim()) continue;
      const parsed = metadata ? { text: line, attribution: metadata } : stream === "out" ? parseLogEnvelope(line) : { text: line };
      const l: LogLine = { t: Date.now(), stream, ...parsed };
      this.log.push(l);
      this.file?.write(`[${stream}] ${formatLogText(l.text, l.attribution)}`);
      this.emit("log", l);
    }
    if (this.log.length > MAX_LOG) this.log.splice(0, this.log.length - MAX_LOG);
  }

  note(text: string, attribution?: LogAttribution): void {
    this.addLog("app", text, attribution);
  }

  /** Decode UTF-8 and assemble full process lines before inspecting structured stdout. */
  private pipeLogs(p: ChildProcess, stdoutLine?: (line: string) => void, stdout = true): void {
    for (const [source, stream] of [[p.stdout, "out"], [p.stderr, "err"]] as const) {
      if (!source || (stream === "out" && !stdout)) continue;
      let pending = "";
      const emit = (line: string) => {
        if (stream === "out") stdoutLine?.(line);
        this.addLog(stream, line);
      };
      source.setEncoding("utf8");
      source.on("data", (chunk: string) => {
        pending += chunk;
        let newline: number;
        while ((newline = pending.indexOf("\n")) !== -1) {
          emit(pending.slice(0, newline).replace(/\r$/, ""));
          pending = pending.slice(newline + 1);
        }
      });
      const flush = () => {
        if (pending) emit(pending);
        pending = "";
      };
      source.on("end", flush);
      p.on("close", flush);
    }
  }

  private env(o: EngineOptions): NodeJS.ProcessEnv {
    const py = pythonCommand();
    const env: NodeJS.ProcessEnv = { ...process.env, ...o.env, PYTHONUTF8: "1", PYTHONUNBUFFERED: "1",
                                      PYTHONIOENCODING: "utf-8", GLIDE_WHISPER_DIRS: whisperDirs().join(path.delimiter) };
    if (py.bundled) {
      // Pin the standard library and DLLs too: Windows may otherwise use another registered Python.
      env.PYTHONHOME = path.dirname(py.cmd);
      env.PYTHONNOUSERSITE = "1";
      env.PYTHONPATH = pylibDir();
    } else {
      env.PYTHONPATH = [pylibDir(), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
    }
    return env;
  }

  /** Start Python in the engine home with the engine's environment: `args` follow the interpreter's own. */
  private spawnPython(args: string[], o: EngineOptions, extra: SpawnOptions = {}): ChildProcess {
    const py = pythonCommand();
    return spawn(py.cmd, [...py.args, ...args], { cwd: o.home, env: this.env(o), windowsHide: true, ...extra });
  }

  /** Run one Python script in the engine home to completion (the first-start builds, pip). */
  runScript(args: string[], label: string, track = true): Promise<number> {
    const o = this.opts!;
    const py = pythonCommand();
    this.note(`${label}: ${[py.cmd, ...py.args, ...args].join(" ")}`);
    return new Promise((resolve) => {
      const p = this.spawnPython(args, o);
      if (track) this.proc = p;
      this.pipeLogs(p);
      p.on("error", (e) => {
        this.note(`${label} failed to start: ${e.message}`);
        if (track && this.proc === p) this.proc = null;     // no 'exit' follows a failed launch
        resolve(-1);
      });
      p.on("exit", (code) => {
        if (track) this.proc = null;
        resolve(code ?? -1);
      });
    });
  }

  /** Run one of the engine's Python tools in the engine home with `input` on its stdin, and answer the JSON object
   * it prints last (the lines before it and its stderr go to the log). The API keys stay out of its environment. */
  pythonJson(args: string[], input: string, o: EngineOptions): Promise<Record<string, unknown>> {
    return new Promise((resolve, reject) => {
      const p = this.spawnPython(args, { ...o, env: {} }, { stdio: ["pipe", "pipe", "pipe"] });
      let out = "";
      p.stdout?.setEncoding("utf8");
      p.stdout?.on("data", (d: string) => (out += d));
      this.pipeLogs(p, undefined, false);
      p.on("error", reject);
      p.on("exit", () => {
        const lines = out.trim().split(/\r?\n/);
        const answer = lines.pop() || "";
        if (lines.length) this.addLog("out", lines.join("\n"));
        try {
          resolve(JSON.parse(answer));
        } catch {
          reject(new Error(`${args[0]} gave no answer`));
        }
      });
      p.stdin?.on("error", () => undefined);       // a tool that exits before reading all of it
      p.stdin?.end(input, "utf8");
    });
  }

  async start(o: EngineOptions): Promise<void> {
    if (this.proc) return;
    this.opts = o;
    this.wantRunning = true;
    // the engine home may have changed since the app started (Settings > Data): put the engine code there first
    try {
      syncEngine(o.home, app.getVersion()).forEach((d) => this.note(d));
      await o.prepare?.();
    } catch (e) {
      this.wantRunning = false;
      this.note((e as Error).message);
      this.setState("crashed", (e as Error).message);
      return;
    }
    if (!this.wantRunning) return this.setState("stopped");
    if (!fs.existsSync(path.join(o.home, "companion", "app.py"))) {
      this.setState("crashed", `no engine in ${o.home}`);
      return;
    }
    if (!o.sim && !o.noKb && !fs.existsSync(path.join(o.home, "data", "kb.db"))) {
      this.setState("building", "first start: building the quest database and knowledge base (a minute or two)");
      const a = await this.runScript([path.join("tools", "build_db.py")], "quest database");
      if (a !== 0) this.note("the quest database step reported a problem (see above)");
      if (!this.wantRunning) return this.setState("stopped");
      const b = await this.runScript([path.join("knowledge", "build_kb.py"), "--if-stale"], "knowledge base");
      if (b !== 0) this.note("the knowledge base step reported a problem; starting the coach anyway");
      if (!this.wantRunning) return this.setState("stopped");
    }
    this.port = await freePort();
    this.token = crypto.randomBytes(24).toString("hex");
    const py = pythonCommand();
    const args = [path.join("companion", "app.py"), "--app", "--port", String(this.port),
                  ...SWITCHES.filter(([opt]) => o[opt]).map(([, flag]) => flag)];
    this.setState("starting", py.bundled ? "bundled Python" : `system Python (${py.cmd})`);
    this.file?.header("coach start", `app: ${app.getVersion()}`, `engine home: ${o.home}`,
                      `python: ${[py.cmd, ...py.args].join(" ")}${py.bundled ? " (bundled)" : ""}`);
    this.note(`starting the coach: ${[py.cmd, ...py.args, ...args].join(" ")}  (in ${o.home})`);
    const p = this.spawnPython(args, o, { stdio: ["pipe", "pipe", "pipe"], env: { ...this.env(o), GLIDE_TOKEN: this.token, GLIDE_APP_VERSION: app.getVersion() } });
    this.proc = p;
    this.pipeLogs(p, (line) => {
      const ready = this.state === "starting" ? line.match(/GLIDE_READY port=\d+[^\r\n]*/) : null;
      if (ready) {
        this.file?.header("coach ready", ready[0]);
        this.setState("running", o.sim ? "simulation" : "");
      }
    });
    p.on("error", (e) => {
      // a Python that can't be launched (missing, blocked) gives 'error' and 'close' but never 'exit': without this
      // the coach would stay "starting" and stop() would wait forever
      if (p.pid !== undefined) return this.note(`the coach process: ${e.message}`);
      this.note(`could not start Python: ${e.message}. Install Python 3.12 or use the installed app (bundled Python).`);
      if (this.proc !== p) return;          // stopped meanwhile
      this.proc = null;
      this.wantRunning = false;
      this.setState("crashed", `could not start Python (${e.message})`);
    });
    p.on("exit", (code, sig) => {
      this.proc = null;
      this.note(`the coach exited (${sig || `code ${code}`})`);
      if (!this.wantRunning) return this.setState("stopped");
      this.setState("crashed", `exited with ${sig || `code ${code}`}`);
      this.scheduleRestart();
    });
  }

  /** Crashes restart after 2, 4, 8 ... s; more than 5 within 2 minutes stops trying. */
  private scheduleRestart(): void {
    const now = Date.now();
    this.restarts = this.restarts.filter((t) => now - t < 120_000);
    if (this.restarts.length >= 5) {
      this.note("the coach keeps crashing; not restarting it. See the log above, then use Start.");
      this.wantRunning = false;
      return;
    }
    this.restarts.push(now);
    const delay = 2000 * 2 ** (this.restarts.length - 1);
    this.note(`restarting in ${delay / 1000}s`);
    this.restartTimer = setTimeout(() => {
      this.restartTimer = null;
      if (this.wantRunning && this.opts) this.start(this.opts);
    }, delay);
  }

  /** Close stdin (the engine exits on its own), then kill it if it hasn't after 4 s; give up waiting 3 s later. */
  stop(): Promise<void> {
    this.wantRunning = false;
    if (this.restartTimer) clearTimeout(this.restartTimer);
    this.restartTimer = null;
    const p = this.proc;
    if (!p) {
      this.setState("stopped");
      return Promise.resolve();
    }
    if (p.pid === undefined || p.exitCode !== null || p.signalCode !== null) {
      // it never started, or has already gone: no 'exit' is coming
      this.proc = null;
      this.setState("stopped");
      return Promise.resolve();
    }
    this.setState("stopping");
    return new Promise((resolve) => {
      let giveUp: NodeJS.Timeout | undefined;
      const done = () => {
        clearTimeout(timer);
        clearTimeout(giveUp);
        if (this.proc === p) this.proc = null;
        if (this.state === "stopping") this.setState("stopped");
        resolve();
      };
      const timer = setTimeout(() => {
        if (p.pid && process.platform === "win32") spawn("taskkill", ["/pid", String(p.pid), "/t", "/f"], { windowsHide: true });
        else p.kill("SIGKILL");
        // quitting the app waits for this: never hang on a process that won't say it's gone
        giveUp = setTimeout(() => {
          this.note("the coach didn't confirm it stopped; carrying on without it");
          done();
        }, 3000);
      }, 4000);
      p.once("exit", done);
      p.once("close", done);
      try {
        p.stdin?.end();
      } catch {
        p.kill();
      }
    });
  }

  async restart(o: EngineOptions): Promise<void> {
    await this.stop();
    this.restarts = [];
    await this.start(o);
  }

  /** Run a short Python snippet in the engine home and return its stdout (detection checks). */
  capture(args: string[], opts: EngineOptions, timeoutMs = 60_000): Promise<{ code: number; out: string }> {
    return new Promise((resolve) => {
      const p = this.spawnPython(args, opts);
      let out = "";
      const timer = setTimeout(() => p.kill(), timeoutMs);
      p.stdout?.on("data", (d) => (out += d.toString("utf8")));
      p.stderr?.on("data", (d) => (out += d.toString("utf8")));
      p.on("error", () => resolve({ code: -1, out }));
      p.on("exit", (code) => {
        clearTimeout(timer);
        resolve({ code: code ?? -1, out });
      });
    });
  }

  /** pip install into userData/pylib (on the engine's PYTHONPATH), e.g. faster-whisper for push-to-talk. */
  async pipInstall(pkgs: string[], opts: EngineOptions): Promise<number> {
    this.opts = this.opts || opts;
    fs.mkdirSync(pylibDir(), { recursive: true });
    return this.runScript(["-m", "pip", "install", "--upgrade", "--target", pylibDir(), ...pkgs], "pip", false);
  }
}
