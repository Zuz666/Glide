// Shared Settings deep-link helpers: jump buttons to API keys or subsections when credentials/endpoints are missing.
import { quiet } from "../bridge.js";
import { esc } from "../dom.js";
import { Obj } from "../types.js";

export interface EndpointRef { id: string; label?: string; base_url: string; key_env?: string }

export const TTS_PROVIDER_KEY: Record<string, string> = {
  openai: "OPENAI_API_KEY",
  elevenlabs: "ELEVENLABS_API_KEY",
  azure: "AZURE_SPEECH_KEY",
  google: "GOOGLE_TTS_API_KEY",
};

export const STT_ENGINE_KEY: Record<string, string> = {
  openai: "OPENAI_API_KEY",
  groq: "GROQ_API_KEY",
  deepgram: "DEEPGRAM_API_KEY",
};

const VENDOR_KEY_HOSTS: Record<string, readonly string[]> = {
  ANTHROPIC_API_KEY: ["api.anthropic.com"],
  OPENROUTER_API_KEY: ["openrouter.ai"],
  GEMINI_API_KEY: ["generativelanguage.googleapis.com"],
  GOOGLE_API_KEY: ["generativelanguage.googleapis.com"],
  OPENAI_API_KEY: ["api.openai.com"],
  GROQ_API_KEY: ["api.groq.com"],
  DEEPGRAM_API_KEY: ["api.deepgram.com"],
  TAVILY_API_KEY: [],
  BRAVE_API_KEY: [],
  ELEVENLABS_API_KEY: [],
  AZURE_SPEECH_KEY: [],
  GOOGLE_TTS_API_KEY: [],
};
const KEY_ENV_RE = /^[A-Z][A-Z0-9_]{1,63}$/;
const ENDPOINT_ID_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;

/** Effective credential variable matching companion/openai_compat.py:endpoint_key_env. */
export function endpointKeyEnv(e: { id?: string; base_url?: string; key_env?: string }): string {
  const id = String(e.id || "").trim().toLowerCase();
  const own = `GLIDE_ENDPOINT_${id.toUpperCase().replace(/-/g, "_")}_KEY`;
  const env = String(e.key_env || own);
  if (!KEY_ENV_RE.test(env)) return own;
  const hosts = Object.hasOwn(VENDOR_KEY_HOSTS, env) ? VENDOR_KEY_HOSTS[env] : undefined;
  if (hosts !== undefined) {
    let host = "";
    try { host = new URL(String(e.base_url || "").trim()).hostname.toLowerCase(); } catch { host = ""; }
    if (!hosts.includes(host)) return own;
  }
  return env;
}
export function isLocalBaseUrl(baseUrl: string): boolean {
  try {
    const host = new URL(baseUrl).hostname.toLowerCase();
    return host === "127.0.0.1" || host === "localhost" || host === "::1" || host === "[::1]";
  } catch {
    return false;
  }
}

export async function loadConfiguredKeys(): Promise<Set<string> | null> {
  const res = await quiet("keys:list");
  if (!res) return null;
  return new Set(res.status.filter((s) => s.saved || s.fromEnv).map((s) => s.env));
}

export function jumpLinkHtml(label: string, tab: string, target?: string, asCode = false): string {
  const inner = asCode ? `<code>${esc(label)}</code>` : esc(label);
  return `<button type="button" class="reset settings-jump" data-jump-tab="${esc(tab)}"${target ? ` data-jump-target="${esc(target)}"` : ""}>${inner}</button>`;
}

export function keyRequirementHtml(env: string, configuredKeys?: ReadonlySet<string> | null, suffix = ""): string {
  if (!env) return "";
  const keyJump = jumpLinkHtml(env, "keys", `key:${env}`, true);
  const tabJump = jumpLinkHtml("Settings > API keys", "keys", `key:${env}`);
  if (configuredKeys?.has(env)) {
    return `<span class="help">Uses ${keyJump} in ${tabJump}${suffix}.</span>`;
  }
  return `<span class="help key-missing">Needs ${keyJump} in ${tabJump}${suffix}.</span>`;
}

function getEndpoints(config: Obj): EndpointRef[] {
  const eps = (config?.companion as Obj | undefined)?.llm as Obj | undefined;
  if (!Array.isArray(eps?.endpoints)) return [];
  const out: EndpointRef[] = [];
  for (const raw of eps.endpoints) {
    if (!raw || typeof raw !== "object") continue;
    const id = String((raw as Obj).id || "").trim().toLowerCase();
    const base_url = String((raw as Obj).base_url || "").trim().replace(/\/+$/, "");
    if (ENDPOINT_ID_RE.test(id) && /^https?:\/\//i.test(base_url)) {
      out.push({
        id,
        label: String((raw as Obj).label || id),
        base_url,
        key_env: endpointKeyEnv({ id, base_url, key_env: typeof (raw as Obj).key_env === "string" ? (raw as Obj).key_env as string : undefined }),
      });
    }
  }
  return out;
}

