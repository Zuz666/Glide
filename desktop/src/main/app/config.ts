// The engine home and its config.json, as the app reads them: where the coach runs, the options it starts with,
// and the parts of its config the app itself acts on (push-to-talk, the overlay's drag key, custom endpoints).
import path from "path";
import { EngineOptions } from "../engine";
import type { LogAttribution } from "../../shared/ipc";
import { defaultHome } from "../paths";
import { secretEnv } from "../secrets";
import { effectiveWhisperModel, languageCode } from "../speech-language";
import { deepMerge, keepBadCopy, Obj, readJson, readJsonFile, unreadableMessage, writeJson } from "../store";
import { settings } from "./settings";

export const engineHome = () => settings.current.engineHome || defaultHome;
export const configPath = () => path.join(engineHome(), "config.json");
export const readConfig = () => readJson(configPath());

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

/** Save a patch of config.json. The engine owns the file (docs/REFACTOR-CONTRACTS.md, contract 5): while it runs,
 * it saves the patch (POST /settings {"save": patch}), so its own writes (model picks, sliders) and the app's never
 * cross. With no coach running (or one that didn't take it), the app merges it in itself (updateConfig). */
export async function saveConfig(patch: Obj, engine: RunningEngine): Promise<Obj> {
  if (engine.state === "running" && engine.url) {
    const speechPatch = speechModelPatch(deepMerge(readConfig(), patch));
    if (speechPatch) patch = deepMerge(patch, speechPatch);
    try {
      const r = await fetch(engine.url + "/settings", {
        method: "POST", headers: { "X-Glide-Token": engine.token }, body: JSON.stringify({ save: patch }),
        signal: AbortSignal.timeout(5000),
      });
      const body = (await r.json()) as { ok?: boolean; applied?: { saved?: boolean } };
      if (r.ok && body.applied?.saved === true) return readConfig();
    } catch { /* the coach is going away: save it here */ }
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
  return { home: engineHome(), env: secretEnv(), sim: !!a.sim || process.argv.includes("--sim"), noPtt: !!a.noPtt,
           noArchive: !!a.noArchive, noKb: !!a.noKb };
}

/** Keys of custom endpoints in config.json (llm.endpoints[].key_env), so the settings page can set them. */
export function endpointKeyEnvs(): string[] {
  const eps = companionSection("llm").endpoints;
  if (!Array.isArray(eps)) return [];
  return eps.map((e) => {
    const o = (e || {}) as Obj;
    return String(o.key_env || `GLIDE_ENDPOINT_${String(o.id || "").toUpperCase().replace(/-/g, "_")}_KEY`);
  }).filter((k) => /^[A-Z][A-Z0-9_]{1,63}$/.test(k));
}

/** A key's value for the app's own requests (voices, endpoint tests): the key store first, then the environment. */
export function keyValue(env: string): string {
  return secretEnv()[env] || process.env[env] || "";
}
