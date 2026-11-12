// API keys. They are kept encrypted with the Windows user's credentials (Electron safeStorage, DPAPI) in
// userData/secrets.json, never in config.json, and handed to the engine as environment variables when it starts.
// Keys already set in the environment (setx ANTHROPIC_API_KEY ...) still work; a key saved here wins.
// safeStorage's asynchronous API (Electron 46 drops the synchronous one): the store is decrypted once at start
// (loadSecrets) into memory, which the engine's environment and the settings page read; setKey saves through it.
import { safeStorage } from "electron";
import type { KeyStatus, KnownKey } from "../shared/ipc";
import { secretsPath } from "./paths";
import { keepBadCopy, readJsonFile, unreadableMessage, writeJson } from "./store";

export const KNOWN_KEYS: KnownKey[] = [
  { env: "ANTHROPIC_API_KEY", label: "Anthropic (Claude)", kind: "model", help: "Claude models, and the model list. The best coaching.",
    guide: { url: "https://platform.claude.com/settings/keys",
      steps: ["Sign in or create an account at the Claude Console (a Claude.ai subscription doesn't include API use).",
              "Add credit under Settings > Billing: the API is prepaid, and a key does nothing until there is credit.",
              "Under Settings > API keys, click Create key, give it a name (e.g. Glide) and copy it. It's shown only once."],
      looks: "sk-ant-api03-…",
      plan: "paid",
      free: "No free tier. Cheapest: Claude Haiku for callouts (a fraction of a cent each) with Sonnet for reviews; " +
            "$5 of credit goes a long way. Set a monthly spend limit under Settings > Limits.",
      links: [{ label: "Billing", url: "https://platform.claude.com/settings/billing" },
              { label: "Prices", url: "https://www.anthropic.com/pricing#api" }] } },
  { env: "GEMINI_API_KEY", label: "Google Gemini", kind: "model", help: "Models shown as gemini:<id>. The fastest way to start: free tier.",
    guide: { url: "https://aistudio.google.com/app/apikey",
      steps: ["Sign in to Google AI Studio with a Google account.",
              "Click Create API key (pick or create a Google Cloud project when asked) and copy it."],
      looks: "AIza…",
      plan: "free-tier",
      free: "Free tier, no card: the Flash models, with daily request limits (see yours in AI Studio). Enough for casual " +
            "play; Google may use free-tier prompts to improve its products. Turn on billing for higher limits.",
      links: [{ label: "Prices and limits", url: "https://ai.google.dev/gemini-api/docs/pricing" }] } },
  { env: "OPENROUTER_API_KEY", label: "OpenRouter", kind: "model", help: "Any vendor/model id, e.g. google/gemini-2.5-flash. One key for hundreds of models.",
    guide: { url: "https://openrouter.ai/settings/keys",
      steps: ["Sign in at openrouter.ai.",
              "Buy credit under Settings > Credits (some models marked :free work without it, with tight limits).",
              "Under Settings > Keys, click Create Key, optionally set a credit limit for it, and copy it."],
      looks: "sk-or-v1-…",
      plan: "free-tier",
      free: "Models whose id ends in :free cost nothing: 50 requests a day (1,000 a day once you've bought $10 of credit), " +
            "20 a minute. A long session can pass 50 callouts, and free models can be slow or disappear. Paid models " +
            "cost their usual price plus a small fee on credit purchases.",
      links: [{ label: "Credits", url: "https://openrouter.ai/settings/credits" },
              { label: "Models and prices", url: "https://openrouter.ai/models" }] } },
  { env: "OPENAI_API_KEY", label: "OpenAI", kind: "model", scopes: ["Models", "Voice", "STT", "Memory"],
    help: "AI coach models (ep:openai:<id>), coach voice (gpt-4o-mini-tts), push-to-talk recognition and memory search.",
    guide: { url: "https://platform.openai.com/api-keys",
      steps: ["Sign in at platform.openai.com (a ChatGPT subscription doesn't include API use).",
              "Add credit under Settings > Billing.",
              "Under API keys, click Create new secret key and copy it. It's shown only once."],
      looks: "sk-proj-…",
      plan: "paid",
      free: "No free tier. One key covers OpenAI chat and reasoning models (add the OpenAI preset under AI models > Custom endpoints), " +
            "coach voice (gpt-4o-mini-tts), push-to-talk transcription and memory embeddings; $5 of credit lasts many hours of play.",
      links: [{ label: "Billing", url: "https://platform.openai.com/settings/organization/billing/overview" }] } },
  { env: "TAVILY_API_KEY", label: "Tavily", kind: "search", help: "Web search for Gemini, OpenRouter and custom-endpoint models (preferred). Claude models search without it.",
    guide: { url: "https://app.tavily.com/home",
      steps: ["Sign up at app.tavily.com.", "Your key is on the dashboard (Overview > API Keys): copy it."],
      looks: "tvly-…",
      plan: "free",
      free: "1,000 free searches a month, no card needed: far more than the coach uses." } },
  { env: "BRAVE_API_KEY", label: "Brave Search", kind: "search", help: "Alternative web search key for non-Claude models (Tavily is simpler).",
    guide: { url: "https://api-dashboard.search.brave.com/app/keys",
      steps: ["Register at the Brave Search API dashboard and confirm your email.",
              "Subscribe to the Search plan (a card is required, used to verify you).",
              "Under API Keys, click Add API key and copy it."],
      plan: "credit",
      free: "$5 of free usage each month (about 1,000 searches), then paid per 1,000. Needs a card on file; Tavily doesn't." } },
  { env: "ELEVENLABS_API_KEY", label: "ElevenLabs", kind: "voice", help: "Cloud coach voice: expressive, natural-sounding speech.",
    guide: { url: "https://elevenlabs.io/app/settings/api-keys",
      steps: ["Sign up at elevenlabs.io.", "Open your profile > API keys, click Create API key and copy it."],
      looks: "sk_…",
      plan: "free-tier",
      free: "Free plan: 10,000 credits a month, about 10 minutes of speech, for personal use. Starter is about $5 a month " +
            "for three times that." } },
  { env: "AZURE_SPEECH_KEY", label: "Azure Speech", kind: "voice", help: "Cloud neural coach voices; set your resource region under Voice.",
    guide: { url: "https://portal.azure.com/#create/Microsoft.CognitiveServicesSpeechServices",
      steps: ["Sign in to the Azure portal (a free account works).",
              "Create a Speech service: pick a region (e.g. East US) and the Free F0 pricing tier.",
              "Open the resource > Keys and Endpoint, copy Key 1, and enter its region (e.g. eastus) under Voice."],
      plan: "free-tier",
      free: "The Free F0 tier gives 500,000 characters of neural voice a month, many hours of coaching, at no cost. " +
            "Azure asks for a card when you sign up but doesn't charge the free tier." } },
  { env: "GOOGLE_TTS_API_KEY", label: "Google Cloud TTS", kind: "voice", help: "Chirp 3 HD, Neural2 and WaveNet cloud coach voices.",
    guide: { url: "https://console.cloud.google.com/apis/credentials",
      steps: ["In the Google Cloud console, pick or create a project and link a billing account.",
              "Enable the Cloud Text-to-Speech API (link below).",
              "Under APIs & Services > Credentials, click Create credentials > API key, copy it, and restrict it to the Text-to-Speech API."],
      looks: "AIza…",
      plan: "free-tier",
      free: "Free every month: 4 million characters of Standard and WaveNet voices, 1 million of Neural2 and Chirp HD. " +
            "Needs a billing account, but the coach stays well inside the free amount.",
      links: [{ label: "Enable Text-to-Speech", url: "https://console.cloud.google.com/apis/library/texttospeech.googleapis.com" }] } },
  { env: "GROQ_API_KEY", label: "Groq", kind: "speech", scopes: ["STT", "Endpoint"],
    help: "Fast online push-to-talk transcription (Whisper large v3 turbo); also usable for a Groq model endpoint.",
    guide: { url: "https://console.groq.com/keys",
      steps: ["Sign in at console.groq.com.", "Click Create API Key and copy it. It's shown only once."],
      looks: "gsk_…",
      plan: "free-tier",
      free: "Free tier with rate limits and no card: plenty for push-to-talk." } },
  { env: "DEEPGRAM_API_KEY", label: "Deepgram", kind: "speech", help: "Online push-to-talk transcription (nova-3).",
    guide: { url: "https://console.deepgram.com/",
      steps: ["Sign up at console.deepgram.com.", "In your project, open Settings > API Keys, click Create a New API Key and copy it."],
      plan: "credit",
      free: "$200 of free credit for new accounts, no card needed to start: years of push-to-talk." } },
];

