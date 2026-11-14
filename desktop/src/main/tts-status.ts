// Authoritative desktop TTS live status shared between the coach screen and the click-through game overlay.
import type { AppSettings, TtsPhase, TtsPlaybackReport, TtsProvider, TtsStatus, Voice } from "../shared/ipc";
import { TTS_KEYS } from "./tts";

export type EdgeStatusSnapshot = {
  workerState: "stopped" | "starting" | "ready" | "reconnecting" | "error";
  readySlots: number;
  totalSlots: number;
  detail?: string;
};

export type WindowsSpeechEvent = {
  type: "submitted" | "started" | "done" | "cancelled" | "error";
  id: string;
  voice?: string;
  elapsedMs?: number;
};

const HOLD_MS = 8000;
const HANDOFF_MS = 1500;
const FALLBACK_GRACE_MS = 250;
const IDENTIFIER = /^[A-Za-z0-9_.:-]{1,128}$/;

function shortEdgeOrAzureVoice(raw: string): string {
  const trimmed = String(raw || "").trim();
  if (!trimmed) return "";
  return trimmed
    .replace(/^[a-z]{2,3}-[A-Z]{2,4}-/, "")
    .replace(/(?:DragonHDLatest|Multilingual)?Neural$/i, "")
    .trim() || trimmed;
}

function shortGoogleVoice(raw: string): string {
  const trimmed = String(raw || "").trim();
  if (!trimmed) return "Charon";
  const parts = trimmed.split("-").filter(Boolean);
  return parts.at(-1) || trimmed;
}

export function shortSapi5Voice(raw: string): string {
  const trimmed = String(raw || "").trim();
  if (!trimmed) return "";
  const noParen = trimmed.replace(/\s*\([^)]*\)\s*$/, "").trim();
  const simplified = noParen
    .replace(/^Microsoft\s+/i, "")
    .replace(/\s+Desktop(?:\s*-.*)?$/i, "")
    .replace(/\s+-\s+.*$/, "")
    .trim();
  return simplified || noParen || trimmed;
}

function describeVoice(
  provider: TtsProvider,
  settings: AppSettings,
  windowsVoice = "",
): { voiceLabel: string; fullVoice: string } {
  const s = settings.speech || {};
  if (provider === "edge") {
    const raw = s.edge?.voice || "";
    const short = shortEdgeOrAzureVoice(raw);
    return {
      voiceLabel: `Edge · ${short || "No voice"}`,
      fullVoice: raw ? `Edge-TTS (${raw})` : "Edge-TTS (no voice selected)",
    };
  }
  if (provider === "openai") {
    const voice = s.openai?.voice || "coral";
    const model = s.openai?.model || "gpt-4o-mini-tts";
    return { voiceLabel: `OpenAI · ${voice}`, fullVoice: `OpenAI (${model} · ${voice})` };
  }
  if (provider === "elevenlabs") {
    const id = s.elevenlabs?.voiceId || "JBFqnCBsd6RMkjVDRZzb";
    const model = s.elevenlabs?.model || "eleven_flash_v2_5";
    const short = id === "JBFqnCBsd6RMkjVDRZzb" ? "George" : id.slice(0, 10);
    return { voiceLabel: `ElevenLabs · ${short}`, fullVoice: `ElevenLabs (${model} · ${id})` };
  }
  if (provider === "azure") {
    const voice = s.azure?.voice || "en-US-AndrewMultilingualNeural";
    return { voiceLabel: `Azure · ${shortEdgeOrAzureVoice(voice)}`, fullVoice: `Azure Speech (${voice})` };
  }
  if (provider === "google") {
    const voice = s.google?.voice || "en-US-Chirp3-HD-Charon";
    return { voiceLabel: `Google · ${shortGoogleVoice(voice)}`, fullVoice: `Google Cloud (${voice})` };
  }
  const raw = windowsVoice;
  const short = shortSapi5Voice(raw) || "Default";
  return {
    voiceLabel: `SAPI5 · ${short}`,
    fullVoice: raw ? `Windows SAPI5 (${raw})` : "Windows SAPI5 (Windows default)",
  };
}

type ActiveRequest = {
  state: "generating" | "handoff" | "speaking" | "fallback";
  owner?: object;
  fallbackVoice?: string;
  requestId?: string;
  actualVoice?: string;
  synthMs?: number;
  detail?: string;
  timer?: NodeJS.Timeout;
};

