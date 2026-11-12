// The Memory page: what the coach learned about the player (notes it keeps and uses), the characters it played for
// them (who each is, its past, who the player is to it), and the open reminders. All live in the engine (/memory,
// /reminders); the reminders also arrive with every state on the stream. The search box narrows the notes and the
// characters' lines to the ones with every word typed (memory-search.ts), to edit or forget them.
import { $, esc, icon, toast } from "./dom.js";
import { dateTimeHtml } from "./date-time.js";
import { markWords, noteMatches, searchWords } from "./memory-search.js";
import { currentPage } from "./nav.js";
import { engineGet, enginePost, live } from "./state.js";
import { Obj } from "./types.js";

const MEM_SOURCE: Record<string, [string, string]> = { player: ["user", "You told me"], coach: ["bot", "The coach noted"],
  reflection: ["lightbulb", "Learned from your sessions"], mark: ["flag", "From your feedback mark"] };
const MEM_TITLE: Record<string, string> = { preference: "How you like to be coached", request: "Standing requests",
  goal: "Your goals", feedback: "Corrections the coach won't repeat", fact: "About you",
  rapport: "What we share: jokes, nicknames, moments" };
// a character's lines, in the order shown (its scene, past and story threads follow)
const CHAR_FIELDS: [string, string][] = [["brief", "Who they are"], ["voice", "How they sound"], ["role", "Who you are to them"]];
let memory: Obj | null = null;
let editing: number | null = null;
let query = "";

interface CharRow { f: string; label: string; text: string; i?: number }
let shownRows: { name: string; r: CharRow }[] = [];     // the characters' lines on the page (data-ced etc. index them)
let charEditing: string | null = null;                   // the line being edited (rowKey)
let forgetArmed: string | null = null;                   // the character whose Forget asks to be pressed again
let busy = false;                                        // a change to the characters on its way: one at a time
// what's typed and not saved yet (a note's edit "n<id>", a character's line by rowKey, an add box "+<name>"): kept
// when the page is drawn again (the search narrowing, a change coming back)
const drafts: Record<string, string> = {};
const rowKey = (name: string, r: CharRow) => [name, r.f, r.i ?? ""].join("\u0001");

/** Read the memory (no body) or change it ({op: ...}); the new memory, or null with a toast when it failed. */
async function memoryCall(body?: Obj): Promise<Obj | null> {
  if (!live.engineUrl) return null;
  try {
    const r = await (body ? enginePost("/memory", body) : engineGet("/memory"));
    const d = await r.json();
    if (d.ok === false) throw new Error(d.error);
    return d.memory || d;
  } catch (e) {
    toast(`Memory: ${(e as Error).message}`, true);
    return null;
  }
}

export async function refreshMemory(): Promise<void> {
  memory = await memoryCall();
  renderMemory();
  renderReminders();
}

