// The engine's event stream (contract #2 and #3: GET /events, and GET /api/state for the state) as the two UIs read
// it: the dashboard (companion/static/dashboard.js, checked against these with // @ts-check) and the desktop app's
// renderer (session bar, activity, overlay, Memory, walking map, Status). companion/app.py dashboard_state() makes
// the state, companion/coach.py and app.py the messages, companion/voice.py the speak events.
//
// TYPE-ONLY, like ipc.ts: both builds compile it into dist/shared, and the dashboard has no build step at all.
// tests/test_engine_contract.py reads the EngineState and CoachMessage keys below (one per line) and checks them
// against a real stream (tests/golden/fixtures/dashboard_init.json): a key the engine adds is added here too.
// Keys can be added, never renamed or removed (contract #3). A key marked ? may be missing; most others can be null
// before the game connects. Keys no UI reads are typed loosely (unknown).

/** event: init, sent once to each new client: the last state and the recent messages (up to 60). Before the engine
 * publishes its first state, state is {} (every key missing): the readers treat a state without level as "waiting
 * for the game". The init's state is whole (community included). */
export interface InitEvent { state: EngineState; messages: CoachMessage[] }

/** event: state (once a second) and GET /api/state: GameState.summary() and what the UIs show around it. The state
 * event leaves out `community` (it rarely changes and is half the state): `event: community` carries it when it
 * changes, and init and /api/state have it in the state. */
export interface EngineState {
  // the character (the addon's strip)
  name: string | null;
  class: string | null;
  race: string | null;
  faction: string | null;
  bind: string | null;
  level: number | null;
  xp: number | null;
  xp_max: number | null;
  rested: number;
  hp: number | null;
  power: number | null;
  power_type: string | null;
  combat: boolean;
  dead: number;
  map: number | null;
  x: number | null;
  y: number | null;
  zone: string | null;
  subzone: string | null;
  money: number | null;
  free_bags: number | null;
  durability: number | null;
  hearth_cd: number | null;
  resting: boolean;
  mounted: boolean;
  on_taxi: boolean;
  kills: number;
  height: number | null;
  fly_to: string | null;
  taxi_to: string | null;
  target: unknown;
  // the RestedXP guide and the quest log
  guide: string | null;
  guide_group: string | null;
  step: number | null;
  steps: (GuideStep | null)[];
  quests: Record<string, QuestLogEntry>;
  coach_flags: Record<string, boolean>;
  skills: unknown;
  talent_points: number | null;
  training: unknown;
  flight_paths: unknown;
  rxp_skipped: unknown;
  rxp_settings: RxpSettings | null;
  vendor_treasure: unknown;
  consumables: unknown;
  corpse: unknown;
  res_sickness_s: number | null;
  buffs: Buff[];
  buffs_hidden: boolean;
  buffs_stale: boolean;
  addon_version: string | null;
  // errors the addon caught since its UI loaded and the last one's text (addon 0.15.3); null: none
  addon_errors: { count: number; last: string | null } | null;
  // pace
  xp_per_hour: number | null;
  ttl_seconds: number | null;
  xp_session: number;
  session_minutes: number;
  on_step_seconds: number;
  dist_to_goto: number | null;
  arrow: GuideElement | null;
  fresh: boolean;
  // places, the route and the map
  standing_in?: string | null;
  waypoint: Waypoint | null;
  addon: AddonCheck;
  upgrades: GearItem[];
  community?: Community | null;
  community_feed?: CommunityFeed | null;
  walk: WalkRoute | null;
  facing: number | null;
  map_image: boolean;
  reminders?: Reminder[];
  tuning: unknown;
  bag_items: GearItem[];
  talent_plan: TalentPick | null;
  level_history: LevelHistory | null;
  rewards: GearItem[];
  weights_profile: string | null;
  frames: number;
  versions: Versions;
  decode_rate: number;
  // the engine's jobs, the coach and the knowledge base
  refresh: Refresh | null;
  navbuild: NavBuild | null;
  models: Models;
  coach: CoachStats;
  kb: KbStatus | null;
  why?: StepWhy | null;
  pace?: Pace | null;
  overlay_step: string | null;
}

/** RestedXP's settings for the character, from the addon (page X); null until it arrives. */
export interface RxpSettings { ssf: boolean; group_quests: boolean }

/** event: message: a line of the coach's feed. The dashboard also shows its own lines in the same shape (kind
 * "question" for what you asked, "error" when it couldn't send it). */
