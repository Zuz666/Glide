// Offline speech recognition (Whisper) for push-to-talk: found, and installed by itself when missing.
//
// Neither the speech packages nor the model ship with the installer (together ~650 MB). The first time push-to-talk
// uses Whisper, faster-whisper and its dependencies are installed into userData\pylib (pinned version; the copy of
// numpy pip brings along is removed so the bundled one is used) and the configured model downloads into
// userData\models. Both folders outlive updates and reinstalls, so this happens once; push-to-talk uses Windows
// speech meanwhile. Then the coach restarts to use it.
import { EventEmitter } from "events";
import fs from "fs";
import path from "path";
import type { SpeechStatus, WhisperModel } from "../shared/ipc";
import { Engine, EngineOptions } from "./engine";
import { pylibDir, userModelsDir } from "./paths";
import { effectiveWhisperModel } from "./speech-language";

const CHECK = `
import json, os, sys
name = sys.argv[1]
r = {"package": False, "model": None}
try:
    import faster_whisper
    r["package"] = True
    r["version"] = getattr(faster_whisper, "__version__", "")
except Exception as e:
    r["error"] = "%s: %s" % (type(e).__name__, e)
for d in os.environ.get("GLIDE_WHISPER_DIRS", "").split(os.pathsep):
    p = os.path.join(d, name) if d else ""
    if p and os.path.exists(os.path.join(p, "model.bin")):
        r["model"] = p
        break
if r["package"] and not r["model"]:
    try:
        from faster_whisper.utils import download_model
        r["model"] = download_model(name, local_files_only=True)   # faster-whisper's own cache (setup-voice.cmd)
    except Exception:
        pass
print("GLIDE_SPEECH " + json.dumps(r))
`;

const DOWNLOAD = `
import sys
from faster_whisper.utils import download_model
print("model ready:", download_model(sys.argv[1], output_dir=sys.argv[2]))
`;

/** The Whisper models Settings offers: faster-whisper names, download size (MB, from Hugging Face) and the trade-off. */
export const WHISPER_MODELS: { name: string; mb: number; note: string }[] = [
  { name: "tiny.en", mb: 74, note: "Fastest and smallest; misses more words" },
  { name: "base.en", mb: 141, note: "Fast; fine for short commands" },
  { name: "distil-small.en", mb: 320, note: "Close to small.en, a little faster" },
  { name: "small.en", mb: 464, note: "Recommended for English: accurate, about a second per question" },
  { name: "distil-medium.en", mb: 755, note: "More accurate; slower on older CPUs" },
  { name: "medium.en", mb: 1460, note: "Most accurate English; slow on CPU" },
  { name: "small", mb: 464, note: "Multilingual; recommended for non-English speech" },
];
const MODEL_MB: Record<string, number> = Object.fromEntries(WHISPER_MODELS.map((m) => [m.name, m.mb]));

const hasModel = (dir: string) => fs.existsSync(path.join(dir, "model.bin"));

/** Each offered model and whether it's on this PC (downloaded, or shipped in a folder of `dirs`). */
export function listWhisperModels(dirs: string[]): WhisperModel[] {
  return WHISPER_MODELS.map((m) => {
    const shipped = dirs.slice(1).some((d) => hasModel(path.join(d, m.name)));
    return { ...m, shipped, downloaded: shipped || hasModel(path.join(userModelsDir(), m.name)) };
  });
}

/** Total size of the files under dir (the download writes partial files inside it). */
function dirBytes(dir: string): number {
  let n = 0;
  try {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      n += e.isDirectory() ? dirBytes(p) : fs.statSync(p).size;
    }
  } catch {
    /* gone or not there yet */
  }
  return n;
}

/** The faster-whisper release installed on first use (tested with the bundled Python 3.12 and numpy). */
const FASTER_WHISPER = "1.2.1";

/** pip --target brings its own numpy along; the bundled one (Python's site-packages) must win, so remove the copy. */
function dropDuplicateNumpy(): void {
  try {
    for (const e of fs.readdirSync(pylibDir())) {
      if (/^numpy($|[-.])/i.test(e)) fs.rmSync(path.join(pylibDir(), e), { recursive: true, force: true });
    }
  } catch {
    /* nothing installed */
  }
}

/** Whether push-to-talk uses Whisper with this config (online engines keep it as their fallback). */
export function wantsWhisper(ptt: Record<string, unknown>, noPtt: boolean): boolean {
  if (noPtt || ptt.enabled === false) return false;
  return String(ptt.engine || "auto") !== "windows";
}

export class Speech extends EventEmitter {
  status: SpeechStatus = { state: "checking", modelName: "small.en" };
  private running: Promise<void> | null = null;

  constructor(private engine: Engine) {
    super();
  }

  private set(s: Partial<SpeechStatus>): void {
    this.status = { ...this.status, ...s };
    this.emit("status", this.status);
  }