type HoldState = {
  phase: "fallback" | "error";
  fallbackVoice?: string;
  detail?: string;
  updatedAt: number;
  timer: NodeJS.Timeout;
};

export class TtsStatusCoordinator {
  private readonly active = new Map<string, ActiveRequest>();
  private hold?: HoldState;
  private lastWindowsVoice?: string;
  private readonly windowsVoiceNames = new Map<string, string>();
  private lastSignature = "";
  private lastSnapshot?: TtsStatus;
  private lastConfigKey?: string;

  constructor(
    private readonly deps: {
      getSettings: () => AppSettings;
      hasKey: (provider: TtsProvider) => boolean;
      edgeSnapshot: () => EdgeStatusSnapshot;
      windowsWorkerState: () => "stopped" | "starting" | "ready";
      onBroadcast: (status: TtsStatus) => void;
    },
  ) {}

  setWindowsVoices(voices: readonly Voice[]): void {
    this.windowsVoiceNames.clear();
    for (const voice of voices) {
      this.windowsVoiceNames.set(voice.id, voice.name);
      if (voice.identity) this.windowsVoiceNames.set(voice.identity, voice.name);
      if (voice.isDefault) this.windowsVoiceNames.set("", voice.name);
    }
    this.publish();
  }

  private windowsVoiceName(selection: string): string {
    const name = this.windowsVoiceNames.get(selection);
    if (name) return name;
    const raw = selection || this.lastWindowsVoice || "";
    // Unresolved registry tokens are selectors, never display names; details remain in diagnostics.
    return /^HKEY_/i.test(raw) ? "Selected voice (name unavailable)" : raw;
  }

  current(): TtsStatus {
    return this.compute(this.lastSnapshot?.updatedAt ?? Date.now());
  }

  refresh(): TtsStatus {
    const s = this.deps.getSettings();
    const provider: TtsProvider = s.speech?.provider || "windows";
    const selection = provider === "windows" ? s.speech?.voiceName || this.lastWindowsVoice || ""
      : describeVoice(provider, s).fullVoice;
    const configKey = `${provider}|${selection}`;
    if (this.lastConfigKey !== undefined && this.lastConfigKey !== configKey) {
      this.clearHold();
    }
    this.lastConfigKey = configKey;
    if (s.muted) {
      this.clearAllActive();
      this.clearHold();
    }
    return this.publish();
  }

  beginSynthesis(requestId: string, detail?: string, owner?: object): void {
    if (!IDENTIFIER.test(requestId)) return;
    this.clearHold();
    const prev = this.active.get(requestId);
    clearTimeout(prev?.timer);
    if (prev?.state === "speaking") return; // Streaming overlap rule: never flicker back from speaking to generating.
    this.active.set(requestId, { state: "generating", owner: owner ?? prev?.owner, detail: detail || prev?.detail });
    this.publish();
  }

  finishSynthesis(requestId: string, ok: boolean, detail?: string): void {
    const entry = this.active.get(requestId);
    if (!entry) return; // Already cancelled or ended by the caller; ignore late completion/rejection.
    if (entry.timer) { clearTimeout(entry.timer); entry.timer = undefined; }
    if (detail) entry.detail = detail;
    if (ok) {
      if (entry.state === "speaking" || entry.state === "fallback") {
        this.publish();
        return;
      }
      entry.state = "handoff";
      entry.timer = setTimeout(() => {
        const cur = this.active.get(requestId);
        if (cur === entry && cur.state === "handoff") {
          this.active.delete(requestId);
          this.publish();
        }
      }, HANDOFF_MS);
      entry.timer.unref();
      this.publish();
      return;
    }
    if (entry.state === "speaking" || entry.state === "fallback") {
      // An audible head fragment is still playing; narrator owns fallback/error reporting when playback finishes.
      this.publish();
      return;
    }
    // Brief grace window so narrator's immediate SAPI5 fallback transitions directly without an error flash.
    entry.timer = setTimeout(() => {
      const cur = this.active.get(requestId);
      if (cur === entry && cur.state !== "fallback" && cur.state !== "speaking") {
        this.active.delete(requestId);
        this.startHold("error", undefined, entry.detail);
      }
    }, FALLBACK_GRACE_MS);
    entry.timer.unref();
    this.publish();
  }