export interface CoachMessage {
  kind: string;                   // callout, answer, banter, review, briefing, reminder, info, error
  text: string;
  speak?: boolean;
  tone?: string;                  // in character: how to deliver this line (added to the speak event's style)
  chat?: boolean;                 // an answer that keeps a chat with the coach going (the player answered an aside)
  urgent?: boolean;
  model?: string;                 // which model (or "offline", "local", "step video") said it
  attribution?: unknown;          // source-time diagnostic metadata; only the Logs view renders it
  t: number;                      // seconds since the epoch
  quests?: number[];              // a briefing's quests
  spoken?: string;                // the start already said while the reply streamed
  url?: string;                   // a step video
  reminder?: number;              // the reminder's id
  facts?: string[];               // the knowledge base's fact ids the answer cited
  facts_unknown?: string[];       // cited ids the knowledge base doesn't know
  facts_missing?: boolean;        // an answer that cited nothing
  web_sources?: WebSource[];
  tool_calls?: number;
  tools?: unknown;
}
export interface WebSource { url: string; title?: string }

/** event: community (data: Community | null): the community notes, sent when they change (and in init's state). */
export type CommunityEvent = Community | null;

/** GET /events?lite=1, the light stream for the desktop app's overlay and session bar: the same events, but its init
 * and state carry only these keys (companion/dashboard.py LITE_KEYS) and it gets no community event. */
export type LiteState = Pick<EngineState, "overlay_step" | "frames" | "level" | "zone" | "session_minutes" | "coach" |
  "models" | "refresh" | "navbuild" | "reminders" | "name" | "class" | "race" | "faction" | "guide" | "guide_group" | "step" |
  "rxp_settings">;
/** The live character and their RestedXP guide step, from the light stream (the Guides page's "Your step"). */
export type LiveGame = Pick<EngineState, "name" | "class" | "race" | "faction" | "guide" | "guide_group" | "step" | "rxp_settings">;
export interface LiteInitEvent { state: LiteState; messages: CoachMessage[] }

/** event: ptt: push-to-talk as it happens: the key is held (listening), the recording is being turned into text
 * (transcribing), or it's done (idle). Sent to every client, the light stream included. */
export interface PttEvent { state: "listening" | "transcribing" | "idle" }

/** event: thinking: active LLM generation turn ("question" | "callout" | "review"), or null when idle. */
export interface ThinkingEvent { role: string | null }

import type { SpeechMarkup } from "./speech-markup.js";

/** event: speak: a line for the desktop app's narrator. */
export interface SpeakEvent {
  text: string;
  interrupt: boolean;             // stop what is being said first
  style?: string;                 // the character the coach plays: how to perform the line (the OpenAI voice)
  tone?: string;                  // in character, how to deliver this one line (also folded into style); ElevenLabs v3 acts it
  markup?: SpeechMarkup;          // native delivery data; text remains authoritative
}

/** event: voice_spoken: a completed standalone SAPI5 turn, never a command to play it again.
 * Replayed to reconnecting clients; at is the completion time in Unix milliseconds. */
export interface VoiceSpokenEvent {
  voice: string;
  text: string;
  at: number;
  synth_ms?: number | null;       // submission to the native first-word event, not audio-byte arrival
  completion_ms?: number;        // submission to successful native completion, including playback
  first_audio_ms?: number;       // approximate submission to first observed audio delivery; present with last_audio_ms
  last_audio_ms?: number;        // approximate submission to last observed audio delivery; not playback completion
  ssml?: boolean;               // validated native markup submitted, not proof that the voice honored its effects
  ssml_document?: string;        // exact bounded document compiled by the native worker for this completed turn
}

export interface GuideElement { tag: string; map?: number; x?: number; y?: number; quest?: number; name?: string;
  where?: string; done?: boolean }
export interface GuideStep { index?: number; text?: string; elements: GuideElement[] }
export interface QuestLogEntry { done: boolean; obj: [number, number][]; name?: string | null; level?: number | null }
export interface Buff { id: string; name: string; mine: boolean; castable?: boolean; duration?: number | null;
  remaining?: number | null }
export interface Waypoint { area?: string; landmark?: string; yards?: number | null; direction?: string;
  relative?: string; text?: string }
export interface AddonCheck { expected: string | null; installed: string | null; running: string | null; ok: boolean;
  problem: string | null; fix: string | null;
  /** what is wrong when ok is false: another version installed, or still running */
  kind?: "installed" | "running" | null }
