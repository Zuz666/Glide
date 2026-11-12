// Settings > Credentials: 4 separate .sgroup panel cards (AI models, TTS voice, Push-to-talk STT, Web search)
// with compact status rows and one shared inline credential editor across all panels.
import { call } from "../bridge.js";
import { esc, icon, toast } from "../dom.js";
import { Group } from "../settings-schema.js";
import { KeyGuide, KeyStatus, KnownKey } from "../types.js";

const PLAN: Record<string, [string, string]> = {
  free: ["Free", "free"],
  "free-tier": ["Free tier", "free"],
  credit: ["Free credit", "free"],
  paid: ["Paid", "paid"],
};

const TESTABLE_MODEL_ENVS = new Set([
  "ANTHROPIC_API_KEY",
  "GEMINI_API_KEY",
  "OPENAI_API_KEY",
  "OPENROUTER_API_KEY",
]);

type KeysPayload = NonNullable<Awaited<ReturnType<typeof call<"keys:list">>>>;

let activeEnv: string | null = null;
let editorSession = 0;
let refreshSeq = 0;
let inflightList: Promise<KeysPayload | undefined> | null = null;
const drafts = new Map<string, string>();
const mountedPanels = new Map<HTMLElement, Group>();

function fetchKeysList(): Promise<KeysPayload | undefined> {
  if (!inflightList) {
    inflightList = call("keys:list").finally(() => { inflightList = null; });
  }
  return inflightList;
}

function pruneDisconnected(): void {
  for (const host of [...mountedPanels.keys()]) {
    if (!host.isConnected) mountedPanels.delete(host);
  }
}

async function refreshAllPanels(): Promise<void> {
  pruneDisconnected();
  const entries = [...mountedPanels.entries()];
  if (!entries.length) return;
  const seq = ++refreshSeq;
  const data = await fetchKeysList();
  if (!data || seq !== refreshSeq) return;
  for (const [host, g] of entries) {
    if (host.isConnected) drawPanel(g, host, data);
  }
}

/** Pre-select a credential row so its single inline editor opens ready to focus (used by deep-links). */
export function activateCredentialRow(env: string | null): void {
  if (activeEnv !== env) {
    activeEnv = env;
    ++editorSession;
  }
  void refreshAllPanels();
}

function keyGuide(env: string, g: KeyGuide): string {
  const links = (g.links || []).map((l) => `<button type="button" class="btn ghost" data-open="${esc(l.url)}">${esc(l.label)}</button>`).join("");
  return `<details class="keyhow" open><summary>How to get this credential</summary>
    ${g.free ? `<p class="keyfree"><b>${g.plan === "paid" ? "Cost & usage" : "Free or cheapest way"}:</b> ${esc(g.free)}</p>` : ""}
    <ol>${g.steps.map((s) => `<li>${esc(s)}</li>`).join("")}<li>Paste it above and click Save, then restart the coach.</li></ol>
    ${g.looks ? `<p>Format: <code>${esc(g.looks)}</code></p>` : ""}${g.cost ? `<p>${esc(g.cost)}</p>` : ""}
    <div class="row"><button type="button" class="btn primary" data-open="${esc(g.url)}" aria-label="Open the ${esc(env)} key page in your browser">${icon("external-link")}Open provider key page</button>${links}</div>
  </details>`;
}

function statusPillHtml(s: KeyStatus): string {
  if (s.saved) {
    return `<span class="key-pill saved">Saved · …${esc(s.hint.replace("…", ""))}</span>`;
  }
  if (s.fromEnv) {
    return `<span class="key-pill env" title="Set as a Windows environment variable">Windows env · …${esc(s.hint.replace("…", ""))}</span>`;
  }
  return `<span class="key-pill none">Not set</span>`;
}

