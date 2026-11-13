// Native delivery data uses Unicode scalar offsets; XML belongs only to the native worker.
export type SpeechFeature = "rate" | "pitch" | "boundaries" | "pauses";
export interface SpeechSpan {
  start: number;
  end: number;
  rate?: "slow" | "medium" | "fast";
  pitch?: "low" | "medium" | "high";
  after_ms?: number;
  boundary?: "s" | "p"; // following boundary at end, not a prosody scope
}
export interface SpeechMarkup { text: string; spans: SpeechSpan[] }
