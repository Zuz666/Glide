// Every setting the Settings page shows. `key` is a path in config.json ("companion.llm.min_interval", "wow_dir"), or
// in the app's own settings when it starts with "app:" ("app:overlay.opacity"). `def` is the default shown when the
// value isn't set. An engine setting ("companion.…") gets its default, range and restart flag from the engine's
// companion/settings_manifest.json (settings-manifest.ts, generated at build), so here it has only its label, help
// and place; the app's own settings carry theirs here.
import { MANIFEST } from "./settings-manifest.js";
import { LANGUAGE_OPTIONS } from "./speech-language.js";

export type FieldType = "bool" | "number" | "range" | "text" | "select" | "list" | "folder";

export interface Field {
  key: string;
  label: string;
  type: FieldType;
  def?: unknown;
  help?: string;
  min?: number;
  max?: number;
  step?: number;
  options?: [string, string][];   // [value, label]
  wide?: boolean;
  restart?: boolean;              // only takes effect after a coach restart (most config.json values)
  invert?: boolean;               // a bool shown positively but stored as its negation ("Push-to-talk" over noPtt)
}

export interface Group { title: string; intro?: string; fields?: Field[]; special?: string }
export interface Tab { id: string; title: string; groups: Group[] }

const L = "companion.llm.";

