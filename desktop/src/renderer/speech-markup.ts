// Renderer preparation never serializes XML; the native worker remains the compiler.
import type { SpeechFeature, SpeechMarkup, SpeechSpan } from "../shared/speech-markup.js";

export const SPEECH_FEATURES: readonly SpeechFeature[] = ["rate", "pitch", "boundaries", "pauses"];

export function speechFeatures(value: unknown): SpeechFeature[] {
  return Array.isArray(value) ? SPEECH_FEATURES.filter((feature) => value.includes(feature)) : [];
}

/** Automatic controls degrade independently, never changing canonical words or retargeting a span. */
export function prepareSpeechMarkup(text: string, markup: SpeechMarkup | undefined,
  features: readonly SpeechFeature[], mode = "ai", localRules = true): SpeechMarkup | undefined {
  if (mode === "off" || !features.length) return undefined;
  if (!markup && (!localRules || !features.includes("boundaries"))) return undefined;
  const chars = Array.from(text);
  const spans: SpeechSpan[] = [];
  let lastEnd = 0, pauseTotal = 0;
  if (markup?.text === text && Array.isArray(markup.spans)) {
    for (const source of markup.spans.slice(0, 128)) {
      if (!source || !Number.isInteger(source.start) || !Number.isInteger(source.end)
        || source.start < lastEnd || source.start < 0 || source.end <= source.start || source.end > chars.length) continue;
      const span: SpeechSpan = { start: source.start, end: source.end };
      if (features.includes("rate") && ["slow", "medium", "fast"].includes(source.rate || "")) span.rate = source.rate;
      if (features.includes("pitch") && ["low", "medium", "high"].includes(source.pitch || "")) span.pitch = source.pitch;
      if (features.includes("boundaries") && (source.boundary === "s" || source.boundary === "p")) span.boundary = source.boundary;
      if (features.includes("pauses") && Number.isInteger(source.after_ms) && source.after_ms! > 0
        && source.after_ms! <= 1500 && pauseTotal + source.after_ms! <= 3000) {
        span.after_ms = source.after_ms;
        pauseTotal += source.after_ms!;
      }
      if (span.rate || span.pitch || span.boundary || span.after_ms) {
        spans.push(span);
        lastEnd = span.end;
      }
    }
  }
  // Only reliable final-language punctuation supplies local structure, not guessed emotional delivery.
  if (localRules && features.includes("boundaries")) {
    for (let end = 1; end <= chars.length; end++) {
      const paragraph = chars[end] === "\n" && chars[end + 1] === "\n" && !/\s/u.test(chars[end - 1]);
      const sentence = /[.!?。！？]/u.test(chars[end - 1]) && (end === chars.length || /\s/u.test(chars[end]));
      if (!paragraph && !sentence) continue;
      const existing = spans.find((span) => span.end === end);
      if (existing) {
        if (paragraph || !existing.boundary) existing.boundary = paragraph ? "p" : "s";
      } else if (spans.length < 128 && !spans.some((span) => end - 1 < span.end && end > span.start)) {
        spans.push({ start: end - 1, end, boundary: paragraph ? "p" : "s" });
      }
    }
  }
  spans.sort((a, b) => a.start - b.start);
  return spans.length ? { text, spans } : undefined;
}
