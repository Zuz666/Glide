// The session bar's order: each piece (data-sb) drags left or right along the bar, and the order is saved with the
// app settings (sessionBarOrder), so it outlives a restart. The spacer between the left and the right pieces stays
// where it is: a piece dropped on its left half ends the left group, on its right half starts the right one.
import { call } from "./bridge.js";
import { $ } from "./dom.js";

const pieces = () => [...$("sessionbar").children] as HTMLElement[];
const keys = () => pieces().map((p) => p.dataset.sb || "");

/** Put the pieces in the saved order; migrate split stt/tts chips right after models when upgrading from older arrays. */
export function orderSessionBar(order: string[] | undefined): void {
  if (!Array.isArray(order) || !order.length) return;
  const normalized = [...order];
  const modelsIdx = normalized.indexOf("models");
  if (modelsIdx >= 0) {
    const missing = (["stt", "tts"] as const).filter((k) => !normalized.includes(k));
    if (missing.length) normalized.splice(modelsIdx + 1, 0, ...missing);
  }
  const byKey = new Map(pieces().map((p) => [p.dataset.sb || "", p]));
  const want = [...normalized.filter((k) => byKey.has(k)), ...[...byKey.keys()].filter((k) => !normalized.includes(k))];
  if (want.join() === keys().join()) return;
  for (const k of want) $("sessionbar").appendChild(byKey.get(k)!);
}

export function wireSessionBarOrder(): void {
  const bar = $("sessionbar");
  let drag: { el: HTMLElement; x: number; id: number; moved: boolean } | null = null;
  let dragged = false;      // a drag just ended: the click that follows it isn't a click
  bar.addEventListener("pointerdown", (e) => {
    const el = (e.target as Element).closest("#sessionbar > [data-sb]") as HTMLElement | null;
    if (e.button !== 0 || !el || el.dataset.sb === "grow") return;
    drag = { el, x: e.clientX, id: e.pointerId, moved: false };
  });
  bar.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.moved) {
      if (Math.abs(e.clientX - drag.x) < 6) return;     // still a click
      drag.moved = true;
      bar.setPointerCapture(e.pointerId);
      drag.el.classList.add("sb-dragging");
      bar.classList.add("sb-moving");
    }
    // the piece goes before the first other shown piece (or the spacer) whose middle is right of the pointer
    const el = drag.el;
    const next = pieces().filter((p) => p !== el && p.offsetWidth)
      .find((p) => { const r = p.getBoundingClientRect(); return e.clientX < r.left + r.width / 2; }) || null;
    if (next ? el.nextElementSibling !== next : bar.lastElementChild !== el) bar.insertBefore(el, next);
  });
  const end = (e: PointerEvent) => {
    if (!drag || e.pointerId !== drag.id) return;
    const { el, moved } = drag;
    drag = null;
    if (!moved) return;
    el.classList.remove("sb-dragging");
    bar.classList.remove("sb-moving");
    if (bar.hasPointerCapture(e.pointerId)) bar.releasePointerCapture(e.pointerId);
    dragged = true;
    setTimeout(() => { dragged = false; }, 0);
    call("app:settings:set", { sessionBarOrder: keys() });
  };
  bar.addEventListener("pointerup", end);
  bar.addEventListener("pointercancel", end);
  bar.addEventListener("click", (e) => {
    if (!dragged) return;
    e.preventDefault();
    e.stopPropagation();
  }, true);
}
