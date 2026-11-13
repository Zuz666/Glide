// The engine home and its config.json, as the app reads them: where the coach runs, the options it starts with,
// and the parts of its config the app itself acts on (push-to-talk, the overlay's drag key, custom endpoints).
import path from "path";
import { EngineOptions } from "../engine";
import type { AppSettings, AppSettingsPatch, LogAttribution, Voice } from "../../shared/ipc";
import { speechFeatures } from "../speech-markup";
import { usesAppWindowsVoice } from "../../shared/windows-process-voice.mjs";
import { OPENAI_EXCLUSION_RULES } from "../openai-exclusions";
import { defaultHome } from "../paths";
import { secretEnv } from "../secrets";
import { effectiveWhisperModel, languageCode } from "../speech-language";
import { appSettingsProblem, deepMerge, keepBadCopy, Obj, readJson, readJsonFile, unreadableMessage, writeJson } from "../store";
import { settings } from "./settings";

export const engineHome = () => settings.current.engineHome || defaultHome;
export const configPath = () => path.join(engineHome(), "config.json");
export const readConfig = () => readJson(configPath());

let speechCatalogue: (() => Promise<Voice[]>) | undefined;
/** Registration supplies the existing native owner, never another worker or permission store. */
export function setSpeechDestinationCatalogue(catalogue: () => Promise<Voice[]>): void {
  speechCatalogue = catalogue;
}

async function destinationPatch(config: Obj, appSettings: AppSettings, catalogue?: Voice[]): Promise<Obj> {
  const provider = appSettings.speech?.provider || "windows";
  let identity = "";
  if (provider === "windows" && (catalogue || speechCatalogue)) {
    try {
      const voices = catalogue ?? (speechCatalogue ? await speechCatalogue() : []);
      const selected = appSettings.speech?.voiceName || "";
      const voice = selected ? voices.find((v) => v.identity === selected || v.id === selected)
        : voices.find((v) => v.isDefault);
      identity = voice?.identity || "";
    } catch { /* Unknown identity grants nothing; voice failures keep their existing reporting owner. */ }
  }
  const permissions = ((config.companion as Obj | undefined)?.voice as Obj | undefined)?.markup_voices as Obj | undefined;
  const features = identity ? speechFeatures(permissions?.[identity]) : [];
  return { companion: { voice: { markup_destination: { provider, identity, features } } } };
}

/** Refresh the active projection at existing session/catalogue entry points, never by a polling timer. */
export function refreshSpeechDestination(engine: RunningEngine, catalogue?: Voice[]): Promise<Obj> {
  return serializeSettingsWrite(async () => {
    const current = readConfig();
    const patch = await destinationPatch(current, settings.current, catalogue);
    const before = ((current.companion as Obj | undefined)?.voice as Obj | undefined)?.markup_destination;
    const after = ((patch.companion as Obj).voice as Obj).markup_destination;
    return JSON.stringify(before) === JSON.stringify(after) ? current : saveConfigNow(patch, engine, {}, true);
  });
}

/** A non-English speech selection saves a multilingual fallback, even when only language changed. */
function speechModelPatch(config: Obj): Obj | undefined {
  const companion = config.companion as Obj | undefined;
  const language = (companion?.voice as Obj | undefined)?.language;
  const model = (companion?.ptt as Obj | undefined)?.whisper_model;
  if (languageCode(language) === "en") return;
  const effective = effectiveWhisperModel(model, language);
  if (model !== effective) return { companion: { ptt: { whisper_model: effective } } };
}

/** An implicit effort belongs to the previous model; an effort supplied with the new model is intentional. */
function resetModelEfforts(current: Obj, next: Obj, patch: Obj): void {
  const before = (current.companion as Obj | undefined)?.llm as Obj | undefined;
  const after = (next.companion as Obj | undefined)?.llm as Obj | undefined;
  const changes = (patch.companion as Obj | undefined)?.llm as Obj | undefined;
  if (!before || !after) return;
  for (const role of ["fast", "deep"]) {
    const model = `${role}_model`, effort = `${role}_reasoning_effort`;
    if (before[model] !== after[model] && changes?.[effort] === undefined) delete after[effort];
  }
}

