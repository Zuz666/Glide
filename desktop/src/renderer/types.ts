// What the main process sends the window (IPC answers and events) and what the engine streams, as the window reads
// them. They are defined once, in desktop/src/shared (type-only files both builds compile), and re-exported here.

export type {
  AddonStatus, AppInfo, AppSettings, AppSettingsPatch, EngineRunState, EngineStatus, Footprint, KeyGuide, KeyStatus, KeyTest, KnownKey,
  LogAttribution, LogLine, Obj, SpeechSettings, SpeechStatus, TtsPhase, TtsPlaybackReport, TtsPlaybackState, TtsProvider, TtsStatus,
  UpdateStatus, Voice, WhisperModel, WowClient,
} from "../shared/ipc.js";
export type {
  CoachMessage, CoachStats, EngineState, InitEvent, LiteInitEvent, LiteState, LiveGame, Models, NavBuild, Refresh, Reminder,
  PttEvent, SpeakEvent, ThinkingEvent, VoiceSpokenEvent,
} from "../shared/engine.js";