function renderMemory(): void {
  const host = $("mem-notes"), chars = $("mem-chars");
  $<HTMLInputElement>("mem-learning").disabled = !memory;
  $<HTMLButtonElement>("mem-reflect").disabled = !memory;
  shownRows = [];
  if (!live.engineUrl || !memory) {
    host.innerHTML = `<div class="mem-empty">Start the coach to see and edit what it remembers.</div>`;
    chars.innerHTML = "";
    return;
  }
  $<HTMLInputElement>("mem-learning").checked = !!memory.learning;
  const notes: Obj[] = memory.notes || [];
  // searching: the notes with every word (in the note, its kind, where it came from); the one being edited stays
  const words = searchWords(query);
  const groups = (memory.kinds as Obj[]).map((k) => {
    const title = MEM_TITLE[k.id] || k.label;
    const all = notes.filter((n) => n.kind === k.id);
    const list = all.filter((n) => editing === n.id || noteMatches(words, n.text, title, (MEM_SOURCE[n.source] || ["", n.source])[1]));
    if (!list.length) return "";
    return `<div class="mem-group"><h2>${esc(title)}<span>${words.length ? `${list.length} of ${all.length}` : list.length}</span></h2><div class="mem-list">${list.map((n) => noteHtml(n, words)).join("")}</div></div>`;
  }).join("");
  const cast: Obj[] = memory.characters || [];
  const cards = cast.map((c) => charHtml(c, words)).filter(Boolean);
  chars.innerHTML = cards.length ? `<div class="mem-group"><h2>Characters the coach has played<span>${words.length ? `${cards.length} of ${cast.length}` : cast.length}</span></h2>
    <p class="mem-intro">Who each one is, their past and who you are to them. The coach builds on what's here: change it, add to it, or forget it.</p>${cards.join("")}</div>` : "";
  if (groups) host.innerHTML = groups;
  else if (words.length) host.innerHTML = cards.length ? "" : `<div class="set-nohit">${icon("search")}<b>No memories match “${esc(query.trim())}”</b><span>Try another word, or press Esc to see them all.</span></div>`;
  else host.innerHTML = `<div class="mem-empty">Nothing yet. Tell the coach how you like to be coached, your goals, or when it gets something wrong; say "remember that…" while playing, or add a note above. ${memory.persistent ? "" : "(Simulation mode: notes here are not saved.)"}</div>`;
  bindNotes(host, notes);
  bindCharacters(chars);
}

function noteHtml(n: Obj, words: string[]): string {
  const [ic, src] = MEM_SOURCE[n.source] || ["user", n.source];
  const body = editing === n.id
    ? `<input data-edit="${n.id}" value="${esc(drafts[`n${n.id}`] ?? n.text)}" aria-label="Edit note">`
    : markWords(n.text, words, esc);
  return `<div class="note${n.pinned ? " pinned" : ""}"><div><div class="txt">${body}</div>
        <div class="meta"><span>${icon(ic)}${esc(src)}</span><span>${n.updated !== n.created ? "Updated" : "Added"} ${dateTimeHtml(n.updated * 1000, "date")}</span>${n.used ? `<span>Used ${n.used}×</span>` : ""}</div></div>
        <div class="acts">
          <button class="btn ghost icon-only${n.pinned ? " on" : ""}" data-pin="${n.id}" title="${n.pinned ? "Unpin" : "Pin: always include it"}" aria-label="${n.pinned ? "Unpin" : "Pin"}">${icon(n.pinned ? "pin-off" : "pin")}</button>
          <button class="btn ghost icon-only" data-ed="${n.id}" title="${editing === n.id ? "Save" : "Edit"}" aria-label="Edit">${icon(editing === n.id ? "check" : "pencil")}</button>
          <button class="btn ghost icon-only danger" data-del="${n.id}" title="Forget it" aria-label="Forget">${icon("trash-2")}</button>
        </div></div>`;
}

/** Pin, edit (Enter saves, Esc cancels) and forget. */
function bindNotes(host: HTMLElement, notes: Obj[]): void {
  const save = async (id: number) => {
    const inp = host.querySelector<HTMLInputElement>(`[data-edit="${id}"]`);
    editing = null;
    delete drafts[`n${id}`];
    if (inp) memory = (await memoryCall({ op: "update", id, text: inp.value })) || memory;
    renderMemory();
  };
  host.querySelectorAll<HTMLButtonElement>("[data-pin]").forEach((b) => (b.onclick = async () => {
    const n = notes.find((x) => x.id === Number(b.dataset.pin));
    memory = (await memoryCall({ op: "update", id: Number(b.dataset.pin), pinned: !n?.pinned })) || memory;
    renderMemory();
  }));
  host.querySelectorAll<HTMLButtonElement>("[data-ed]").forEach((b) => (b.onclick = () => {
    const id = Number(b.dataset.ed);
    if (editing === id) return save(id);
    editing = id;
    renderMemory();
    host.querySelector<HTMLInputElement>(`[data-edit="${id}"]`)?.focus();
  }));
  host.querySelectorAll<HTMLInputElement>("[data-edit]").forEach((inp) => {
    inp.oninput = () => { drafts[`n${inp.dataset.edit}`] = inp.value; };
    inp.onkeydown = (e) => {
      if (e.key === "Enter") save(Number(inp.dataset.edit));
      if (e.key === "Escape") { editing = null; delete drafts[`n${inp.dataset.edit}`]; renderMemory(); }
    };
  });
  host.querySelectorAll<HTMLButtonElement>("[data-del]").forEach((b) => (b.onclick = async () => {
    memory = (await memoryCall({ op: "remove", id: Number(b.dataset.del) })) || memory;
    toast("Forgotten.");
    renderMemory();
  }));
}