function isOfficialOpenAiEndpoint(rawUrl: unknown): boolean {
  if (typeof rawUrl !== "string") return false;
  try {
    const u = new URL(rawUrl.trim());
    const pathName = u.pathname.replace(/\/+$/, "");
    return u.protocol === "https:" && u.hostname.toLowerCase() === "api.openai.com"
      && (u.port === "" || u.port === "443") && pathName === "/v1"
      && !u.username && !u.password && !u.search && !u.hash;
  } catch {
    return false;
  }
}

function isValidIsoCalendarDate(raw: unknown): raw is string {
  if (typeof raw !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(raw)) return false;
  const [y, m, d] = raw.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

function cachedModelShutdownMap(): Map<string, string> {
  const out = new Map<string, string>();
  const cached = readJson(path.join(engineHome(), "data", "live", "models.json"));
  const list = Array.isArray(cached.models) ? cached.models : [];
  for (const row of list) {
    if (row && typeof row === "object" && typeof (row as Obj).id === "string"
        && isValidIsoCalendarDate((row as Obj).shutdown_date)) {
      out.set((row as Obj).id as string, (row as Obj).shutdown_date as string);
    }
  }
  return out;
}

function excludedOpenAiReason(name: string, shutdownDate?: string): string | null {
  if (shutdownDate && shutdownDate <= new Date().toISOString().slice(0, 10)) {
    return `shut down on ${shutdownDate}`;
  }
  const m = name.trim().toLowerCase();
  if (!m) return "empty model id";
  for (const rule of OPENAI_EXCLUSION_RULES) {
    if (rule.prefixes.some((p) => m.startsWith(p))
        || rule.suffixes.some((s) => m.endsWith(s))
        || rule.contains.some((c) => m.includes(c))) {
      return rule.reason;
    }
  }
  return null;
}

function validateOfficialOpenAiPatch(next: Obj, patch: Obj): void {
  const changes = (patch.companion as Obj | undefined)?.llm as Obj | undefined;
  if (!changes) return;
  const after = (next.companion as Obj | undefined)?.llm as Obj | undefined;
  const eps = Array.isArray(after?.endpoints) ? after.endpoints : [];
  const byId = new Map<string, Obj>();
  for (const e of eps) {
    if (e && typeof e === "object" && typeof (e as Obj).id === "string") {
      byId.set(((e as Obj).id as string).trim().toLowerCase(), e as Obj);
    }
  }
  let shutdowns: Map<string, string> | undefined;
  for (const role of ["fast_model", "deep_model"]) {
    const rawCandidate = changes[role];
    if (typeof rawCandidate !== "string") continue;
    const candidate = rawCandidate.trim();
    if (!candidate.startsWith("ep:")) continue;
    const rest = candidate.slice(3);
    const colon = rest.indexOf(":");
    if (colon <= 0) continue;
    const eid = rest.slice(0, colon), mid = rest.slice(colon + 1);
    const ep = byId.get(eid);
    if (!ep || !isOfficialOpenAiEndpoint(ep.base_url)) continue;
    shutdowns ??= cachedModelShutdownMap();
    const reason = excludedOpenAiReason(mid, shutdowns.get(candidate));
    if (reason) throw new Error(`${candidate} cannot be used as a coach model (${reason}).`);
  }
}

/** Merge a patch into config.json, re-read first, when no coach is running (saveConfig otherwise; the first start's
 * WoW folder, before the coach starts). Returns the new config. Throws, leaving the file alone, when config.json is
 * there but unreadable. The optional reporter runs only after the local write succeeds. */
export function updateConfig(patch: Obj, note?: RunningEngine["note"]): Obj {
  const p = configPath();
  const cur = readJsonFile(p);
  if (cur.bad !== undefined) {
    // never save over a config.json that can't be read (a stray comma would cost the WoW folder, endpoints, models)
    throw new Error(unreadableMessage(p, cur.bad, keepBadCopy(p), "this change wasn't saved") + ", then try again.");
  }
  let next = deepMerge(cur.value, patch);
  validateOfficialOpenAiPatch(next, patch);
  resetModelEfforts(cur.value, next, patch);
  const speechPatch = speechModelPatch(next);
  if (speechPatch) next = deepMerge(next, speechPatch);
  writeJson(p, next);
  if (note) {
    const before = (cur.value.companion as Obj | undefined)?.llm as Obj | undefined;
    const after = (next.companion as Obj | undefined)?.llm as Obj | undefined;
    for (const role of ["fast", "deep"]) {
      const model = after?.[`${role}_model`];
      const previous = before?.[`${role}_reasoning_effort`] ?? "auto";
      const reasoning = after?.[`${role}_reasoning_effort`] ?? "auto";
      if (model === before?.[`${role}_model`] && reasoning === previous) continue;
      note(`[coach] Saved ${role}: ${model ?? "default model"}; reasoning ${previous} → ${reasoning} (pending restart).`,
        { category: "AI", operation: "settings", role, status: "saved", application: "pending restart",
          ...(typeof model === "string" ? { model } : {}),
          ...(typeof reasoning === "string" ? { reasoning } : {}),
          ...(typeof previous === "string" ? { previous_reasoning: previous } : {}) });
    }
  }
  return next;
}

/** The running coach, as saveConfig needs it. */
export interface RunningEngine {
  state: string; url: string | null; token: string;
  note(text: string, attribution?: LogAttribution): void;
}

// App voice changes and engine config edits share one order, including across awaited engine saves.
let settingsWrite: Promise<unknown> = Promise.resolve();
function serializeSettingsWrite<T>(write: () => Promise<T>): Promise<T> {
  const result = settingsWrite.then(write);
  settingsWrite = result.catch(() => undefined);
  return result;
}

/** Resolve legacy mode before materializing an inherited name, so the literal never changes its meaning. */
async function persistWindowsProcessVoice(next: AppSettings, engine: RunningEngine): Promise<void> {
  const config = readConfig();
  const currentVoice = ((config.companion as Obj | undefined)?.voice as Obj | undefined)?.voice;
  const linked = usesAppWindowsVoice(next.speech, currentVoice);
  let patch = await destinationPatch(config, next);
  if (linked) {
    const voice = next.speech?.voiceName || "";
    if (currentVoice !== voice) patch = deepMerge(patch, { companion: { voice: { voice } } });
  }
  const projected = (patch.companion as Obj).voice as Obj;
  const current = (config.companion as Obj | undefined)?.voice as Obj | undefined;
  if (JSON.stringify(current?.markup_destination) !== JSON.stringify(projected.markup_destination) || projected.voice !== undefined)
    await saveConfigNow(patch, engine, {}, true);
  next.speech = { ...next.speech, windowsProcessUseAppVoice: linked };
}

/** Persist app changes only after a linked engine voice is confirmed saved by its owner. */
export function saveAppSettingsPatch(patch: AppSettingsPatch, engine: RunningEngine): Promise<AppSettings> {
  return serializeSettingsWrite(async () => {
    const problem = appSettingsProblem();
    if (problem) throw new Error(problem);
    const next = deepMerge(settings.current as Obj, patch as Obj) as AppSettings;
    if (patch.speech !== undefined) await persistWindowsProcessVoice(next, engine);
    // Re-merge the patch, not the awaited snapshot: tray/mute changes may have happened meanwhile.
    return settings.merge({ ...patch, ...(patch.speech !== undefined ? { speech: next.speech } : {}) });
  });
}

/** Before starting Python, refresh the standalone literal from the persisted app link. */
function prepareWindowsProcessVoice(home: string): Promise<void> {
  return serializeSettingsWrite(async () => {
    if (engineHome() !== home) throw new Error("The engine home changed; start the coach again with the current settings.");
    if (appSettingsProblem()) return; // retain the engine's defaults when app settings could not be loaded
    const next = { ...settings.current };
    await persistWindowsProcessVoice(next, { state: "stopped", url: null, token: "", note() {} });
    if (settings.current.speech?.windowsProcessUseAppVoice !== next.speech?.windowsProcessUseAppVoice) {
      settings.merge({ speech: { windowsProcessUseAppVoice: next.speech?.windowsProcessUseAppVoice } });
    }
  });
}

/** Save a patch of config.json. The engine owns the file (docs/REFACTOR-CONTRACTS.md, contract 5): while it runs,
 * it saves the patch (POST /settings {"save": patch}), so its own writes and the app's never cross.
 * Explicit live changes require an engine-confirmed combined save: rejection/disconnection must not become a
 * local save for restart. Ordinary save-only callers retain the existing local fallback. */
export async function saveConfig(patch: Obj, engine: RunningEngine, liveChanges: Obj = {}): Promise<Obj> {
  return serializeSettingsWrite(async () => {
    const projection = await destinationPatch(deepMerge(readConfig(), patch), settings.current);
    return saveConfigNow(deepMerge(patch, projection), engine, liveChanges);
  });
}

async function saveConfigNow(patch: Obj, engine: RunningEngine, liveChanges: Obj = {}, requireEngineSave = false): Promise<Obj> {
  const applying = Object.keys(liveChanges).length > 0;
  if (applying && (engine.state !== "running" || !engine.url)) throw new Error("The coach is not connected; your edits are kept.");
  if (requireEngineSave && engine.state === "running" && !engine.url) throw new Error("The coach is not connected; your edits are kept.");
  if (engine.state === "running" && engine.url) {
    const speechPatch = speechModelPatch(deepMerge(readConfig(), patch));
    if (speechPatch) patch = deepMerge(patch, speechPatch);
    let validationError: Error | undefined;
    try {
      const r = await fetch(engine.url + "/settings", {
        method: "POST", headers: { "X-Glide-Token": engine.token }, body: JSON.stringify({ save: patch, ...liveChanges }),
        signal: AbortSignal.timeout(5000),
      });
      const body = (await r.json()) as { ok?: boolean; error?: string; applied?: { saved?: boolean } };
      if (r.ok && body.applied?.saved === true && (!applying || body.ok === true)) return readConfig();
      if (r.status === 400 || body.ok === false) {
        validationError = new Error(body.error || "The coach refused this settings change; your edits are kept.");
      } else if (applying || requireEngineSave) {
        validationError = new Error(body.error || "The coach did not confirm the settings save; your edits are kept.");
      }
    } catch (error) {
      if (applying || requireEngineSave) throw error;
      // Save-only caller: the coach is going away, so save it here.
    }
    if (validationError) throw validationError;
  }
  // not taken (stopped, starting, or config.json unreadable: updateConfig then says why and leaves the file alone)
  return updateConfig(patch, (text, attribution) => engine.note(text, attribution));
}

/** One section of config.json's "companion" settings ({} when it isn't there). */
export function companionSection(name: string): Obj {
  return (((readConfig().companion as Obj | undefined)?.[name] as Obj | undefined) || {}) as Obj;
}

export function engineOptions(): EngineOptions {
  const a = settings.current.engineArgs || {};
  const home = engineHome();
  return { home, env: secretEnv(), sim: !!a.sim || process.argv.includes("--sim"), noPtt: !!a.noPtt,
           noArchive: !!a.noArchive, noKb: !!a.noKb, prepare: () => prepareWindowsProcessVoice(home) };
}

const ENDPOINT_VENDOR_HOSTS: Record<string, readonly string[]> = {
  ANTHROPIC_API_KEY: ["api.anthropic.com"], OPENROUTER_API_KEY: ["openrouter.ai"],
  GEMINI_API_KEY: ["generativelanguage.googleapis.com"], GOOGLE_API_KEY: ["generativelanguage.googleapis.com"],
  OPENAI_API_KEY: ["api.openai.com"], GROQ_API_KEY: ["api.groq.com"], DEEPGRAM_API_KEY: ["api.deepgram.com"],
  TAVILY_API_KEY: [], BRAVE_API_KEY: [], ELEVENLABS_API_KEY: [], AZURE_SPEECH_KEY: [], GOOGLE_TTS_API_KEY: [],
};

/** Effective keys of custom endpoints in config.json (matching companion/openai_compat.py:endpoint_key_env). */
export function endpointKeyEnvs(): string[] {
  const eps = companionSection("llm").endpoints;
  if (!Array.isArray(eps)) return [];
  const out: string[] = [];
  for (const e of eps) {
    const o = (e || {}) as Obj;
    const id = String(o.id || "").trim().toLowerCase();
    const base = String(o.base_url || "").trim().replace(/\/+$/, "");
    if (!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(id) || !/^https?:\/\//i.test(base)) continue;
    const own = `GLIDE_ENDPOINT_${id.toUpperCase().replace(/-/g, "_")}_KEY`;
    let env = String(o.key_env || own);
    if (!/^[A-Z][A-Z0-9_]{1,63}$/.test(env)) env = own;
    else if (Object.hasOwn(ENDPOINT_VENDOR_HOSTS, env)) {
      let host = "";
      try { host = new URL(base).hostname.toLowerCase(); } catch { host = ""; }
      if (!ENDPOINT_VENDOR_HOSTS[env].includes(host)) env = own;
    }
    if (!out.includes(env)) out.push(env);
  }
  return out;
}

/** A key's value for the app's own requests (voices, endpoint tests): the key store first, then the environment. */
export function keyValue(env: string): string {
  return secretEnv()[env] || process.env[env] || "";
}