export function modelRequirementHtml(
  modelId: string,
  config: Obj,
  configuredKeys?: ReadonlySet<string> | null,
  catalogKeys?: Obj | null,
): string {
  const id = String(modelId || "").trim();
  if (!id) return "";
  if (id.startsWith("ep:")) {
    const eid = id.split(":")[1] || "";
    const ep = getEndpoints(config).find((x) => x.id === eid);
    if (!eid || !ep) {
      return `<span class="key-missing">Endpoint <code>${esc(eid ? `ep:${eid}` : "ep:…")}</code> is not configured — add it in ${jumpLinkHtml("Settings > AI models > Custom endpoints", "models", "Custom endpoints")}.</span>`;
    }
    if (!isLocalBaseUrl(ep.base_url)) {
      const env = endpointKeyEnv(ep);
      if (configuredKeys && !configuredKeys.has(env)) {
        return `<span class="key-missing">Needs ${jumpLinkHtml(env, "keys", `key:${env}`, true)} in ${jumpLinkHtml("Settings > API keys", "keys", `key:${env}`)} (endpoint ${jumpLinkHtml(`ep:${eid}`, "models", "Custom endpoints")}).</span>`;
      }
    }
    return "";
  }
  const [prov, env]: [string, string] = id.startsWith("gemini:")
    ? ["gemini", "GEMINI_API_KEY"]
    : id.includes("/")
      ? ["openrouter", "OPENROUTER_API_KEY"]
      : ["anthropic", "ANTHROPIC_API_KEY"];
  const hasKey = configuredKeys ? configuredKeys.has(env)
    : catalogKeys && prov in catalogKeys ? Boolean(catalogKeys[prov]) : true;
  if (hasKey) return "";
  const keyJump = jumpLinkHtml(env, "keys", `key:${env}`, true);
  const tabJump = jumpLinkHtml("Settings > API keys", "keys", `key:${env}`);
  return `<span class="key-missing">Needs ${keyJump} in ${tabJump}.</span>`;
}

export function formatCatalogIssuesHtml(
  st: Obj,
  config: Obj,
  configuredKeys?: ReadonlySet<string> | null,
): string {
  const parts: string[] = [];
  const errors: Obj = st.errors || {};
  const endpoints = getEndpoints(config);
  for (const [k, raw] of Object.entries(errors)) {
    const msg = String(raw || "");
    if (k === "anthropic" && msg.includes("ANTHROPIC_API_KEY")) {
      const rest = msg.replace("no ANTHROPIC_API_KEY", "").trim();
      parts.push(`anthropic: no ${jumpLinkHtml("ANTHROPIC_API_KEY", "keys", "key:ANTHROPIC_API_KEY", true)}${rest ? ` ${esc(rest)}` : ""}`);
    } else if (k === "gemini" && msg.includes("GEMINI_API_KEY")) {
      const rest = msg.replace("no GEMINI_API_KEY", "").trim();
      parts.push(`gemini: no ${jumpLinkHtml("GEMINI_API_KEY", "keys", "key:GEMINI_API_KEY", true)}${rest ? ` ${esc(rest)}` : ""}`);
    } else if (k.startsWith("ep:")) {
      const eid = k.slice(3);
      const ep = endpoints.find((x) => x.id === eid);
      const epJump = jumpLinkHtml(k, "models", "Custom endpoints", true);
      const env = ep && !isLocalBaseUrl(ep.base_url) ? endpointKeyEnv(ep) : "";
      const missingEpKey = env && configuredKeys && !configuredKeys.has(env);
      parts.push(`${epJump}: ${esc(msg)}${missingEpKey ? ` (set ${jumpLinkHtml(env, "keys", `key:${env}`, true)})` : ""}`);
    } else {
      parts.push(`${esc(k)}: ${esc(msg)}`);
    }
  }
  const hasOpenRouter = configuredKeys ? configuredKeys.has("OPENROUTER_API_KEY")
    : st.keys && "openrouter" in st.keys ? Boolean(st.keys.openrouter) : true;
  if (!hasOpenRouter && !("openrouter" in errors)) {
    parts.push(`openrouter: no ${jumpLinkHtml("OPENROUTER_API_KEY", "keys", "key:OPENROUTER_API_KEY", true)} (public list shown)`);
  }
  return parts.join("; ");
}
