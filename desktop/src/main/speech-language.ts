export function languageCode(value: unknown): string {
  const tag = typeof value === "string" ? value.trim().toLowerCase() : "";
  return /^[a-z]{2,3}(?:[-_][a-z0-9]{1,8})*$/.test(tag) ? tag.split(/[-_]/)[0] : "en";
}

/** English-only models must not be selected for another recognition language. */
export function effectiveWhisperModel(model: unknown, language: unknown): string {
  const name = String(model || "");
  if (languageCode(language) !== "en" && (!name || name.endsWith(".en"))) return "small";
  return name || "small.en";
}
