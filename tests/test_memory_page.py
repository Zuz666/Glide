#!/usr/bin/env python3
"""The Memory page (desktop/src/renderer/memory.ts) with its search (memory-search.ts), compiled with the project's
TypeScript beside stand-ins for the page and the engine, and run under Node: the notes by kind, and the characters
the coach played (who is played now, their lines: who they are, how they sound, who the player is to them, their past,
their own story threads), each line to edit or clear. The search narrows both to what has every word typed (marked), a
character whole when its name matches; nothing matching says so. Text is escaped. And played through, on a stand-in
page that finds the buttons and boxes in what was drawn: an edit (what's typed kept while the search narrows the page;
saved with the words it replaces), a double click that is one change, an edit the engine refuses (the page shown as
it is now), an add box kept while Forget asks to be pressed again (only its button changing), Forget pressed twice.

Needs node and desktop/node_modules (skips with 77 without them).
Run: python3 tests/test_memory_page.py   (prints OK)
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import desktop_ts

HERE = os.path.dirname(os.path.abspath(__file__))
RENDERER = os.path.join(HERE, "..", "desktop", "src", "renderer")

node = desktop_ts.node
if not node or not desktop_ts.available():
    print("SKIP: needs node and desktop/node_modules (cd desktop && npm ci)")
    sys.exit(77)

STUBS = {
    "dom.ts": r"""
export const toast = (..._a: unknown[]) => undefined;
export const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!));
export const icon = (n: string, cls = "icon") => `<svg class="${cls}" aria-hidden="true"><use href="#i-${n}"/></svg>`;
// a stand-in element: finds the buttons and boxes ("[data-x]", "[data-x=\"v\"]", a form's "input") in what was drawn
// into it, the same object each time until it's drawn again (the handlers the page binds stay on it)
const unesc = (s: string) => s.replace(/&(amp|lt|gt|quot|#39);/g, (_m: string, e: string) =>
  ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" } as Record<string, string>)[e]);
function fake(dataset: Record<string, string> = {}, around = ""): any {
  let html = "";
  const el: any = { dataset, value: "", className: "", disabled: false, checked: false, on: {}, made: new Map(),
    addEventListener(t: string, fn: any) { el.on[t] = fn; }, focus() { (globalThis as any).focused = el; },
    get innerHTML() { return html; }, set innerHTML(v: string) { html = v; el.made = new Map(); },
    querySelectorAll(sel: string) {
      const m = /^\[data-([\w-]+)(?:="([^"]*)")?\]$/.exec(sel);
      if (!m) return [];
      const camel = m[1].replace(/-(\w)/g, (_: string, ch: string) => ch.toUpperCase());
      const out: any[] = [];
      for (const x of html.matchAll(new RegExp(`<[a-z]+[^>]*?[ ]data-${m[1]}="([^"]*)"[^>]*>`, "g"))) {
        const v = unesc(x[1]);
        if (m[2] !== undefined && v !== m[2]) continue;
        let child = el.made.get(m[1] + "=" + v);
        if (!child) {
          child = fake({ [camel]: v }, html.slice(x.index));
          const val = /\svalue="([^"]*)"/.exec(x[0]);
          child.value = val ? unesc(val[1]) : "";
          el.made.set(m[1] + "=" + v, child);
        }
        out.push(child);
      }
      return out;
    },
    querySelector(sel: string) {
      if (sel === "input") {
        if (!el.input) { el.input = fake(); const v = /<input[^>]*?\svalue="([^"]*)"/.exec(around); el.input.value = v ? unesc(v[1]) : ""; }
        return el.input;
      }
      return el.querySelectorAll(sel)[0] || null;
    } };
  return el;
}
const els: Record<string, any> = {};
(globalThis as any).els = els;
export function $<T extends HTMLElement = HTMLElement>(id: string): T {
  return (els[id] ??= fake({ id })) as unknown as T;
}
""",
    "nav.ts": "export const currentPage = () => \"memory\";\n",
    "state.ts": """
export const live: { engineUrl: string | null; reminders: { id: number; what: string; when?: string; every_min?: number }[] } =
  { engineUrl: "http://engine", reminders: [] };
