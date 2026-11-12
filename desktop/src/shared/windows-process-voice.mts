import type { SpeechSettings } from "./ipc.js";

/** Legacy explicit voices, including the empty Windows-default name, are separate until linking is chosen. */
export function usesAppWindowsVoice(speech: SpeechSettings | undefined, engineVoice: unknown): boolean {
  return speech?.windowsProcessUseAppVoice ?? (engineVoice === undefined);
}