function activeEditorHtml(s: KeyStatus, k: KnownKey | undefined, draft: string): string {
  const testable = TESTABLE_MODEL_ENVS.has(s.env);
  return `<div class="key-editor">
    <div class="key-editor-bar">
      <input type="password" data-env="${esc(s.env)}" value="${esc(draft)}" placeholder="${s.saved || s.fromEnv ? `Paste new ${esc(s.env)} to replace…` : `Paste ${esc(s.env)}…`}" autocomplete="off" aria-label="${esc(s.env)}">
      <button type="button" class="btn primary" data-save="${esc(s.env)}">Save</button>
      ${testable ? `<button type="button" class="btn" data-test="${esc(s.env)}">Test</button>` : ""}
      ${s.saved ? `<button type="button" class="btn ghost danger" data-del="${esc(s.env)}">Remove</button>` : ""}
      <button type="button" class="btn ghost" data-close-editor>Close</button>
    </div>
    <div class="help key-test-msg" data-test-msg hidden role="status"></div>
    ${k?.guide ? keyGuide(s.env, k.guide) : `<p class="help">Stored encrypted with your Windows account as <code>${esc(s.env)}</code> and passed to the coach on startup.</p>`}
  </div>`;
}

function keyRowHtml(s: KeyStatus, k: KnownKey | undefined, isOpen: boolean): string {
  const plan = k?.guide?.plan ? PLAN[k.guide.plan] : null;
  const scopes = (k?.scopes || []).map((sc) => `<span class="key-scope">${esc(sc)}</span>`).join("");
  const actionLabel = isOpen ? "Editing" : s.saved || s.fromEnv ? "Replace" : "Set";
  return `<div class="keyrow${isOpen ? " open" : ""}" data-key-row="${esc(s.env)}">
    <div class="keyrow-head" data-toggle-env="${esc(s.env)}">
      <div class="keyrow-meta">
        <div class="keyrow-title">
          <b>${esc(k?.label || s.env)}</b>
          ${plan ? `<span class="plan ${plan[1]}">${plan[0]}</span>` : ""}
          ${scopes}
          <code class="key-env">${esc(s.env)}</code>
        </div>
        <span class="k">${esc(k?.help || "Credential for a custom OpenAI-compatible endpoint.")}</span>
      </div>
      <div class="keyrow-side">
        ${statusPillHtml(s)}
        <button type="button" class="btn${isOpen ? " ghost" : ""}" data-edit-env="${esc(s.env)}" aria-expanded="${isOpen}">${actionLabel}</button>
      </div>
    </div>
    ${isOpen ? activeEditorHtml(s, k, drafts.get(s.env) || "") : ""}
  </div>`;
}