export const engineGet = async (_p: string) => ({ json: async () => (globalThis as any).memoryFixture });
export const enginePost = async (_p: string, b: unknown) => {
  (globalThis as any).posts.push(b);
  return { json: async () => ((globalThis as any).refuse ? { ok: false, error: "it changed meanwhile" } : { memory: (globalThis as any).memoryFixture }) };
};
""",
    "types.ts": "export type Obj = any;\n",
}

FIXTURE = {
    "learning": True, "persistent": True, "playing": "Sarge",
    "kinds": [{"id": "preference", "label": "Preferences"}, {"id": "fact", "label": "Facts"}],
    "notes": [{"id": 1, "kind": "preference", "text": "Prefers short callouts", "source": "player", "created": 1,
               "updated": 1, "pinned": False, "used": 0},
              {"id": 2, "kind": "fact", "text": "Plays a <b>dwarf</b> warrior from Gnomeregan's edge", "source": "coach",
               "created": 1, "updated": 2, "pinned": True, "used": 3}],
    "characters": [
        {"name": "Sarge", "brief": "A grizzled dwarf drill sergeant.", "voice": "Gruff, booming.",
         "role": "Their drill sergeant; calls them recruit.", "scene": "", "lore": ["Lost his beard to a rocket."],
         "plot": [], "last": 2},
        {"name": "Fizzle", "brief": "A twitchy gnome tinker.", "voice": "", "role": "", "scene": "",
         "lore": ["Grew up in Gnomeregan.", "Hates boats.", "Owes Sarge a favour."], "plot": ["Wants his workshop back."],
         "last": 1}]}

RUN = r"""
const timers = [];                  // (window.setTimeout: Forget's few seconds, run when the test says)
globalThis.document = { addEventListener() {} };
globalThis.window = { setTimeout: (fn) => (timers.push(fn), timers.length), clearTimeout() {} };
globalThis.memoryFixture = %s;
globalThis.posts = [];
const m = await import(%s);
const els = globalThis.els;
const settle = () => new Promise((r) => setTimeout(r, 5));
m.wireMemory();
await m.refreshMemory();
await settle();
const shot = () => ({ notes: els["mem-notes"].innerHTML, chars: els["mem-chars"].innerHTML });
const out = { all: shot() };
const search = (q) => { els["mem-search"].value = q; els["mem-search"].on.keydown({ key: "Enter", preventDefault() {} }); return shot(); };
out.gnome = search("gnomeregan");
out.sarge = search("sarge");
out.none = search("zzz");
out.back = (els["mem-search"].on.keydown({ key: "Escape", preventDefault() {} }), shot());
const chars = els["mem-chars"];
const last = () => globalThis.posts[globalThis.posts.length - 1];
const edBy = (text) => chars.querySelector(`[data-ced="${new RegExp(text + `</div></div><div class="acts"><button[^>]*data-ced="([0-9]+)"`).exec(chars.innerHTML)[1]}"]`);
const clearBy = (text) => chars.querySelector(`[data-cclear="${new RegExp(text + `</div></div><div class="acts">.*?data-cclear="([0-9]+)"`).exec(chars.innerHTML)[1]}"]`);

// an edit: what's typed stays while the search narrows the page; saved with the words it replaces
await edBy("Hates boats\.").onclick();
let inp = chars.querySelector("[data-cedit]");
inp.value = "Hates boats and lakes."; inp.oninput();
search("boats");
out.draftKept = chars.innerHTML.includes('value="Hates boats and lakes."');
chars.querySelector("[data-cedit]").onkeydown({ key: "Enter" }); await settle();
out.edit = last();
els["mem-search"].on.keydown({ key: "Escape", preventDefault() {} });

// a double click is one change
const n0 = globalThis.posts.length;
const cl = clearBy("Lost his beard to a rocket\.");
cl.onclick(); cl.onclick(); await settle();
out.doubleClick = globalThis.posts.length - n0;

// refused (the coach added to it meanwhile): the page as it is now, the edit closed
globalThis.refuse = true;
await edBy("Grew up in Gnomeregan\.").onclick();
inp = chars.querySelector("[data-cedit]"); inp.value = "Grew up beneath Gnomeregan."; inp.oninput();
inp.onkeydown({ key: "Enter" }); await settle();
globalThis.refuse = false;
out.refused = { post: last(), editOpen: chars.innerHTML.includes("data-cedit") };

// an add box keeps what's typed while Forget asks (only its button changes), and after the page is drawn again
let form = chars.querySelector('[data-cadd="Fizzle"]');
form.querySelector("input").value = "Fears water."; form.querySelector("input").oninput();
const before = chars.innerHTML;
const fg = chars.querySelector('[data-forget="Sarge"]');
await fg.onclick();
out.armed = { button: fg.innerHTML, className: fg.className, redrawn: chars.innerHTML !== before };
timers.splice(0).forEach((fn) => fn());
out.disarmed = fg.innerHTML;
search("");
form = chars.querySelector('[data-cadd="Fizzle"]');
out.addKept = form.querySelector("input").value;
await form.onsubmit({ preventDefault() {} });
out.add = last();