/** A character's lines: who it is, how it sounds, who the player is to it, its scene, its past, its threads. */
function charRows(c: Obj): CharRow[] {
  const rows: CharRow[] = CHAR_FIELDS.map(([f, label]) => ({ f, label, text: c[f] || "" }));
  if (c.scene) rows.push({ f: "scene", label: "The scene you're playing", text: c.scene });
  (c.lore || []).forEach((text: string, i: number) => rows.push({ f: "lore", label: "Their past", text, i }));
  (c.plot || []).forEach((text: string, i: number) => rows.push({ f: "plot", label: "Their own story", text, i }));
  return rows;
}

/** A character's card: its lines (searching: all of them when its name matches, else the ones that do), each to edit
 * or clear; a line to add to its past; Forget (pressed twice). "" when searching and nothing of it matches. */
function charHtml(c: Obj, words: string[]): string {
  const all = charRows(c);
  const rows = !words.length || noteMatches(words, c.name) ? all
    : all.filter((r) => rowKey(c.name, r) === charEditing || noteMatches(words, r.label, r.text));
  if (!rows.length) return "";
  const tag = c.name === memory?.playing ? "Playing now" : "";
  const armed = forgetArmed === c.name;
  let last = "";
  const body = rows.map((r) => {
    const k = shownRows.push({ name: c.name, r }) - 1;
    const ed = charEditing === rowKey(c.name, r);
    const lab = r.label !== last ? `<div class="lab">${esc(r.label)}</div>` : "";
    last = r.label;
    const txt = ed ? `<input data-cedit="${k}" value="${esc(drafts[rowKey(c.name, r)] ?? r.text)}" aria-label="${esc(r.label)}">`
      : r.text ? markWords(r.text, words, esc) : `<span class="unset">Not set yet</span>`;
    const clear = r.text && r.f !== "brief"
      ? `<button class="btn ghost icon-only danger" data-cclear="${k}" title="${r.f === "lore" || r.f === "plot" ? "Forget it" : "Clear it"}" aria-label="Clear">${icon("trash-2")}</button>`
      : `<span class="acts-gap"></span>`;          // (the Edit buttons in one column)
    return `<div class="note"><div>${lab}<div class="txt">${txt}</div></div><div class="acts"><button class="btn ghost icon-only" data-ced="${k}" title="${ed ? "Save" : "Edit"}" aria-label="Edit">${icon(ed ? "check" : "pencil")}</button>${clear}</div></div>`;
  }).join("");
  return `<div class="char"><h3>${markWords(c.name, words, esc)}${tag ? `<span class="char-tag">${tag}</span>` : ""}<span class="grow"></span>
      <button class="${forgetClass(armed)}" data-forget="${esc(c.name)}" title="Forget ${esc(c.name)}: who they are, their past, all of it">${forgetLabel(c.name, armed)}</button></h3>
    <div class="mem-list">${body}</div>${words.length ? "" : `
    <form class="mem-add char-add" data-cadd="${esc(c.name)}"><input class="grow" value="${esc(drafts[`+${c.name}`] ?? "")}" placeholder="Add to ${esc(c.name)}'s past" aria-label="Add to ${esc(c.name)}'s past" autocomplete="off"><button class="btn" type="submit">${icon("plus")}Add</button></form>`}</div>`;
}