function drawPanel(g: Group, host: HTMLElement, r: KeysPayload): void {
  const targetKind: KnownKey["kind"] = g.keyKind || "model";
  const known = (env: string) => r.known.find((k) => k.env === env);
  const kindOf = (env: string): KnownKey["kind"] => known(env)?.kind || "model";
  if (activeEnv && !r.status.some((s) => s.env === activeEnv)) {
    activeEnv = null;
  }

  const prevFocused = document.activeElement instanceof HTMLInputElement
    && host.contains(document.activeElement)
    && document.activeElement.dataset.env === activeEnv
    ? document.activeElement : null;
  const selStart = prevFocused?.selectionStart ?? null;
  const selEnd = prevFocused?.selectionEnd ?? null;

  const items = r.status.filter((s) => kindOf(s.env) === targetKind);
  const configuredCount = items.filter((s) => s.saved || s.fromEnv).length;
  const anyModel = r.status.some((s) => (s.saved || s.fromEnv) && kindOf(s.env) === "model");

  const sgroup = host.closest<HTMLElement>(".sgroup");
  if (sgroup) {
    sgroup.dataset.keyGroup = targetKind;
    const h2 = sgroup.querySelector<HTMLElement>(":scope > h2");
    if (h2) {
      let badge = h2.querySelector<HTMLElement>(".key-count");
      if (!badge) {
        badge = document.createElement("span");
        badge.className = "key-count";
        h2.appendChild(badge);
      }
      badge.textContent = `${configuredCount}/${items.length} configured`;
    }
  } else {
    host.dataset.keyGroup = targetKind;
  }

  const needBanner = targetKind === "model" && !anyModel
    ? `<div class="keyintro need"><b>Add one model credential to turn the AI coach on</b><p>Quickest free start: <b>Google Gemini</b>. All-in-one (models, voice, STT, memory): <b>OpenAI</b>. Best coaching: <b>Anthropic (Claude)</b>.</p></div>`
    : "";

  host.innerHTML = needBanner + items.map((s) => keyRowHtml(s, known(s.env), activeEnv === s.env)).join("");

  const closeEditor = () => {
    activeEnv = null;
    ++editorSession;
    void refreshAllPanels();
  };

  const toggle = (env: string) => {
    activeEnv = activeEnv === env ? null : env;
    ++editorSession;
    const session = editorSession;
    void refreshAllPanels().then(() => {
      if (editorSession === session && activeEnv === env) {
        document.querySelector<HTMLInputElement>(`#set-body input[data-env="${CSS.escape(env)}"]`)?.focus({ preventScroll: true });
      }
    });
  };

  host.querySelectorAll<HTMLButtonElement>("[data-edit-env]").forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      toggle(btn.dataset.editEnv!);
    };
  });
  host.querySelectorAll<HTMLElement>("[data-toggle-env]").forEach((head) => {
    head.onclick = () => toggle(head.dataset.toggleEnv!);
  });
  host.querySelector<HTMLButtonElement>("[data-close-editor]")?.addEventListener("click", closeEditor);
  host.querySelectorAll<HTMLButtonElement>("[data-open]").forEach((b) => {
    b.onclick = () => call("shell:open", b.dataset.open!);
  });

  const activeInput = host.querySelector<HTMLInputElement>(".key-editor input[data-env]");
  const saveBtn = host.querySelector<HTMLButtonElement>(".key-editor [data-save]");
  if (activeInput && saveBtn) {
    const env = saveBtn.dataset.save!;
    const session = editorSession;
    activeInput.addEventListener("input", () => {
      drafts.set(env, activeInput.value);
    });
    if (prevFocused && activeEnv === env) {
      activeInput.focus({ preventScroll: true });
      if (selStart !== null) activeInput.setSelectionRange(selStart, selEnd);
    }
    const doSave = async () => {
      const rawSubmitted = activeInput.value;
      if (!rawSubmitted.trim()) return toast("Paste the credential first", true);
      saveBtn.disabled = true;
      const ok = await call("keys:set", env, rawSubmitted);
      saveBtn.disabled = false;
      if (ok) {
        const unchanged = (drafts.get(env) ?? rawSubmitted) === rawSubmitted;
        if (unchanged) drafts.delete(env);
        toast(`${env} saved. Restart the coach to apply engine credentials.`);
        if (unchanged && editorSession === session && activeEnv === env) {
          activeEnv = null;
          ++editorSession;
        }
        void refreshAllPanels();
      }
    };
    saveBtn.onclick = doSave;
    activeInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); void doSave(); }
      else if (ev.key === "Escape") { ev.preventDefault(); closeEditor(); }
    });
  }

  host.querySelector<HTMLButtonElement>(".key-editor [data-test]")?.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget as HTMLButtonElement;
    const env = btn.dataset.test!;
    const session = editorSession;
    const msg = host.querySelector<HTMLElement>("[data-test-msg]");
    const val = activeInput?.value.trim() || undefined;
    if (msg) { msg.hidden = false; msg.textContent = "Testing…"; msg.className = "help key-test-msg"; }
    btn.disabled = true;
    const res = await call("keys:test", env, val);
    if (editorSession !== session || !msg?.isConnected) return;
    btn.disabled = false;
    if (!res) return;
    msg.textContent = res.ok ? `Valid — ${res.models?.length ?? 0} models reachable.` : `Failed: ${res.error || "rejected"}`;
    msg.className = `help key-test-msg ${res.ok ? "ok" : "bad"}`;
  });

  host.querySelector<HTMLButtonElement>(".key-editor [data-del]")?.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget as HTMLButtonElement;
    const env = btn.dataset.del!;
    const session = editorSession;
    const rawBefore = activeInput?.value ?? "";
    btn.disabled = true;
    const ok = await call("keys:set", env, "");
    btn.disabled = false;
    if (ok) {
      const unchanged = (drafts.get(env) ?? rawBefore) === rawBefore;
      if (unchanged) drafts.delete(env);
      toast(`${env} removed`);
      if (unchanged && editorSession === session && activeEnv === env) {
        activeEnv = null;
        ++editorSession;
      }
      void refreshAllPanels();
    }
  });
}

export async function renderKeys(g: Group, host: HTMLElement): Promise<void> {
  pruneDisconnected();
  mountedPanels.set(host, g);
  const seq = refreshSeq;
  const r = await fetchKeysList();
  if (!r || !host.isConnected || seq !== refreshSeq) return;
  drawPanel(g, host, r);
}
