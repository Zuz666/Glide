// Only the app's top-level page can reach native formatting. Its current Coach frame gets a bounded proxy.
import type { DateTimeRequest } from "../shared/date-time.js";
import { glide } from "./bridge.js";
import { toast } from "./dom.js";
import { live } from "./state.js";

export function wireDateTimes(): void {
  window.glideDates.useFormatter((requests) => glide.invoke("app:dates", requests));
  const frame = () => document.getElementById("dash") as HTMLIFrameElement | null;
  const origin = () => live.engineUrl ? new URL(live.engineUrl).origin : "";
  glide.on("app:datesChanged", () => {
    window.glideDates.refresh();
    if (origin()) frame()?.contentWindow?.postMessage({ type: "glide:datesChanged" }, origin());
  });
  document.addEventListener("date-format-error", () => toast("Windows date/time formatting failed. See Logs for details.", true));
  window.addEventListener("message", async (event) => {
    const target = frame()?.contentWindow, expectedOrigin = origin(), data = event.data;
    if (!target || event.source !== target || !expectedOrigin || event.origin !== expectedOrigin
      || data?.type !== "glide:dates" || !Number.isSafeInteger(data.id)
      || !Array.isArray(data.requests) || data.requests.length > 256) return;
    const id: number = data.id;
    try {
      const values = await glide.invoke("app:dates", data.requests as DateTimeRequest[]);
      if (frame()?.contentWindow === target && origin() === expectedOrigin)
        target.postMessage({ type: "glide:datesResult", id, values }, expectedOrigin);
    } catch {
      if (frame()?.contentWindow === target && origin() === expectedOrigin)
        target.postMessage({ type: "glide:datesResult", id, error: "Windows date/time formatting failed" }, expectedOrigin);
    }
  });
}