const forgetClass = (armed: boolean) => `btn ghost${armed ? " danger armed" : ""}`;
const forgetLabel = (name: string, armed: boolean) => `${icon("trash-2")}${armed ? `Forget ${esc(name)}?` : "Forget"}`;

/** Edit a character's line (Enter saves, Esc cancels), clear it, add to its past, forget the character. One change
 * at a time (a double click is one); one the engine refuses (the line moved: the coach added to it meanwhile) shows
 * the page as it is now. A line of its past or story goes with the words shown (`was`): the engine finds it by them. */
function bindCharacters(host: HTMLElement): void {
  const change = async (body: Obj, after?: (ok: boolean) => void) => {
    if (busy) return;
    busy = true;
    try {
      const r = await memoryCall({ op: "character", ...body });
      after?.(!!r);
      memory = r || (await memoryCall()) || memory;
    } finally {
      busy = false;
    }
    renderMemory();
  };
  const line = (row: { name: string; r: CharRow }, text: string) => ({ name: row.name, field: row.r.f, text,
    index: row.r.i, ...(row.r.i === undefined ? {} : { was: row.r.text }) });
  const save = (k: number) => {
    const row = shownRows[k];
    const inp = host.querySelector<HTMLInputElement>(`[data-cedit="${k}"]`);
    if (!row || !inp || inp.value.trim() === row.r.text) {
      charEditing = null;
      if (row) delete drafts[rowKey(row.name, row.r)];
      renderMemory();
      return;
    }
    // saved, or refused (the line moved: the page is shown as it is now, the edit closed rather than on another line)
    const key = rowKey(row.name, row.r);
    change(line(row, inp.value), () => { charEditing = null; delete drafts[key]; });
  };
  host.querySelectorAll<HTMLButtonElement>("[data-ced]").forEach((b) => (b.onclick = () => {
    const k = Number(b.dataset.ced), row = shownRows[k];
    if (!row) return;
    if (charEditing === rowKey(row.name, row.r)) return save(k);
    charEditing = rowKey(row.name, row.r);
    renderMemory();
    host.querySelector<HTMLInputElement>("[data-cedit]")?.focus();     // (one line edited at a time)
  }));
  host.querySelectorAll<HTMLInputElement>("[data-cedit]").forEach((inp) => {
    const row = shownRows[Number(inp.dataset.cedit)];
    inp.oninput = () => { if (row) drafts[rowKey(row.name, row.r)] = inp.value; };
    inp.onkeydown = (e) => {
      if (e.key === "Enter") save(Number(inp.dataset.cedit));
      if (e.key === "Escape") { charEditing = null; if (row) delete drafts[rowKey(row.name, row.r)]; renderMemory(); }
    };
  });
  host.querySelectorAll<HTMLButtonElement>("[data-cclear]").forEach((b) => (b.onclick = () => {
    const row = shownRows[Number(b.dataset.cclear)];
    if (row) return change(line(row, ""));
  }));
  host.querySelectorAll<HTMLFormElement>("[data-cadd]").forEach((f) => {
    const name = f.dataset.cadd || "";
    const inp = f.querySelector<HTMLInputElement>("input");
    if (inp) inp.oninput = () => { drafts[`+${name}`] = inp.value; };
    f.onsubmit = (e) => {
      e.preventDefault();
      const text = inp?.value.trim() || "";
      if (text) return change({ name, field: "lore", text, index: null }, (ok) => { if (ok) delete drafts[`+${name}`]; });
    };
  });
  // Forget: pressed once it asks to be pressed again, for a few seconds (only the button changes: what's being typed
  // elsewhere stays)
  const arm = (name: string, on: boolean) => {
    forgetArmed = on ? name : null;
    host.querySelectorAll<HTMLButtonElement>("[data-forget]").forEach((b) => {
      if (b.dataset.forget !== name) return;
      b.className = forgetClass(on);
      b.innerHTML = forgetLabel(name, on);
    });
  };
  host.querySelectorAll<HTMLButtonElement>("[data-forget]").forEach((b) => (b.onclick = async () => {
    const name = b.dataset.forget || "";
    if (forgetArmed !== name) {
      if (forgetArmed) arm(forgetArmed, false);
      arm(name, true);
      window.setTimeout(() => { if (forgetArmed === name) arm(name, false); }, 4000);
      return;
    }
    if (busy) return;
    busy = true;
    forgetArmed = null;
    try {
      const r = await memoryCall({ op: "character_forget", name });
      if (r) { memory = r; toast(`${name} forgotten.`); }
    } finally {
      busy = false;
    }
    renderMemory();
  }));
}

