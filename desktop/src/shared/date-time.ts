// Presentation only: numbers are Unix milliseconds; strings are calendar dates, never midnight instants.
export type DateTimeStyle = "date" | "shortTime" | "longTime" | "dateTime";
export interface DateTimeRequest { value: number | string; style: DateTimeStyle }
export type DateTimeFormatter = (requests: DateTimeRequest[]) => Promise<string[]>;
export interface DateTimeDisplay {
  useFormatter(format: DateTimeFormatter): void;
  refresh(): void;
  text(value: number | string, style: DateTimeStyle): string;
}
declare global { interface Window { glideDates: DateTimeDisplay } }
