// A read-only view of the running coach's projection. Settings drafts never enter this view.
import type { CharacterMemoryStatus, CoachStats } from "../shared/engine.js";
import { $ } from "./dom.js";
import { show } from "./nav.js";
import { openSettingsTab } from "./settings/page.js";

type Mark = "enabled" | "disabled" | "waiting" | "warning" | "partial" | "unknown";
const MARKS: Record<Mark, string> = { enabled: "✓", disabled: "−", waiting: "…", warning: "!", partial: "◐", unknown: "?" };
const RECALL: Record<CharacterMemoryStatus["recall"]["state"], [string, Mark]> = {
  waiting_character: ["Waiting for Character", "waiting"],
  words: ["Word-based recall", "enabled"],
  unavailable: ["Unavailable — word-based fallback", "warning"],
  unchecked: ["Waiting for recall", "waiting"],
  preparing: ["Preparing", "waiting"],
  partial: ["Partially prepared — word-based matching remains available", "partial"],
  ready: ["Meaning-based recall available", "enabled"],
  empty: ["No memories to prepare", "waiting"],
  error: ["Warning — word-based fallback", "warning"],
};
const UNKNOWN = "Unknown — waiting for engine state.";

function feature(id: string, name: string, text: string, mark: Mark): void {
  const el = $(id);
  el.dataset.state = mark;
  el.title = `${name}: ${text}`;
  el.setAttribute("aria-label", el.title);
  el.querySelector(".sb-feature-mark")!.textContent = MARKS[mark];
}

/** Updates icons, tooltip and the open native popover together, including on disconnect. */
export function showCharacterMemory(coach: Partial<CoachStats> | null): void {
  const value = coach?.character_memory;
  const mode = coach?.mode || "";
  const ai = mode.startsWith("offline:") ? `Paused: ${mode.slice(8).trim()}`
    : mode === "offline" ? "Offline, rules only" : mode ? "AI live" : UNKNOWN;
  let character = UNKNOWN, learning = UNKNOWN, recall = UNKNOWN, source = "";
  let characterMark: Mark = "unknown", learningMark: Mark = "unknown", recallMark: Mark = "unknown";
  if (value) {
    character = value.character.enabled ? `Enabled${value.character.name ? ` — ${value.character.name}` : ""}` : "Disabled — no active Character.";
    characterMark = value.character.enabled ? "enabled" : "disabled";
    learning = !value.learning.enabled ? "Disabled — this does not disable stored notes or explicit memory writes."
      : value.learning.available ? "Enabled — automatic learning permitted."
      : "Paused / unavailable — automatic learning enabled.";
    if (value.learning.reason) learning += ` ${value.learning.reason}`;
    learningMark = !value.learning.enabled ? "disabled" : value.learning.available ? "enabled" : "waiting";
    const observed = RECALL[value.recall.state];
    [recall, recallMark] = observed || [UNKNOWN, "unknown"];
    if (!recall.endsWith(".")) recall += ".";
    recall += `\nSelected method: ${value.recall.method === "meaning" ? "Meaning-based" : "Word-based"}.`;
    if (value.recall.detail) recall += `\n${value.recall.detail}`;
    if (value.recall.method === "meaning") source = `${value.recall.source || "Not configured"} / ${value.recall.model || "Not resolved"}`;
  }
  feature("sb-character", "Character", character, characterMark);
  feature("sb-learning", "Memory Learning", learning, learningMark);
  feature("sb-recall", "Memory Recall", recall, recallMark);
  $("sb-recall").querySelector("use")!.setAttribute("href", value?.recall.method === "meaning" ? "#i-sparkles" : "#i-search");
  $("cm-ai").textContent = ai;
  $("cm-character").textContent = character;
  $("cm-learning").textContent = value?.learning.enabled && value.learning.available ? "Enabled" : learning;
  $("cm-recall").textContent = recall;
  $("cm-source").textContent = source;
  $("cm-source-row").hidden = !source;
  const summary = `Coach: ${ai}\nCharacter: ${character}\nMemory Learning: ${learning}\nMemory Recall: ${recall}${source ? `\nRecall source / model: ${source}` : ""}`;
  $("sb-mode-wrap").title = `${summary}\nOpen Character and Memory details.`;
  $("sb-mode-wrap").setAttribute("aria-label", `${summary}. Open Character and Memory details.`);
}

export function wireCharacterMemory(): void {
  const popup = $("character-memory-details");
  // Page shortcuts must not consume the native popover's Escape close request.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && popup.matches(":popover-open")) event.stopPropagation();
  }, true);
  const openPage = (page: string) => {
    show(page);
    const heading = document.querySelector<HTMLElement>(`#page-${page} h2, #page-${page} h1`);
    if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
  };
  const links: [string, () => void][] = [
    ["character-memory-status", () => openPage("status")],
    ["character-memory-roleplay", () => openSettingsTab("coach", "companion.llm.persona")],
    ["character-memory-memory", () => openPage("memory")],
    ["character-memory-recall", () => openSettingsTab("coach", "companion.llm.recall_meaning")],
    ["character-memory-source", () => openSettingsTab("coach", "companion.llm.recall_embed_model")],
  ];
  for (const [id, navigate] of links) $(id).onclick = (event) => {
    event.preventDefault();
    popup.hidePopover();
    navigate();
  };
  showCharacterMemory(null);
}