export function renderReminders(): void {
  const host = $("rem-list");
  if (!live.engineUrl) { host.innerHTML = `<div class="rem-empty">Start the coach to see your reminders.</div>`; return; }
  const reminders = live.reminders;
  host.innerHTML = reminders.length ? reminders.map((r) => `<div class="rem-row"><button class="btn icon-only" data-done="${r.id}" title="Mark done" aria-label="Mark done">${icon("check")}</button>
    <div class="grow">${esc(r.what)}<small>${esc([r.when, r.every_min ? `nudges every ${r.every_min} min` : ""].filter(Boolean).join(", ") || "whenever")}</small></div></div>`).join("")
    : `<div class="rem-empty">No open reminders.</div>`;
  host.querySelectorAll<HTMLButtonElement>("[data-done]").forEach((b) => (b.onclick = () =>
    enginePost("/reminders", { done: Number(b.dataset.done) }).catch(() => undefined)));
}

// the search box: narrows as you type; Esc clears it; Enter goes to the first Edit shown; Ctrl+F jumps to it here
function setMemoryQuery(q: string): void {
  query = q;
  const box = $<HTMLInputElement>("mem-search");
  if (box.value !== q) box.value = q;
  renderMemory();
}

export function wireMemory(): void {
  const box = $<HTMLInputElement>("mem-search");
  let searchTimer: number | undefined;
  box.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => { query = box.value; renderMemory(); }, 120);
  });
  box.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); setMemoryQuery(""); }
    if (e.key === "Enter") {
      clearTimeout(searchTimer);
      setMemoryQuery(box.value);
      ($("mem-notes").querySelector<HTMLElement>("[data-ed]") || $("mem-chars").querySelector<HTMLElement>("[data-ced]"))?.focus();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (currentPage() !== "memory") return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") { e.preventDefault(); box.focus(); box.select(); }
    // (Esc in a line being edited cancels the edit only)
    else if (e.key === "Escape" && query && !(e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement)) setMemoryQuery("");
  });
  $("mem-add").onsubmit = async (e) => {
    e.preventDefault();
    const text = $<HTMLInputElement>("mem-text").value.trim();
    if (!text) return;
    const r = await memoryCall({ op: "add", text, kind: $<HTMLSelectElement>("mem-kind").value || undefined });
    if (r) { memory = r; $<HTMLInputElement>("mem-text").value = ""; toast("Remembered."); renderMemory(); }
  };
  $("mem-learning").onchange = async () => {
    memory = (await memoryCall({ op: "learning", on: $<HTMLInputElement>("mem-learning").checked })) || memory;
    toast($<HTMLInputElement>("mem-learning").checked ? "The coach will learn from your sessions." : "Learning is off; the coach only remembers what you tell it.");
  };
  $("mem-reflect").onclick = async () => {
    if (await memoryCall({ op: "reflect" })) toast("The coach is reviewing your recent sessions. New notes appear here and in the coach feed.", false, 7000);
    window.setTimeout(refreshMemory, 15000);
  };
  $("rem-add").onsubmit = (e) => {
    e.preventDefault();
    let q = $<HTMLInputElement>("rem-text").value.trim();
    if (!q || !live.engineUrl) return;
    if (!/remind me/i.test(q)) q = `remind me to ${q}`;
    enginePost("/ask", { q }).catch(() => undefined);
    $<HTMLInputElement>("rem-text").value = "";
    toast("Reminder sent to the coach.");
  };
}
