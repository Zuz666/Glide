// Safe timestamp markup and leaf bindings; native transport is initialized separately by the app.
import type { DateTimeStyle } from "../shared/date-time.js";
import { esc } from "./dom.js";

const DATE_ATTRIBUTES = ["dateValue", "dateKind", "dateStyle", "datePrefix", "dateSuffix", "dateAttribute"];

export function dateTimeHtml(value: number | string, style: DateTimeStyle): string {
  return `<time data-date-value="${esc(value)}" data-date-kind="${typeof value === "string" ? "calendar" : "instant"}" data-date-style="${style}">${esc(dateTimeText(value, style))}</time>`;
}
export function dateTimeText(value: number | string, style: DateTimeStyle): string {
  return window.glideDates?.text(value, style) || "";
}
export function clearDateTime(el: HTMLElement): void {
  for (const key of DATE_ATTRIBUTES) delete el.dataset[key];
}
export function bindDateTime(el: HTMLElement, value: number | string, style: DateTimeStyle,
  options: { prefix?: string; suffix?: string; attribute?: "title" } = {}): void {
  el.dataset.dateValue = String(value);
  el.dataset.dateKind = typeof value === "string" ? "calendar" : "instant";
  el.dataset.dateStyle = style;
  el.dataset.datePrefix = options.prefix || "";
  el.dataset.dateSuffix = options.suffix || "";
  el.dataset.dateAttribute = options.attribute || "";
}

