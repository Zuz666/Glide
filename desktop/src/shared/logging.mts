// Explicit ESM keeps both desktop builds' shared runtime output identical; ipc.ts stays type-only.
import type { LogAttribution } from "./ipc.js";

const STRING_FIELDS = ["operation", "model", "reported_model", "provider", "role", "reasoning", "sent_reasoning",
  "status", "voice", "language", "request_id", "previous_reasoning", "application"] as const;
const NUMBER_FIELDS = ["elapsed_ms", "rate", "volume"] as const;
const PREFIX = "GLIDE_LOG ";
const MAX_ENVELOPE = 1_000_000;
const MAX_FIELD = 512;

/** Copy only the bounded, flat attribution contract, in canonical field order. */
export function normalizeLogAttribution(value: unknown): LogAttribution | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const input = value as Record<string, unknown>;
  if (input.category !== "AI" && input.category !== "STT" && input.category !== "TTS") return undefined;
  const result: LogAttribution = { category: input.category };
  for (const key of STRING_FIELDS) {
    const field = input[key];
    if (typeof field === "string" && field.length > 0 && field.length <= MAX_FIELD && !/[\u0000-\u001f\u007f\u2028\u2029]/.test(field)) result[key] = field;
  }
  for (const key of NUMBER_FIELDS) {
    const field = input[key];
    if (typeof field === "number" && Number.isFinite(field) && Math.abs(field) <= 1_000_000_000) result[key] = field;
  }
  return result;
}

/** Invalid envelopes stay ordinary raw output; never interpret arbitrary process logs as metadata. */
export function parseLogEnvelope(line: string): { text: string; attribution?: LogAttribution } {
  if (line.startsWith(PREFIX) && line.length <= MAX_ENVELOPE) {
    try {
      const envelope: unknown = JSON.parse(line.slice(PREFIX.length));
      if (envelope && typeof envelope === "object" && !Array.isArray(envelope)) {
        const record = envelope as Record<string, unknown>;
        const attribution = normalizeLogAttribution(record.attribution);
        if (typeof record.text === "string" && attribution) return { text: record.text, attribution };
      }
    } catch { /* Keep malformed JSON as the original readable process output. */ }
  }
  return { text: line };
}

/** The same readable metadata suffix for clipboard and disk, without forged extra log lines. */
export function formatLogText(text: string, attribution?: LogAttribution): string {
  const metadata = normalizeLogAttribution(attribution);
  if (!metadata) return text;
  const readable = text.replace(/[\u0000-\u001f\u007f\u2028\u2029]/g, (character) => {
    const escaped = JSON.stringify(character).slice(1, -1);
    return escaped === character ? `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}` : escaped;
  });
  return `${readable} [attribution] ${JSON.stringify(metadata)}`;
}
