import type { SpeechFeature } from "../shared/speech-markup";

/** Main-process permission normalization; XML/control validation belongs to the native worker. */
export function speechFeatures(value: unknown): SpeechFeature[] {
  if (!Array.isArray(value)) return [];
  return (["rate", "pitch", "boundaries", "pauses"] as const).filter((feature) => value.includes(feature));
}
