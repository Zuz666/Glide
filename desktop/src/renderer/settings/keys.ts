// Settings > API keys: each known key (and each custom endpoint's), whether it's set, a box to
// paste a new one, and how to get it. Values go to the main process and never come back.
import { call } from "../bridge.js";
import { esc, toast } from "../dom.js";
import { Group } from "../settings-schema.js";
import { KeyGuide, KeyStatus, KnownKey } from "../types.js";

const PLAN: Record<string, [string, string]> = { free: ["Free", "free"], "free-tier": ["Free tier", "free"],
  credit: ["Free credit", "free"], paid: ["Paid", "paid"] };

const INTRO = (anyModel: boolean) => `<div class="keyintro${anyModel ? "" : " need"}"><b>${anyModel ? "Which keys do I need?" : "Add one model key to turn the coach on"}</b>
    <p>One model key is enough. Quickest: <b>Google Gemini</b>, free and about a minute to set up. Best coaching: <b>Anthropic (Claude)</b>,
    prepaid credit. <b>OpenRouter</b> is one key for models from every vendor. Web search keys only matter for non-Claude models, and voice keys are
    optional: Windows' own voices work without one.</p>
    <p><b>Completely free setup:</b> Google Gemini for the coach, Tavily for web search, Windows voices (or Azure's free tier) to speak, and Groq or
    offline recognition for push-to-talk. Open "How to get this key" under each for the steps.</p></div>`;

/** "How to get this key": the page that makes keys (opens in the browser), the steps, what a key looks like, cost. */
function keyGuide(env: string, g: KeyGuide): string {
  const links = (g.links || []).map((l) => `<button type="button" class="btn ghost" data-open="${esc(l.url)}">${esc(l.label)}</button>`).join("");
  return `<details class="keyhow"><summary>How to get this key</summary>
    ${g.free ? `<p class="keyfree"><b>${g.plan === "paid" ? "Cheapest way" : "Free or cheapest way"}:</b> ${esc(g.free)}</p>` : ""}
    <ol>${g.steps.map((s) => `<li>${esc(s)}</li>`).join("")}<li>Paste it above and click Save key, then restart the coach.</li></ol>
    ${g.looks ? `<p>A key looks like <code>${esc(g.looks)}</code>.</p>` : ""}${g.cost ? `<p>${esc(g.cost)}</p>` : ""}
    <div class="row"><button type="button" class="btn primary" data-open="${esc(g.url)}" aria-label="Open the ${esc(env)} key page in your browser">Open the key page</button>${links}</div>
  </details>`;
}

function keyRowHtml(s: KeyStatus, k: KnownKey | undefined): string {
  const st = s.saved ? `<div class="keystate saved">Saved, ending ${esc(s.hint.replace("…", ""))}</div>` : s.fromEnv ? `<div class="keystate env" title="Set as a Windows environment variable">From a Windows environment variable, ending ${esc(s.hint.replace("…", ""))}</div>` : `<div class="keystate none">Not set</div>`;
  const plan = k?.guide?.plan ? PLAN[k.guide.plan] : null;
  return `<div class="keyrow" data-key-row="${esc(s.env)}"><div><b>${esc(k?.label || s.env)}</b>${plan ? ` <span class="plan ${plan[1]}">${plan[0]}</span>` : ""}<span class="k">${esc(k?.help || "Key for a custom endpoint.")}</span></div>
      <div><input type="password" data-env="${esc(s.env)}" placeholder="${s.saved || s.fromEnv ? "Paste a new key to replace it" : "Paste the key"}" autocomplete="off" aria-label="${esc(s.env)}">${st}</div>
      <div class="row"><button class="btn" data-save="${esc(s.env)}">Save key</button>${s.saved ? `<button class="btn ghost danger" data-del="${esc(s.env)}">Remove</button>` : ""}</div>
      ${k?.guide ? keyGuide(s.env, k.guide) : ""}</div>`;
}

export async function renderKeys(g: Group, host: HTMLElement): Promise<void> {
  const r = await call("keys:list");
  if (!r) return;
  const known = (env: string) => r.known.find((k) => k.env === env);
  const anyModel = r.status.some((s) => (s.saved || s.fromEnv) && known(s.env)?.kind === "model");
  host.innerHTML = INTRO(anyModel) + r.status.map((s) => keyRowHtml(s, known(s.env))).join("");
  host.querySelectorAll<HTMLButtonElement>("[data-open]").forEach((b) => (b.onclick = () => call("shell:open", b.dataset.open!)));
  host.querySelectorAll<HTMLButtonElement>("[data-save]").forEach((b) => (b.onclick = async () => {
    const inp = host.querySelector<HTMLInputElement>(`input[data-env="${b.dataset.save}"]`)!;
    if (!inp.value.trim()) return toast("Paste the key first", true);
    if (await call("keys:set", b.dataset.save!, inp.value)) {
      toast(`${b.dataset.save} saved. Restart the coach to use it.`);
      renderKeys(g, host);
    }
  }));
  host.querySelectorAll<HTMLButtonElement>("[data-del]").forEach((b) => (b.onclick = async () => {
    if (await call("keys:set", b.dataset.del!, "")) {
      toast(`${b.dataset.del} removed`);
      renderKeys(g, host);
    }
  }));
}