  cancelSynthesis(requestId: string): void {
    const entry = this.active.get(requestId);
    if (!entry) return;
    clearTimeout(entry.timer);
    this.active.delete(requestId);
    this.publish();
  }

  hasOwner(owner: object): boolean {
    for (const entry of this.active.values()) {
      if (entry.owner === owner) return true;
    }
    return false;
  }

  clearOwner(owner: object): void {
    let changed = false;
    for (const [id, entry] of [...this.active.entries()]) {
      if (entry.owner !== owner) continue;
      clearTimeout(entry.timer);
      this.active.delete(id);
      changed = true;
    }
    if (changed) this.publish();
  }

  reportPlayback(report: TtsPlaybackReport, owner?: object): void {
    if (!report || typeof report !== "object") return;
    const { requestId, state } = report;
    if (typeof requestId !== "string" || !IDENTIFIER.test(requestId)) return;
    const prev = this.active.get(requestId);
    if (prev?.timer) { clearTimeout(prev.timer); prev.timer = undefined; }
    const resolvedOwner = owner ?? prev?.owner;
    if (state === "playing") {
      this.clearHold();
      this.active.set(requestId, { state: "speaking", owner: resolvedOwner, detail: prev?.detail });
      this.publish();
      return;
    }
    if (state === "fallback") {
      this.clearHold();
      const fb = typeof report.fallbackVoice === "string"
        ? shortSapi5Voice(this.windowsVoiceName(report.fallbackVoice)).slice(0, 128) : undefined;
      const defaultFb = shortSapi5Voice(this.windowsVoiceName(this.deps.getSettings().speech?.voiceName || "")) || "Default";
      this.active.set(requestId, {
        state: "fallback",
        owner: resolvedOwner,
        fallbackVoice: fb || prev?.fallbackVoice || defaultFb,
        detail: prev?.detail,
      });
      this.publish();
      return;
    }
    if (state === "ended") {
      this.active.delete(requestId);
      if (prev?.state === "fallback") {
        this.startHold("fallback", prev.fallbackVoice, prev.detail);
      } else {
        this.publish();
      }
      return;
    }
    if (state === "error") {
      this.active.delete(requestId);
      this.startHold("error", undefined, prev?.detail);
    }
  }

  onWindowsEvent(ev: WindowsSpeechEvent): void {
    if (ev.voice) this.lastWindowsVoice = ev.voice;
    const existing = this.active.get(ev.id);
    if (existing?.state === "fallback") {
      if (ev.type === "started" || ev.type === "done") {
        if (ev.voice) {
          existing.fallbackVoice = shortSapi5Voice(this.windowsVoiceName(ev.voice)) || existing.fallbackVoice;
          existing.actualVoice = ev.voice;
        }
        if (ev.type === "started") {
          existing.requestId = ev.id;
          if (typeof ev.elapsedMs === "number") existing.synthMs = ev.elapsedMs;
        }
        this.publish();
      } else if (ev.type === "cancelled") {
        clearTimeout(existing.timer);
        this.active.delete(ev.id);
        this.publish();
      } else if (ev.type === "error") {
        clearTimeout(existing.timer);
        this.active.delete(ev.id);
        this.startHold("error", undefined, existing.detail || "SAPI5 fallback failed");
      }
      return;
    }
    if (ev.type === "submitted") {
      if (existing) {
        // SAPI5 started for a failed online request before reportPlayback("fallback") arrived.
        if (existing.timer) { clearTimeout(existing.timer); existing.timer = undefined; }
        existing.state = "fallback";
        existing.fallbackVoice = shortSapi5Voice(this.windowsVoiceName(this.deps.getSettings().speech?.voiceName || "")) || "Default";
      } else {
        this.clearHold();
        this.active.set(ev.id, { state: "generating" });
      }
      this.publish();
    } else if (ev.type === "started") {
      this.clearHold();
      this.active.set(ev.id, {
        state: "speaking",
        requestId: ev.id,
        ...(ev.voice ? { actualVoice: ev.voice } : {}),
        ...(typeof ev.elapsedMs === "number" ? { synthMs: ev.elapsedMs } : {}),
      });
      this.publish();
    } else if (ev.type === "done" || ev.type === "cancelled") {
      clearTimeout(existing?.timer);
      this.active.delete(ev.id);
      this.publish();
    } else if (ev.type === "error") {
      clearTimeout(existing?.timer);
      this.active.delete(ev.id);
      this.startHold("error", undefined, "SAPI5 speech failed");
    }
  }