// A saved key's name: an API key or token variable (..._KEY / ..._TOKEN), never one that changes how programs run
// (PATH, PYTHON*, PIP_*, proxies, certificate bundles: the saved keys go into every engine and pip process)
const ENV_RE = /^[A-Z][A-Z0-9_]{0,58}_(KEY|TOKEN)$/;
const ENV_DENY = /^(PATH|PATHEXT|COMSPEC|PYTHON|PIP_|NODE_|NPM_|ELECTRON_|SSL_|REQUESTS_|CURL_|HTTPS?_PROXY|ALL_PROXY|NO_PROXY)/;
const keyName = (k: string) => ENV_RE.test(k) && !ENV_DENY.test(k);

/** Why secrets.json can't be used, or null: there but unreadable (a copy is kept). Saving would replace every key
 * in it with the one being set, so setKey refuses until it is fixed or deleted. */
export function secretsProblem(): string | null {
  const p = secretsPath();
  const file = readJsonFile(p);
  return file.bad === undefined ? null
    : unreadableMessage(p, file.bad, keepBadCopy(p), "the saved API keys can't be used, changes to them aren't saved")
      + " (then enter the keys again if it can't be fixed).";
}

/** The decrypted keys, as loadSecrets or the last setKey left them. */
let keys: Record<string, string> = {};
/** loadSecrets and setKey run one at a time: two saves at once would each write the file without the other's key. */
let queue: Promise<unknown> = Promise.resolve();
function queued<T>(job: () => Promise<T>): Promise<T> {
  const run = queue.then(job);
  queue = run.catch(() => undefined);
  return run;
}