// Forget, pressed twice
const f2 = chars.querySelector('[data-forget="Fizzle"]');
await f2.onclick(); await f2.onclick(); await settle();
out.forget = last();
console.log(JSON.stringify(out));
"""

tmp = tempfile.mkdtemp(prefix="glide-memory-page-")
try:
    files = {f"renderer/{name}": data for name, data in STUBS.items()}
    for name in ("memory.ts", "memory-search.ts", "date-time.ts"):
        with open(os.path.join(RENDERER, name), encoding="utf-8") as fh:
            files[f"renderer/{name}"] = fh.read()
    with open(os.path.join(RENDERER, "..", "shared", "date-time.ts"), encoding="utf-8") as fh:
        files["shared/date-time.ts"] = fh.read()
    out = desktop_ts.compiled("memory-page", files, ["renderer/memory.ts", "--target", "ES2023", "--module", "ES2022",
                                                     "--moduleResolution", "bundler", "--lib", "ES2023,DOM", "--strict",
                                                     "--skipLibCheck"])
    run = os.path.join(tmp, "run.mjs")
    with open(run, "w", encoding="utf-8") as fh:
        fh.write(RUN % (json.dumps(FIXTURE), json.dumps(desktop_ts.url(os.path.join(out, "renderer", "memory.js")))))
    r = subprocess.run([node, run], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, "running the Memory page failed: %s" % r.stderr[-2000:]
    got = json.loads(r.stdout)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
if os.environ.get("GLIDE_MEMORY_PAGE_DUMP"):            # (the fragments, for a look at the page in a browser)
    with open(os.environ["GLIDE_MEMORY_PAGE_DUMP"], "w", encoding="utf-8") as fh:
        json.dump(got, fh)


def text(markup):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


allp = got["all"]
# the notes by kind, escaped; the characters: the one played marked, each line to edit (and clear)
assert "Prefers short callouts" in allp["notes"] and "Plays a &lt;b&gt;dwarf&lt;/b&gt;" in allp["notes"], allp["notes"]
chars = text(allp["chars"])
assert "Characters the coach has played 2" in chars, chars
assert re.search(r"Sarge\s*</?\w*.*?Playing now", allp["chars"]) and allp["chars"].count("Playing now") == 1, chars
for want in ("Who they are A grizzled dwarf drill sergeant.", "How they sound Gruff, booming.",
             "Who you are to them Their drill sergeant; calls them recruit.",
             "Their past Lost his beard to a rocket.", "Their past Grew up in Gnomeregan. Hates boats. Owes Sarge a favour.",
             "Their own story Wants his workshop back.", "How they sound Not set yet", "Add"):
    assert want in chars, (want, chars)
assert allp["chars"].count("data-ced=") == 11 and allp["chars"].count("data-cclear=") == 7, \
    "every line edits; all but who they are and the unset ones clear"
assert allp["chars"].count('data-forget="') == 2 and 'placeholder="Add to Fizzle\'s past"' in allp["chars"]

# searching: the note and the character lines with the word (marked); not the rest
g = got["gnome"]
assert "Prefers short" not in g["notes"] and "<mark>Gnomeregan</mark>&#39;s edge" in g["notes"], g["notes"]
assert "Characters the coach has played 1 of 2" in text(g["chars"]), text(g["chars"])
assert "Grew up in <mark>Gnomeregan</mark>." in g["chars"] and "Hates boats" not in g["chars"] and \
    "Lost his beard" not in g["chars"] and "data-cadd" not in g["chars"], g["chars"]
# a character's name: all of it; the other's lines that name it
s = got["sarge"]
assert "Lost his beard to a rocket." in s["chars"] and "<mark>Sarge</mark>" in s["chars"], s["chars"]
assert "Owes <mark>Sarge</mark> a favour." in s["chars"] and "Hates boats" not in s["chars"], s["chars"]
assert s["notes"] == "", "no note has it: no empty box"
# nothing: says so; Esc: all of it again
assert "No memories match “zzz”" in got["none"]["notes"] and got["none"]["chars"] == "", got["none"]
assert got["back"] == allp, "Esc clears the search"

# the edit, its draft kept; saved by the words it replaces
assert got["draftKept"], "what's typed stays while the search narrows the page"
assert got["edit"] == {"op": "character", "name": "Fizzle", "field": "lore", "text": "Hates boats and lakes.",
                       "index": 1, "was": "Hates boats."}, got["edit"]
assert got["doubleClick"] == 1, "a double click is one change"
assert got["refused"]["post"]["was"] == "Grew up in Gnomeregan." and not got["refused"]["editOpen"], got["refused"]
a = got["armed"]
assert "Forget Sarge?" in a["button"] and "armed" in a["className"] and not a["redrawn"], a
assert "Forget Sarge?" not in got["disarmed"] and got["addKept"] == "Fears water.", (got["disarmed"], got["addKept"])
assert got["add"] == {"op": "character", "name": "Fizzle", "field": "lore", "text": "Fears water.", "index": None}, got["add"]
assert got["forget"] == {"op": "character_forget", "name": "Fizzle"}, got["forget"]
print("OK")