export const SCHEMA: Tab[] = [
  {
    id: "general", title: "General", groups: [
      { title: "App", fields: [
        { key: "app:autoStartEngine", label: "Start the coach when the app opens", type: "bool", def: true },
        { key: "app:autoUpdate", label: "Check for updates", type: "bool", def: true,
          help: "Glide checks GitHub for a newer version every 30 minutes and tells you when there is one. Download it from the footer; nothing installs until you click Restart to update. Your data and settings are kept." },
        { key: "app:updateVoice", label: "Say when an update is out", type: "bool", def: true,
          help: "The coach says so out loud, and reminds you every 10 minutes until you update. Off: only the footer button and a note." },
        { key: "app:closeToTray", label: "Keep Glide running in the tray when the window is closed", type: "bool", def: false,
          help: "Off: closing the window quits Glide, the coach included. On: the window only hides, and the overlay, voice and push-to-talk keep working; quit from the tray icon." },
      ] },
      { title: "Coach process", fields: [
        { key: "app:engineArgs.sim", label: "Simulation mode", type: "bool", def: false, restart: true,
          help: "A simulated Skyborne Alliance warrior levels through your RestedXP guides. No game needed. Nothing is saved." },
        { key: "app:engineArgs.noPtt", label: "Push-to-talk", type: "bool", def: true, invert: true, restart: true,
          help: "Hold the push-to-talk key (Voice settings) to ask the coach out loud." },
        { key: "app:engineArgs.noArchive", label: "Archive SavedVariables and the quest cache", type: "bool", def: true, invert: true, restart: true,
          help: "Copies them into data\\archive whenever they change, because the beta client doesn't read them back. Your recordings depend on it." },
        { key: "app:engineArgs.noKb", label: "Rebuild the knowledge base in the background", type: "bool", def: true, invert: true, restart: true,
          help: "Merges new recordings and RestedXP updates while you play, then switches over without a restart." },
      ] },
      { title: "First-run setup", special: "setup-again" },
      { title: "Walking map", special: "walk-map" },
      { title: "Data", special: "data-home" },
    ],
  },
  {
    id: "models", title: "AI models & keys", groups: [
      { title: "API keys", special: "keys",
        intro: "Keys are encrypted with your Windows account and passed to the coach when it starts. They are never written to config.json. Changes apply on the next coach restart." },
      { title: "Choose models", special: "model-picker",
        intro: "Every model your keys and endpoints can use, cheapest first within each provider, with what it would cost an hour at your usage. Choose reasoning effort separately for each role; changes apply to the next call." },
      { title: "Model ids", intro: "Or type an id: claude-… (Anthropic), gemini:<id> (Gemini API), vendor/model (OpenRouter), ep:<endpoint>:<model> (custom endpoint).",
        fields: [
          { key: L + "fast_model", label: "Fast model", type: "text", wide: true,
            help: "Live callouts, spoken questions and quest briefings." },
          { key: L + "fast_reasoning_effort", label: "Fast reasoning effort", type: "select",
            options: [["auto", "Auto"]], help: "Supported levels for the fast model above. Save to apply." },
          { key: L + "deep_model", label: "Deep model", type: "text", wide: true,
            help: "Typed questions and pace reviews." },
          { key: L + "deep_reasoning_effort", label: "Deep reasoning effort", type: "select",
            options: [["auto", "Auto"]], help: "Supported levels for the deep model above. Save to apply." },
          { key: L + "voice_question_model", label: "Spoken questions use", type: "select",
            options: [["fast", "the fast model"], ["deep", "the deep model"]] },
          { key: L + "enabled", label: "AI coach on", type: "bool",
            help: "Off: only simple rule-based callouts, with no API calls." },
        ] },
      { title: "Custom endpoints", special: "endpoints",
        intro: "Any OpenAI-compatible server: a local Ollama (http://127.0.0.1:11434/v1) or LM Studio (http://127.0.0.1:1234/v1), OpenAI, Groq, Mistral, DeepSeek, Together… Their models join the list above as ep:<id>:<model>." },
      { title: "Requests", fields: [
        { key: L + "timeout", label: "Request timeout (s)", type: "number" },
        { key: L + "busy_retry_s", label: "Retry a busy model after (s)", type: "number", step: 0.5 },
        { key: L + "tune_thinking", label: "Automatic thinking tuning", type: "bool",
          help: "When reasoning effort is Auto, keep fast Claude and Gemini calls lightweight. Explicit effort selections take priority." },
      ] },
      { title: "Prompt caching", fields: [
        { key: L + "prompt_cache", label: "Prompt caching", type: "bool",
          help: "Repeated prompt prefixes (tools, rules, glossary) are read from cache at a tenth of the price or less." },
        { key: L + "cache_ttl_fast", label: "Fast model cache lifetime", type: "select", options: [["5m", "5 minutes"], ["1h", "1 hour"]] },
        { key: L + "cache_ttl_deep", label: "Deep model cache lifetime", type: "select", options: [["5m", "5 minutes"], ["1h", "1 hour"]] },
        { key: L + "callout_tools", label: "Send tool definitions with callouts", type: "bool",
          help: "Callouts and spoken questions then share one cached prefix." },
      ] },
    ],
  },
  {
    id: "coach", title: "Coaching", groups: [
      { title: "Callouts", fields: [
        { key: L + "min_interval", label: "Minimum seconds between callouts", type: "number",
          help: "Deaths and low health go out right away." },
        { key: L + "callout_words", label: "Callout length (words)", type: "number" },
        { key: L + "settle_s", label: "Quest burst: wait for quiet (s)", type: "number", step: 0.5,
          help: "Turning in or picking up several quests at once: the coach waits until nothing new came for this long, then sums it all up in one line." },
        { key: L + "settle_max_s", label: "Quest burst: wait at most (s)", type: "number" },
        { key: L + "summary_words", label: "Quest burst summary length (words)", type: "number" },
        { key: L + "answer_words", label: "Spoken answer length (words)", type: "number" },
        { key: L + "typed_answer_words", label: "Typed answer length (words)", type: "number" },
        { key: L + "review_words", label: "Pace review length (words)", type: "number" },
        { key: L + "speak_reviews", label: "Speak pace reviews", type: "bool" },
        { key: "companion.rules.review_minutes", label: "Pace review every (minutes)", type: "number" },
        { key: "companion.rules.stuck_minutes", label: "Stuck callout after (minutes)", type: "number" },
      ] },
      { title: "Roleplay", intro: "Ask the coach to play anyone (\"talk like a pirate\", \"be my drill sergeant\", \"you're Thrall now\") and it stays in character until you tell it to drop the act. It writes these itself; you can edit them here.", fields: [
        { key: L + "persona", label: "Character", type: "text", wide: true,
          help: "Who the coach plays, and how they talk. Empty: the coach's own plain voice." },
        { key: L + "persona_voice", label: "Voice direction", type: "text", wide: true,
          help: "How the character sounds out loud: accent, pace, energy. Performed by the OpenAI voice (gpt-4o-mini-tts)." },
        { key: L + "banter", label: "Speak up unprompted", type: "bool",
          help: "In character, the coach says something now and then on its own: a quip, a callback, a question about you. It learns what lands with you and grows closer the more you play together." },
        { key: L + "banter_minutes", label: "About every (minutes)", type: "number",
          help: "Or just tell it: \"talk less\", \"more\", \"stop the chatter\"." },
        { key: L + "banter_words", label: "Aside length (words)", type: "number" },
        { key: L + "banter_reply_words", label: "Chat reply length (words)", type: "number",
          help: "Answer an aside and the coach keeps the conversation going, for as long as you keep answering." },
        { key: L + "recall_meaning", label: "Remember by meaning", type: "text",
          help: "Memories come back for what you mean, not only the words you use (\"the big gnoll that wrecked us\" finds the Hogger story). off; openai (your OpenAI key); gemini (your Gemini key); or ep:<endpoint id> for a local Ollama or LM Studio, free. Each memory is embedded once." },
        { key: L + "recall_embed_model", label: "Embedding model", type: "text",
          help: "Empty: text-embedding-3-small (OpenAI), gemini-embedding-001 (Gemini). A local endpoint needs its model, e.g. nomic-embed-text." },
        { key: L + "recall_min_similarity", label: "How close in meaning", type: "number", step: 0.05,
          help: "0-1: higher brings back only memories very close to the moment." },
        { key: L + "persona_word_factor", label: "Extra room for the act", type: "number", step: 0.1,
          help: "In character, lines may be this many times longer than the lengths above." },
      ] },
      { title: "Quest briefings and step videos", fields: [
        { key: L + "quest_briefings", label: "Quest briefings", type: "bool",
          help: "When you accept a quest, the coach reads RestedXP's instructions for it and explains how to do it." },
        { key: L + "briefing_words", label: "Briefing length (words)", type: "number" },
        { key: L + "briefing_max_quests", label: "Quests read in full per briefing", type: "number" },
        { key: L + "briefing_delay_s", label: "Wait before briefing (s)", type: "number", step: 0.5,
          help: "Quests accepted together within this time get one briefing." },
        { key: L + "video_tips", label: "Say what RestedXP step videos show", type: "bool",
          help: "Steps with a YouTube example clip: the coach says what the clip shows (where to go, what to do), with a link to the clip in the chat." },
        { key: L + "step_videos", label: "Open RestedXP step videos", type: "bool",
          help: "Steps with a YouTube example clip open it in a new browser window (never in combat, on a taxi or dead)." },
        { key: L + "video_browser", label: "Browser for step videos", type: "text", wide: true,
          help: "Path to a browser .exe. Empty: the default browser." },
      ] },
      { title: "Question context", fields: [
        { key: L + "voice_lookahead_steps", label: "Route steps ahead, spoken questions", type: "range" },
        { key: L + "lookahead_steps", label: "Route steps ahead, typed questions and reviews", type: "range" },
        { key: L + "conversation_turns", label: "Past questions included", type: "range" },
        { key: L + "conversation_minutes", label: "…from the last (minutes)", type: "range" },
        { key: L + "voice_tool_calls", label: "Tool calls per spoken question", type: "number" },
        { key: L + "voice_tool_seconds", label: "Time limit, spoken question (s)", type: "number" },
        { key: L + "tool_calls", label: "Tool calls per typed question", type: "number" },
        { key: L + "tool_seconds", label: "Time limit, typed question (s)", type: "number" },
      ] },
      { title: "Web search", fields: [
        { key: L + "web_search", label: "Web search for typed questions", type: "bool" },
        { key: L + "web_search_voice", label: "Web search for spoken questions", type: "bool", help: "A search adds a few seconds." },
        { key: L + "web_search_max", label: "Searches per question", type: "number" },
      ] },
      { title: "Reminders", fields: [
        { key: L + "reminder_near_yd", label: "\"Near an NPC\" distance (yards)", type: "number" },
        { key: L + "reminder_check_s", label: "Check reminders every (s)", type: "number" },
      ] },
      { title: "Community notes", fields: [
        { key: "companion.community.feed", label: "Get new community notes as they're published", type: "bool",
          help: "What the WoW Forever community finds (patches, XP findings, route tricks) reaches the coach without an app update." },
        { key: "companion.community.feed_minutes", label: "Check for new notes every (minutes)", type: "number" },
      ] },
    ],
  },
  {
    id: "buffs", title: "Buffs & gear", groups: [
      { title: "Buff reminders", fields: [
        { key: "companion.rules.buffs.enabled", label: "Buff reminders", type: "bool" },
        { key: "companion.rules.buffs.min_duration", label: "Only buffs lasting at least (s)", type: "number" },
        { key: "companion.rules.buffs.warn_before", label: "Warn before expiry (s)", type: "number" },
        { key: "companion.rules.buffs.drop_grace", label: "Missing for (s) before \"Rebuff\"", type: "number" },
        { key: "companion.rules.buffs.remind_others", label: "Also buffs other players cast on you", type: "bool" },
        { key: "companion.rules.buffs.ignore", label: "Ignore these buffs", type: "list", help: "One name per line." },
      ] },
      { title: "Gear upgrades", fields: [
        { key: "companion.gear.kind", label: "Stat weights", type: "select", options: [["Speedrun", "Speedrun"], ["Hardcore", "Hardcore"]] },
        { key: "companion.gear.spec", label: "Spec", type: "text",
          help: "e.g. Enhancement, Frost, Arms. Empty: your class's first profile." },
        { key: "companion.gear.min_gain_pct", label: "Minimum gain to call an upgrade (%)", type: "number", step: 0.5 },
      ] },
      { title: "RestedXP route", intro: "These describe how you play, so the route analysis matches.", fields: [
        { key: "companion.rxp.ssf", label: "Solo self-found", type: "bool" },
        { key: "companion.rxp.group_quests", label: "Do group quests", type: "bool" },
        { key: "companion.rxp.hardcore", label: "Hardcore", type: "bool" },
        { key: "companion.rxp.dungeons", label: "Dungeon runs in the plan", type: "bool" },
      ] },
    ],
  },
  {
    id: "voice", title: "Voice", groups: [
      { title: "Voice output", fields: [
        { key: "companion.voice.enabled", label: "Speak callouts and answers", type: "bool" },
        { key: "companion.voice.output", label: "Spoken by", type: "select",
          options: [["app", "this app (Windows or online voices, below)"], ["windows", "the coach's own Windows speech process"]] },
        { key: "companion.voice.announce", label: "Announce readiness at startup", type: "bool" },
      ] },
      { title: "Coach voice", special: "voices" },
      { title: "Windows speech process", intro: "Only used when \"Spoken by\" is the coach's own process.", fields: [
        { key: "companion.voice.voice", label: "Voice name", type: "text", help: "e.g. Microsoft Zira Desktop. Empty: the Windows default." },
        { key: "companion.voice.rate", label: "Rate", type: "range" },
        { key: "companion.voice.volume", label: "Volume", type: "range" },
      ] },
      { title: "Push-to-talk", fields: [
        { key: "companion.ptt.enabled", label: "Push-to-talk", type: "bool" },
        { key: "companion.ptt.key", label: "Key", type: "text",
          help: "F8, MOUSE4, MOUSE5, CTRL+Q… WoW sees the key too, so pick one you haven't bound in game." },
        { key: "companion.ptt.engine", label: "Speech recognition", type: "select",
          options: [["auto", "auto (Whisper, else Windows)"], ["whisper", "Whisper (offline, accurate, bundled)"], ["windows", "Windows built-in (offline)"],
                    ["openai", "OpenAI (online, OPENAI_API_KEY)"], ["groq", "Groq Whisper large v3 turbo (online, fastest, GROQ_API_KEY)"], ["deepgram", "Deepgram nova-3 (online, DEEPGRAM_API_KEY)"]],
          help: "Online engines fall back to the offline one when they fail." },
        { key: "companion.ptt.online_model", label: "Online recognition model", type: "text",
          help: "Empty: gpt-4o-mini-transcribe (OpenAI), whisper-large-v3-turbo (Groq), nova-3 (Deepgram)." },
        { key: "companion.ptt.whisper_model", label: "Whisper model", type: "select",
          help: "Non-English speech needs multilingual small, not an English-only .en model. Downloads when needed; offline recognition is unavailable until the model is ready. Online recognition can still work; Windows fallback needs a recognizer for the selected language.",
          options: [["tiny.en", "tiny.en: fastest, 74 MB"], ["base.en", "base.en: fast, 141 MB"], ["distil-small.en", "distil-small.en: 320 MB"],
                    ["small.en", "small.en: recommended, 464 MB"], ["distil-medium.en", "distil-medium.en: 755 MB"],
                    ["medium.en", "medium.en: most accurate, 1.4 GB"], ["small", "small: multilingual, 464 MB"]] },
        { key: "companion.ptt.only_when_wow_focused", label: "Only while WoW is focused", type: "bool" },
        { key: "companion.ptt.beep", label: "Beep on start and stop", type: "bool" },
        { key: "companion.ptt.min_seconds", label: "Ignore presses shorter than (s)", type: "number", step: 0.05 },
      ] },
      { title: "Offline speech recognition (Whisper)", special: "speech-install" },
    ],
  },
  {
    id: "overlay", title: "Overlay", groups: [
      { title: "Overlay", intro: "A click-through line over the game: the current step, and the last thing the coach said. Hold Alt (the drag key) with the mouse over it: drag moves it, the mouse wheel changes its width, right-click collapses it.", fields: [
        { key: "companion.overlay.drag_key", label: "Drag key", type: "select", options: [["ALT", "Alt"], ["SHIFT", "Shift"], ["CTRL", "Ctrl"]] },
        { key: "app:overlay.enabled", label: "Show the overlay", type: "bool", def: false, help: "Off by default. Also in the tray menu." },
        { key: "app:overlay.opacity", label: "Background opacity", type: "range", def: 0.85, min: 0.2, max: 1, step: 0.05 },
        { key: "app:overlay.fontSize", label: "Font size", type: "range", def: 13, min: 9, max: 24 },
        { key: "app:overlay.collapsed", label: "Step line only", type: "bool", def: false },
      ] },
      { title: "Position", special: "overlay-unlock" },
    ],
  },
  {
    id: "capture", title: "Game capture", groups: [
      { title: "Check the capture", special: "capture-check" },
      { title: "Data strip", intro: "The addon draws a strip of coloured cells in the top-left corner of the game window, and the coach reads it. Set WoW to Windowed or Windowed (Fullscreen); exclusive fullscreen can capture as black.", fields: [
        { key: "companion.capture.window_title", label: "WoW window title", type: "text" },
        { key: "companion.capture.fps", label: "Reads per second", type: "number" },
        { key: "companion.capture.width", label: "Smallest capture width (px)", type: "number",
          help: "The capture grows by itself to fit the strip (a bigger /glide cell, for example); this is the least it reads." },
        { key: "companion.capture.height", label: "Smallest capture height (px)", type: "number" },
      ] },
    ],
  },
  {
    id: "language", title: "Language", groups: [
      { title: "Coach language", fields: [
        { key: "companion.voice.language", label: "Coach and speech language", type: "select", options: LANGUAGE_OPTIONS,
          help: "AI-generated text and translated messages use this language when an LLM is available; otherwise text stays English. Also controls recognition and voice filtering. Save and restart the coach. Language support varies by speech provider." },
      ] },
      { title: "Language tools and models", special: "language" },
    ],
  },
];

// the engine's settings: default, range and "needs a restart" from the manifest
for (const f of SCHEMA.flatMap((t) => t.groups.flatMap((g) => g.fields || []))) {
  const m = f.key.startsWith("companion.") ? MANIFEST[f.key.slice("companion.".length)] : undefined;
  if (!m) continue;
  f.def = m.default;
  if (m.min !== undefined) f.min = m.min;
  if (m.max !== undefined) f.max = m.max;
  if (!m.live && !m.app) f.restart = true;
}