  private startHold(phase: "fallback" | "error", fallbackVoice?: string, detail?: string): void {
    this.clearHold();
    const updatedAt = Date.now();
    const timer = setTimeout(() => {
      if (this.hold?.timer === timer) {
        this.hold = undefined;
        this.publish();
      }
    }, HOLD_MS);
    timer.unref();
    this.hold = { phase, fallbackVoice, detail, updatedAt, timer };
    this.publish(updatedAt);
  }

  private clearHold(): void {
    if (!this.hold) return;
    clearTimeout(this.hold.timer);
    this.hold = undefined;
  }

  private clearAllActive(): void {
    for (const entry of this.active.values()) clearTimeout(entry.timer);
    this.active.clear();
  }

  private compute(now: number): TtsStatus {
    const s = this.deps.getSettings();
    const provider: TtsProvider = s.speech?.provider || "windows";
    const { voiceLabel, fullVoice } = describeVoice(provider, s, this.windowsVoiceName(s.speech?.voiceName || ""));
    const edge = provider === "edge" ? this.deps.edgeSnapshot() : undefined;
    const workerState = provider === "edge" ? edge!.workerState
      : provider === "windows" ? this.deps.windowsWorkerState()
      : undefined;

    let phase: TtsPhase;
    let fallbackVoice: string | undefined;
    let detail: string | undefined = edge?.detail;
    let holding: boolean | undefined;
    let activeEntry: ActiveRequest | undefined;

    if (s.muted) {
      phase = "muted";
    } else {
      let bestRank = 0;
      for (const entry of this.active.values()) {
        const rank = entry.state === "fallback" ? 3 : entry.state === "speaking" ? 2 : 1;
        if (rank >= bestRank) {
          bestRank = rank;
          activeEntry = entry;
        }
      }
      if (activeEntry) {
        if (activeEntry.state === "fallback") {
          phase = "fallback";
          fallbackVoice = activeEntry.fallbackVoice;
          detail = activeEntry.detail || detail;
        } else if (activeEntry.state === "speaking") {
          phase = "speaking";
          detail = activeEntry.detail || detail;
        } else {
          phase = "generating";
          detail = activeEntry.detail || detail;
        }
      } else if (this.hold) {
        phase = this.hold.phase;
        fallbackVoice = this.hold.fallbackVoice;
        detail = this.hold.detail || detail;
        holding = true;
      } else if (Object.hasOwn(TTS_KEYS, provider) && !this.deps.hasKey(provider)) {
        phase = "no-key";
      } else if (provider === "edge") {
        if (edge!.workerState === "ready") phase = "ready";
        else if (edge!.workerState === "error") phase = "error";
        else phase = "connecting";
      } else {
        phase = "ready";
      }
    }

    return {
      provider,
      voiceLabel,
      fullVoice,
      phase,
      ...(workerState ? { workerState } : {}),
      ...(edge ? { readySlots: edge.readySlots, totalSlots: edge.totalSlots } : {}),
      ...(fallbackVoice ? { fallbackVoice } : {}),
      ...(activeEntry?.requestId ? { requestId: activeEntry.requestId } : {}),
      ...(activeEntry?.actualVoice ? { actualVoice: activeEntry.actualVoice } : {}),
      ...(typeof activeEntry?.synthMs === "number" ? { synthMs: activeEntry.synthMs } : {}),
      ...(detail ? { detail } : {}),
      ...(holding ? { holding: true } : {}),
      updatedAt: holding && this.hold ? this.hold.updatedAt : now,
    };
  }

  private publish(explicitNow?: number): TtsStatus {
    const candidate = this.compute(explicitNow ?? Date.now());
    const { updatedAt: _ignored, ...comparable } = candidate;
    const signature = JSON.stringify(comparable);
    if (signature === this.lastSignature && this.lastSnapshot) {
      return this.lastSnapshot;
    }
    this.lastSignature = signature;
    this.lastSnapshot = candidate;
    this.deps.onBroadcast(candidate);
    return candidate;
  }
}