/** The versions in play (also in GET /health): the desktop app running the engine (null run on its own; the engine
 * has no version of its own), the Glide addon the engine expects, the one installed and the one the game runs, the
 * strip version the engine reads and the one the addon last sent (null before any frame), and the kb.db schema and
 * walking map builder version the engine expects. */
export interface Versions { app: string | null; addon_expected: string | null; addon_installed: string | null;
  addon_running: string | null; strip: number; strip_seen: number | null; kb_schema: number | null; nav: number | null }
export interface GearItem { item: string; slot_name?: string; replaces?: string | null; gain: number;
  verdict?: string; wait_level?: number | null }
export interface CommunityNote { kind: string; claim: string; status: string; published?: string; levels?: number[];
  sources?: { medium?: string; url?: string }[] }
export interface Community { notes: CommunityNote[]; total: number; last_scan: string | null }
/** The published community feed (companion/community_feed.py): its version, when it was last checked, and the
 * notes it brought that the player hasn't seen yet (fresh, until POST /community/seen); null in a simulated run. */
export interface CommunityFeed { version: string | null; notes: number | null; checked: string | null;
  fresh: { count: number; at: string; titles: string[]; ids?: string[] } | null; error: string | null }
export interface WalkSegment { mode: "walk" | "fly" | string; points: [number, number][]; s?: number; to?: string }
export interface WalkRoute { step?: number; map: number; segments: WalkSegment[]; through?: string[] }
export interface TalentPick { build?: string; done?: boolean; talent?: string; rank?: number; level?: number;
  due?: boolean; matches_forever?: boolean }
export interface LevelTime { level: number; minutes: number; best_minutes?: number | null; best_character?: string;
  deaths?: number }
export interface LevelHistory { levels: LevelTime[]; best_for_current_level?: { minutes: number; character: string } | null }
export interface Reminder { id: number; what: string; when: string; every_min: number | null }
/** The quest database and knowledge base re-read (Settings > Re-read, the Status page). */
export interface Refresh { state: "waiting" | "maps" | "glide.db" | "kb.db" | "done"; force: boolean;
  started: number | null; finished: number | null; seconds: number | null;
  maps?: string; glide: string | null; kb: string | null }
/** The walking map build (navmesh), per continent. */
export interface NavBuild { state: "waiting" | "checking" | "building" | "done" | "failed"; map: number | null;
  tile: number; tiles: number; maps_done: number; started: number | null; finished: number | null; force: boolean;
  outdated: number[]; built: number[] }
export interface Models { fast: string | null; deep: string | null; fast_name: string | null; deep_name: string | null;
  fast_reasoning?: string | null; deep_reasoning?: string | null; voice_question?: "fast" | "deep" | null;
  listen: string; listen_fallback: string | null;
  translation?: { language: string; available: boolean } }
export interface CoachStats { mode: string; calls: number; errors: number; in_tokens: number; out_tokens: number;
  tool_calls: number; cited: number; unsupported: number; uncached_in_tokens: number; cache_read_tokens: number;
  cache_write_tokens: number; cost_usd: number; web_searches?: number;
  thinking?: string | null; link_issue?: string | null; link_status?: "warn" | "error" | null }
export interface KbStatus { ok: boolean; reason?: string; guides?: number }
/** The step brief: why the route does this step now (knowledge/api; GameState and the knowledge pack). */
export interface StepWhy { n?: number; kind?: string; unavailable?: string; stale?: boolean; xp_now?: number;
  unlocks_xp?: number; time_s?: number; xp_per_min?: number; why?: string[]; why_text?: Record<string, string>;
  quests?: WhyQuest[]; enables?: { quest: string; step?: number }[]; rxp_notes?: string[]; co_located?: string[];
  alternatives?: WhyAlternative[]; risks?: WhyRisk[]; live?: string[];
  plan?: { exp_level?: number; level_now?: number; delta?: number };
  next?: { n: number; kind?: string; xp?: number; quests?: string[] } }
export interface WhyQuest { name: string; role?: string; xp_at_level?: number; xp_your_level?: number | null;
  unlocks_xp?: number; decay_at_level?: number; turnin_step?: number | null; turnin_guide?: string }
export interface WhyAlternative { name: string; kind?: string; xp?: number | null; xp_per_min?: number | null;
  why_not?: string[] }
export interface WhyRisk { name: string; level?: number | string; elite?: boolean; dist_yd?: number | null; note?: string }
export interface Pace { xp_per_hour?: number; benchmark_xph?: number; ratio?: number; xp_to_level?: number;
  your_eta_s?: number; eta_next_level_s?: number; pending_turnin_xp?: number }