  async check(opts: EngineOptions, modelName: string): Promise<SpeechStatus> {
    this.set({ state: "checking", modelName, detail: undefined });
    const r = await this.engine.capture(["-c", CHECK, modelName], opts, 120_000);
    const line = r.out.split(/\r?\n/).find((l) => l.startsWith("GLIDE_SPEECH "));
    if (!line) {
      this.set({ state: "error", detail: `the check failed: ${r.out.trim().slice(-300)}` });
      return this.status;
    }
    const d = JSON.parse(line.slice(13));
    this.set({ state: d.package && d.model ? "ready" : "error", package: d.package, version: d.version, model: d.model,
               detail: !d.package ? `faster-whisper missing (${d.error || "not installed"})` : !d.model ? `model ${modelName} not downloaded` : undefined });
    return this.status;
  }

  /** Check, and install / download what's missing. onInstalled runs when something new was put in place. */
  ensure(opts: EngineOptions, ptt: Record<string, unknown>, onInstalled: () => void, force = false, language: unknown = "en"): Promise<void> {
    // Only a model this app knows: the name becomes a folder name and a download (config.json is the player's).
    const asked = effectiveWhisperModel(ptt.whisper_model, language);
    const modelName = MODEL_MB[asked] ? asked : effectiveWhisperModel(undefined, language);
    if (this.running) {
      return this.status.modelName === modelName ? this.running
        : this.running.then(() => this.ensure(opts, ptt, onInstalled, force, language));
    }
    if (!force && !wantsWhisper(ptt, !!opts.noPtt)) {
      this.set({ state: "off", modelName, detail: "push-to-talk doesn't use Whisper with these settings" });
      return Promise.resolve();
    }
    return this.runAlone(() => this.installMissing(opts, modelName, onInstalled));
  }

  /** Install faster-whisper when it's missing, then the model; onInstalled when either was put in place and works. */
  private async installMissing(opts: EngineOptions, modelName: string, onInstalled: () => void): Promise<void> {
    let st = await this.check(opts, modelName);
    let changed = false;
    if (!st.package) {
      this.set({ state: "installing", detail: "installing offline speech recognition (one time, about 120 MB)…" });
      this.engine.note("[speech] offline speech recognition isn't installed yet: installing faster-whisper " + FASTER_WHISPER);
      const code = await this.engine.pipInstall([`faster-whisper==${FASTER_WHISPER}`], opts);
      dropDuplicateNumpy();
      if (code !== 0) return this.set({ state: "error", detail: "installing offline speech recognition failed: check the internet connection, then Check and repair (see Logs)" });
      changed = true;
      st = await this.check(opts, modelName);
    }
    if (st.package && !st.model) {
      if (!(await this.fetchModel(modelName))) return;
      changed = true;
      st = await this.check(opts, modelName);
    }
    if (st.state === "ready") this.engine.note(`[speech] Whisper ready: ${modelName} (${st.model})`);
    if (changed && st.state === "ready") onInstalled();
  }

  /** Run one install or download at a time: callers check `running` first and get the one under way instead. */
  private runAlone(job: () => Promise<void>): Promise<void> {
    this.running = job().finally(() => (this.running = null));
    return this.running;
  }

  /** Download one model into userData\models, with progress in the status. False (and an error status) on failure. */
  private async fetchModel(name: string): Promise<boolean> {
    const dst = path.join(userModelsDir(), name);
    fs.mkdirSync(dst, { recursive: true });
    // a model fetched ahead of time leaves the one in use (and its state) alone; only its own row and the footer move
    const forUse = name === this.status.modelName;
    this.set({ failed: undefined, ...(forUse ? { state: "downloading" as const, detail: `downloading the ${name} speech model (one time)…` } : {}),
               fetching: name, gotMB: 0, totalMB: MODEL_MB[name] });
    this.engine.note(`[speech] downloading the Whisper model ${name} to ${dst}`);
    const tick = setInterval(() => this.set({ gotMB: Math.round(dirBytes(dst) / 1048576) }), 1000);
    const code = await this.engine.runScript(["-c", DOWNLOAD, name, dst], "speech model", false).finally(() => clearInterval(tick));
    this.set({ fetching: undefined, gotMB: undefined, totalMB: undefined });
    if (code === 0 && hasModel(dst)) return true;
    const detail = `downloading the ${name} speech model failed: check the internet connection, then Download again`;
    this.set(forUse ? { state: "error", detail } : { failed: detail });
    return false;
  }

  /** Download a model on request without switching to it, then report on the one in use again. */
  download(opts: EngineOptions, name: string): Promise<void> {
    if (this.running) return this.running;
    if (!MODEL_MB[name]) return Promise.resolve();
    const inUse = this.status.modelName;
    return this.runAlone(async () => {
      if (await this.fetchModel(name)) await this.check(opts, inUse);
    });
  }

  /** Remove a downloaded model to free disk space; never the one in use, never one shipped with the app. */
  remove(name: string): boolean {
    if (name === this.status.modelName || this.status.fetching === name || !MODEL_MB[name]) return false;
    fs.rmSync(path.join(userModelsDir(), name), { recursive: true, force: true });
    this.engine.note(`[speech] deleted the Whisper model ${name}`);
    return true;
  }
}