/** secrets.json decrypted; rotate: Windows asks for a key to be encrypted again (its key changed). */
async function decryptFile(): Promise<{ keys: Record<string, string>; rotate: boolean }> {
  const raw = readJsonFile(secretsPath()).value;
  const out: Record<string, string> = {};
  let rotate = false;
  if (!(await safeStorage.isAsyncEncryptionAvailable())) return { keys: out, rotate };
  for (const [k, v] of Object.entries(raw)) {
    if (!keyName(k) || typeof v !== "string") continue;
    try {
      const r = await safeStorage.decryptStringAsync(Buffer.from(v, "base64"));
      out[k] = r.result;
      rotate ||= r.shouldReEncrypt;
    } catch {
      /* written by another Windows user: ignore */
    }
  }
  return { keys: out, rotate };
}

async function save(next: Record<string, string>): Promise<void> {
  const raw: Record<string, string> = {};
  for (const [k, v] of Object.entries(next)) if (v) raw[k] = (await safeStorage.encryptStringAsync(v)).toString("base64");
  writeJson(secretsPath(), raw);
}

/** Reads the key store into memory: once at start, before the engine starts (and after secrets.json changed outside
 * the app). Keys Windows wants encrypted again are saved again. */
export function loadSecrets(): Promise<void> {
  return queued(async () => {
    const file = await decryptFile();
    keys = file.keys;
    if (file.rotate && !secretsProblem()) await save(keys);
  });
}

/** The environment variables to add to the engine's environment. */
export function secretEnv(): Record<string, string> {
  return { ...keys };
}

/** One key as the settings page sees it: saved here, or set in the environment, and its last four characters. */

/** For the settings page: which keys are set, and where from. Values are never sent to the renderer. */
export function keyStatus(extraEnvs: string[]): KeyStatus[] {
  const saved = keys;
  const envs = [...new Set([...KNOWN_KEYS.map((k) => k.env), ...extraEnvs, ...Object.keys(saved)])];
  return envs.map((env) => {
    const v = saved[env] || process.env[env] || "";
    return { env, saved: !!saved[env], fromEnv: !saved[env] && !!process.env[env], hint: v ? `…${v.slice(-4)}` : "" };
  });
}

/** Set (non-empty value) or remove (empty value) one key. It starts from secrets.json as it is on disk, so a key
 * saved meanwhile by another copy of the app isn't lost. */
export function setKey(env: string, value: string): Promise<void> {
  return queued(async () => {
    if (!keyName(env)) throw new Error(`not a key's variable name (it must end in _KEY or _TOKEN): ${env}`);
    if (!(await safeStorage.isAsyncEncryptionAvailable())) throw new Error("Windows can't encrypt the key store for this user");
    const problem = secretsProblem();
    if (problem) throw new Error(problem);
    const next = (await decryptFile()).keys;
    if (value.trim()) next[env] = value.trim();
    else delete next[env];
    await save(next);
    keys = next;
  });
}
