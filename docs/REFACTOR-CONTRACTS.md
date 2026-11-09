# Refactor contracts

The refactor (branch `refactor/cohesion`) may restructure anything inside a part of Glide. It may not change what
crosses between parts, or what a player sees, without a deliberate decision recorded here. The golden masters in
`tests/golden/` (`python tests/golden/golden.py check`) catch behaviour changes; this file names the interfaces
they don't spell out.

## The parts

| Part | Code | Language |
|---|---|---|
| Addon | `addon/Glide/` | Lua 5.1 (WoW API) |
| Engine | `companion/` (minus `static/`) | Python 3.12 |
| Dashboard | `companion/static/` | HTML, CSS, browser JS |
| Knowledge and tools | `knowledge/`, `tools/`, `wowfiles/` | Python 3.12 |
| Desktop app | `desktop/src/` | TypeScript (Electron) |

## Interfaces between parts (frozen)

1. **The strip** (addon → engine). The pixel grid, its markers (magenta, then yellow for the binary strip, cyan
   for the old 6-bit one), the framing `[version][seq][len][payload][fletcher16]`, the payload `core|page` with
   `key=value;` pairs, every core key and page id (`pg=B`, `Z`, `G`, `S0..2`, `Q*`, `A*`, `W*`, `I*`, `R*`, ...),
   the ring events `id:kind:arg`, and the gauges under the grid. The full list is `docs/STRIP.md`, generated from
   `companion/strip_spec.py`: that is this interface's definition. `companion/protocol.py` and
   `addon/Glide/Codec.lua` must stay twins (framing, Fletcher-16, symbols; State.lua `ns.Hash` and
   `protocol.list_hash`; Coach.lua's flag bits and the registry's). Pinned by `tests/test_link.py`,
   `tests/test_strip_spec.py` and the addon and engine golden masters.
2. **Engine HTTP** (engine → dashboard and desktop app), on `127.0.0.1`:
   `/`, `/events` (SSE: `init`, `state`, `message`, `speak`), `/api/state`, `/ask`, `/settings`, `/models`,
   `/memory`, `/reminders`, `/layout`, `/health`, `/refresh`, `/calibrate`, `/mute`, `/xph`,
   `/fonts/<name>.woff2`, `/map/<uiMapID>.png`, `/icon/<spellID>.png`. Request and response shapes stay as they are.
3. **The dashboard state** (the `state` event and `/api/state`): every key the dashboard or the desktop app reads
   (`companion/static/dashboard.js`; in the app `desktop/src/renderer/session-bar.ts`, `overlay.ts`, `activity.ts`,
   `memory.ts`, `status.ts`, `settings/walk-map.ts` and `coach.ts`): `models`, `coach`, `refresh`, `navbuild`,
   `reminders`, `why`, `pace`, `steps`, `waypoint`, `walk`, `buffs`, `quests`, `addon`, `overlay_step`, ... The
   shapes are typed in `desktop/src/shared/engine.ts` (`EngineState`, `CoachMessage`, `SpeakEvent`), which both UIs
   are checked against. Keys can be added, not renamed or removed. Messages keep `kind`, `text`, `speak`, `urgent`,
   `url`, `model`, `facts`, `facts_unknown`, `web_sources`, `t`.
4. **Desktop IPC** (main ↔ renderer, `desktop/src/main/preload.ts`): the channel names (`engine:*`, `config:*`,
   `keys:*`, `speech:*`, `tts:*`, `update:*`, `addon:*`, `overlay:*`, `app:*`, `res:*`, `shell:open`,
   `dialog:folder`, `data:import`, `endpoint:test`) and their arguments.
5. **Files on the player's disk**, read across versions: `config.json` (engine settings; keys as in
   `companion/app.py` `DEFAULTS` and `desktop/src/renderer/settings-schema.ts`), `app-settings.json` (desktop),
   `secrets.json` (DPAPI-encrypted keys), `data/live/*` (logs, layout, reminders, memory, xph history),
   `data/gamedata/*` (manifest, maps, spell_icons.json, target_icons.json, icons/), `GlideLinkDB` and `GlideRecorderDB`
   (addon SavedVariables). New fields are fine; existing ones keep their meaning, and old files must still load.
   **Who writes `config.json`** (R20): the engine owns it. In Python it is read and written only through
   `companion/glideconfig.py` (the engine, `knowledge/store.py`, `knowledge/eval/run_eval.py` and the tools
   `build_db.py`, `build_vendor_treasures.py`, `extract_maps.py`); a write re-reads the file, merges, and replaces it
   atomically, one writer at a time. While the engine runs, the desktop app saves through it:
   `POST /settings {"save": <patch>}` (the same patch `config:set` takes; `null` removes a key), and reads the file
   (`readConfig()` / `companionSection()`, read-only). The app writes `config.json` itself only when no engine is
   running: the WoW folder found at the first start (`findWowOnce`, before the engine starts), `config:set` while
   the coach is stopped or starting (Settings, the WoW folder on the Addon page, custom endpoints), and
   `data:import`, which copies another folder's `config.json` over this one. `companion.overlay.{enabled,x,y,width,alpha,
   font_size}` belong to the standalone Python overlay (`companion/overlay.py`, run without the app); the app's
   overlay uses `app-settings.json` `overlay.{opacity,fontSize,...}` and reads only `companion.overlay.drag_key`
   from `config.json` (both overlays use it).
6. **The databases**: `data/glide.db` and `data/kb.db` table and column names (51 kb tables) as read by the engine
   and written by `tools/` and `knowledge/build_kb.py`. A schema change means a rebuild for every player, so it is
   out of scope for the refactor. With a kb.db, glide.db is the knowledge build's input: at runtime the engine reads
   kb.db (through `knowledge.api`), and glide.db only when there is no knowledge pack and in the simulator (`--sim`).
7. **Command lines**: `companion/app.py` flags (`--app`, `--sim`, `--port`, `--no-voice`, ...), `tools/*.py`
   stages and flags, the addon's slash commands (`/glide ...`, `/coach ...`, `/glidelink ...`) and key bindings.
8. **What the player sees**: coach wording, dashboard and app texts, addon texts. Unchanged unless a step is
   explicitly about wording (the golden masters will flag every changed string).

## Rules for the refactor

- Each step keeps `python tests/run_all.py` (API keys blanked) and `python tests/golden/golden.py check` green, and
  builds the desktop app (`npm run build` in `desktop/`). One concern per commit.
- A golden difference is either a bug in the step (fix it) or an intended change (record it here first, then
  re-record that probe: `golden.py record <probe>`).
- No new runtime dependencies. No behaviour, performance or data-format "improvements" smuggled into a structural
  change: those are separate, explicit commits.
- Match the language's established idioms; keep comment density and voice as they are (comments say why).

## Recorded changes

- **Windows speech settings link:** `shell:open` now allows exactly `ms-settings:speech` on Windows, in addition to its existing web-page and local-directory targets. Other URI schemes, other Settings pages and parameterized variants remain rejected. The Language help popup uses this fixed destination to open Windows voice management ([Microsoft URI reference](https://learn.microsoft.com/en-us/windows/apps/develop/launch/launch-settings)).

- **Translation status and community WIP:** the optional `models.translation` status contains `{language, available}`, computed from the running coach's language and fast-model eligibility (enabled, key, suspension, backoff and configured provider). The footer uses it rather than unsaved settings; it makes no model requests. News/community translation is a disabled UI-only WIP control, with no persisted setting or translation implementation.

- **Multilingual coach and speech:** `companion.voice.language` selects the coach/recognition language; non-English selections migrate English-only Whisper weights to multilingual `small`. Edge-TTS uses its live voice catalogue, with a language filter and no named voice default. Authenticated `POST /localize` accepts `{text}` and returns `{ok, text, language}`; unavailable translation preserves English. Translated messages may carry `original_text`; generated answers are marked to avoid duplicate translation. The named startup resource is `voice.ready`. Interface language remains English (WIP). Recognition/localization tests cover language boundaries, fallback and delivery; the desktop golden retains IPC/settings behavior but no longer copies internal exports or UI wording.

- **R1, knowledge golden** (`knowledge` probe re-recorded): stored step briefs are built with RestedXP's SSF and
  group quests off (meta `step_brief_rxp`); a player with other RestedXP options now gets a brief computed for
  their own route instead of the stored one, and every route and knowledge-pack cache keys on those options. The
  probe's players use the default options (SSF and group quests on), so 15 of its 117 samples show the planned
  values (expected level, XP per minute, time) of their own route. No resolved step or error changed. The KB build
  now also writes meta `step_brief_rxp` (no table or column change); an older kb.db without it counts as built
  with the same options.
- **R3, addon 0.15.1: list pages leave room for their header.** Extras.lua chunked list pages for an
  `n=;h=;<key>=` header only; the training list (page P) also carries `pl=;k=;u=`, so from about 15 missing spells
  its pages were cut, never matched their hash, and the companion never took the training list. Chunks are now
  sized for the real header (the wire format is unchanged; a list may spread over one more page). Pages G, S0..2
  and K now fit their last field to the room left instead of being cut by RefreshPages (G and S: the same text the
  cut gave, give or take a byte; K: whole skill ranks only). `tests/test_page_sizes.py` checks every page at its
  longest. Addon golden re-recorded: only the version in the payload (`gv=0.15.1`) and the strip paint it changes.
- **R9, desktop probe**: `main/store.js` exports `readJsonFile` (a JSON file's object, and why it couldn't be read).
  The app no longer saves over a `config.json` it can't parse (it keeps a `config.json.<time>.bad` copy and shows
  why); the engine starts on its defaults instead of stopping with a traceback. Old configs load as before.
- **R7, desktop probe (Settings texts and defaults, contract 8)**: "Wait before briefing" shows 6 s, the engine's own
  default (it showed 2.5; the engine always waited 6). "Strip columns" / "Strip rows" ("Must match the addon") are
  gone from Settings > Game capture: the addon has no matching setting. The engine still reads `companion.grid`
  from an old config.json (contract 5). Save now sends `video_tips` to the running coach like the other toggles.
- **R4, contract 2 (engine HTTP): a Host check and a per-run token.** Every request must carry
  `Host: 127.0.0.1:<port>` or `localhost:<port>` (else 403), so a rebound DNS name can't reach the engine. Every
  POST, `GET /api/state` and `GET /memory` must also carry `X-Glide-Token: <token>` (else 403), so a web page open
  in a browser can't drive the coach (a cross-site form or no-cors POST can't send that header). The desktop app
  makes a new token per engine start and passes it in the env var `GLIDE_TOKEN`; run on its own, the engine makes
  one. The engine puts it in the page it serves (`<meta name="glide-token">`), so the dashboard sends it too.
  `serve(..., token=None)` (the tests) asks for none. Contract 4, additive: `engine:get` and `engine:state` carry
  `token`.
- **R6, contract 8 (what the player hears): "Glide voice ready."** With voice output "app" (the default) it is now
  said when the app's windows connect (about 1.5 s after the first event-stream client), honouring the `announce`
  setting; before, it was published before anyone listened and never heard.
- **R6, contract 8 (dashboard text): a question the engine didn't take** shows "Problem: The coach didn't get that
  question; ask again." in the chat instead of failing silently.
- **R14, documentation only (contract 3)**: the state's readers in the desktop app are `session-bar.ts`,
  `overlay.ts`, `activity.ts`, `memory.ts`, `status.ts`, `settings/walk-map.ts` and `coach.ts` (not `app.ts`), and
  the dashboard also reads the messages' `facts_unknown` and `web_sources`; contract 3 now says so. Nothing the
  engine sends changed. The stream is typed in `desktop/src/shared/engine.ts`; `dashboard.js` is checked against it
  with `// @ts-check` (`desktop/tsconfig.dashboard.json`, run by `npm run build`; JSDoc only, still no build step),
  and `tests/test_engine_contract.py` checks the types against `tests/golden/fixtures/dashboard_init.json`.
- **R15, no interface change (contract 4)**: the IPC channels, their arguments and events are typed once in
  `desktop/src/shared/ipc.ts` (type-only) and used by `main/app/ipc.ts`, `main/preload.ts` and the renderer; both
  tsconfigs now compile `src` into `dist` (output paths unchanged: `dist/main`, `dist/renderer`, plus a type-only
  `dist/shared` nothing loads). `tests/test_ipc_contract.py` checks the map, the preload lists and the handlers agree.
- **R12, contract 1 written down.** `docs/STRIP.md` (generated from `companion/strip_spec.py`) now lists every core
  key, page and page key, ring event kind and coach flag bit, with its type, meaning, the addon module that sets it
  and the version that brought it; it is the definition of contract 1. `tests/test_strip_spec.py` checks that the
  real addon sends only registered keys and that the engine and `knowledge/sources/live_logs.py` read only
  registered ones. "Twins" in contract 1 no longer covers the old 6-bit colour functions: `ns.SymbolColor`,
  `ns.LEVELS` and `ns.Fletcher16` are gone from Codec.lua (only the deleted `addon/_old` used them); the engine
  still reads the old cyan strip. The list hash lives once on the engine side (`protocol.list_hash`). No wire change.
- **R12 follow-up, addon 0.15.2: gear entries fit their page.** A worn, bag or reward entry with many stats could
  outgrow its page, get cut and keep the whole list from ever being taken. An entry now keeps the stats that fit,
  dropping the least useful first (school-only spell damage, defensive ratings, regen, spell hit/crit; weapon dps and
  speed, armor and the main stats stay); vendor treasure entries are fitted to page D the same way instead of being
  left out. Same format; ordinary items are unchanged. Addon golden re-recorded: only the version in the payload
  (`gv=0.15.2`) and the strip paint it changes.
- **R38, addon 0.15.2: luacheck fixes.** The recorder's flight-master scan now keeps the y coordinate of each node
  (it was always nil); the rest are unused locals. Addon golden re-recorded: only the version in the payload
  (`gv=0.15.2`), the window's version line and the strip paint change.
- **R35, desktop golden: app/ modules and the IPC channels (contract 4) pinned.** The probe now walks dist/main
  recursively (app/*.js was unpinned; only modules that still have a .ts source count, since tsc leaves stale
  output behind), and records every channel app/ipc.js registers and every event the main process sends. The probe
  fails unless they equal preload.ts's INVOKE and EVENTS. Re-recorded: new keys only (`main/app/*`, `ipc`).
- **R35, knowledge golden: other RestedXP settings sampled.** Every third moment is answered again with self-found
  off and with group quests off (one setting flipped from the default each); kb.db is opened read-only and a
  missing one is reported as skipped. Re-recorded: the old records are unchanged, the new ones are appended.
- **R35, engine golden: two sections.** `sim` is the old record list; the simulator now draws the binary strip the
  addon draws and sends a core that starts like the addon's (`gv`, `gk=0`, `cf=255`; the probe pins gv to 0.15.0),
  so the only change in it is `summary.addon_version` (was null). `addon` replays the addon golden's payloads
  through GameState and records the ring events and summary of each frame. Re-recorded.
- **R35, golden tiers.** Each probe declares `TIER`: addon, desktop and dashboard are "portable" (repository only),
  engine, knowledge and parser are "machine" (this PC's data). `golden.py check --portable` runs the first tier.
  The dashboard probe's headless Edge takes a free debugging port (it used fixed 9351) and waits until the page
  reads the same twice instead of a fixed 4 s.
- **R16, contract 5 (config.json): settings out of range are clamped.** Keys are unchanged. Every engine setting's
  default, range, kind and "applies live" flag now lives once in `companion/settings_manifest.json` (read by
  `settings.py`, app.py DEFAULTS, tuning.py and, via the generated `settings-manifest.ts`, the desktop Settings page).
  A hand-edited value outside its range now reads as the nearest end of it (`lookahead_steps: 500` -> 80, as the
  dashboard already showed), a value of the wrong kind as the default; `load_config` clamps what config.json sets.
  In-range configs behave as before (all goldens "same"). Small default alignments for configs built without
  app.py DEFAULTS (tests, evals): `min_interval` falls back to 20 (the coach's own fallback said 10), `timeout` to 25
  (llm_client said 40); DEFAULTS `gear.spec` is "" instead of None (same meaning: no spec). The dashboard's input
  price for ids without a catalog price now comes from models.py's one Anthropic table (claude-opus-4-0: $15/MTok;
  tuning.py's copy said $5); run_eval.py uses the same table.
- **R16 follow-up, desktop probe (main-process exports)**: `main/store.js` exports `keepBadCopy`,
  `unreadableMessage` and `appSettingsProblem`; `main/secrets.js` exports `secretsProblem`. An unreadable
  `app-settings.json` or `secrets.json` (contract 5) is no longer saved over: the app runs on its defaults (no saved
  keys) for that run, keeps a `<file>.<time>.bad` copy, logs why, and a settings or key change shows the reason
  instead of replacing the file (before, the next save wrote the defaults plus the change, losing every saved key).
- **R21, contract 2 (no shape change): `POST /mute` is for the engine's own voice only.** Mute lives in the app
  (`app-settings.json` `muted`). With voice output "app" (the default) the window's narrator drops the coach's lines
  while muted and the app no longer posts `/mute`; with output "windows" (the engine's speech process) it posts
  `/mute` as before, at every coach start too. The engine keeps mute per run in its voice (`Voice.set_muted` /
  `HubVoice.set_muted`, under the voice's lock; muting interrupts the line being spoken). A change of voice output
  takes a coach restart, as before.
- **R19, contract 8 (Status page and addon wording): one owner per check.** Run by the desktop app (`--app`), the
  engine's `/health` no longer reports the install (the addon installed or left over, "WoW folder not set",
  "RestedXP not found" / "RestedXP <version>"): the app's own checks (`addon_installed`, `addon_old`, `wow_dir`,
  `rxp_app`) cover them, so the page stops showing two items for one problem. The engine keeps only what it alone
  sees: the game still running another addon version, now titled "Restart WoW to load Glide <version>" (detail
  "The game is running Glide X, expected Y. Fully exit and restart WoW (a /reload doesn't load new addon files).",
  no button). The addon status (dashboard banner, the coach's `addon_outdated` callout) says under the app "install
  the update on the Glide app's Addon page, then fully restart WoW" / "install the addon on the Glide app's Addon
  page (it removes them), then fully restart WoW" instead of "copy addon\Glide from the project ..." / "remove the
  ... folder from Interface\AddOns ...", and "the app has" instead of "the project has". Contract 3, additive: the
  state's `addon` carries `kind` ("old", "installed", "running" or null). Run on its own the engine keeps every
  check and the developer wording.
- **R24, contract 2 (engine HTTP): one route table, and the token on two side-effecting GETs.** The engine's
  routes are one table (`dashboard.serve`'s page, `/events`, `/api/state`, `/layout` and static files, plus
  `App.http_routes()`); request and response shapes are unchanged. A path matches exactly with its query parsed
  (`/xph?x` now answers; `/modelsfoo` and `/index...` other than `/index.html` are 404); a known path asked with
  another method answers 405 with `Allow`; a handler's exception answers 500 `{"ok": false, "error"}` instead of
  dropping the connection; an empty POST body reads as `{}`. `GET /calibrate` (runs a capture) and `GET /models`
  (`?refresh=1` refreshes the catalog) now need `X-Glide-Token`, like `/api/state` and `GET /memory`: only the
  desktop app's renderer calls them, through `engineGet`, which sends it. Contract 2, additive (R20):
  `POST /settings` also takes `{"save": <config.json patch>}`.
- **R20, contract 5 (config.json): one reader and writer, and the engine owns the file.** Keys and format are
  unchanged. Every Python reader goes through `companion/glideconfig.py`, so the knowledge build and the tools
  (`build_db.py`, `build_vendor_treasures.py`, `extract_maps.py`, same command lines) now take a config.json that
  Notepad saved with a BOM (they failed on one), and an unreadable file reads as "no WoW folder" instead of an
  exception. While the engine runs, the desktop app's `config:set` saves through `POST /settings {"save": patch}`
  (see contract 5 above for when the app still writes it itself). Desktop probe re-recorded: `main/app/config.js`
  exports `saveConfig` (and `RunningEngine` is a type only). `settings_manifest.json` entries may carry
  `"standalone": true` (the standalone Python overlay's keys); the generated desktop manifest is unchanged.
- **R29, a new shipped code folder: `wowfiles/` (command lines unchanged, contract 7).** The readers of the game
  client's files (casc, db2, dbdefs, db2_import, blp, spells, wdb, adt, worldobjects, terrain, navbuild) moved from
  `tools/` into the `wowfiles` package, with `navformat.py` holding the walking map's format (tile size, layout,
  NAV_VERSION, NAV_REQUIRED, a numpy-free read_version). The engine and the knowledge loaders import it as a package
  (no sys.path edits into tools/); `kb_refresh.NavBuilder.needed` reads only navformat. `tools/*.py` keep every
  stage and flag (`casc.py`, `wdb.py`, `db2_import.py` and `svparse.py` are thin entry points now). The app ships
  the folder (`desktop/package.json` extraResources) and syncEngine copies and replaces it (`paths.ts` CODE_DIRS).
  `tools/svparse.py` is now `knowledge/sources/_lua.py`'s SavedVariables reader (`read_saved_variables`, the one Lua
  table reader); on the 141 real recorder/RXPGuides SavedVariables files it gives the identical result. glide.db's
  input fingerprint lists `knowledge/sources/_lua.py` and `wowfiles/wdb.py` instead of `tools/svparse.py` and
  `tools/wdb.py`, so the next refresh rebuilds glide.db once.
- **R18, parser golden (KB data): dungeon flags hold RestedXP's dungeon tag.** `.dungeon X` now stores X as
  RestedXP's GetDungeonName makes it (upper case, then its alias table: 'Mara' -> 'MARA', 'Stockades' -> 'STOCKS';
  `rxp_parser.dungeon_tag`), the key RestedXP's dungeon settings use; an unknown name sets no flag and warns, as
  RestedXP does. `#dungeon X` step tags stay as written (RestedXP keeps them raw too). `.enabledungeon [!]X` is a
  parsed verb now (args `dungeon` tag, `enabled`), no longer "unknown_verb". Parser golden re-recorded: the
  `parsed` hash of the 14 Dungeon-group guides with `.enabledungeon` and the 7 Alliance guides that spell dungeons
  in mixed case changed, and `_includes` with them; every step numbering (`_index_maps`) is unchanged. A kb.db
  built before this keeps the old spellings until the next KB build (only a selection of some dungeons, not all
  or none, notices).
- **R18, knowledge golden: one RestedXP step rule, RestedXP's own Dungeon-guide behaviour.** The live coach
  (`Route`) and the build's step timing (`live_logs.StepMaps`, obs_step_time) now share one rule and one numbering
  (`rxp_parser.step_shown` / `visible_steps` / `rxp_numbering`) over one settings type (`rxp_parser.RxpSettings`,
  read from RestedXP's SavedVariables names or Glide's `ssf` / `group_quests` / ... names; `settings_key` is
  unchanged). They disagreed on the Dungeon-group guides: the coach turned group quests and every dungeon on there
  when the player had not set them, the build none. Both now do what RestedXP's DungeonCheck does: the guide's own
  `.enabledungeon [!]X` lines first, then a `#disabledungeons` header (47 non-dungeon guides) turns dungeons off,
  then the player's selection; unset options are RestedXP's defaults (off) in every group. For the live coach
  (DEFAULT_RXP players, dungeons unset) this changes the numbering of 5 Dungeon-group guides whose dungeon steps
  name a dungeon the guide does not switch on (BFD in (A) 17-18 Hall of Thanes and 20-21 Darkshore/Ashenvale, DM in
  (H) 12-16 and 18-21 The Barrens and 16-17 Ragefire Chasm): RestedXP hides those steps unless that dungeon is
  selected, and now so does the coach. With the desktop "dungeon runs" switch on, guides with `#disabledungeons`
  drop the dungeon steps RestedXP never shows there (3 guides for a Warrior). Other rule details now follow RestedXP
  exactly where the two copies differed (never on today's data): `#beta`, `#som` / `#era` / `#era/som`, server tags,
  Lua truth for tag values. Stored briefs: meta `step_brief_rxp` gains `"rule": 2`; a kb.db built before it has its
  Dungeon-group guides' stored briefs treated as built with group quests and every dungeon on (as they were), so
  they are served only to such a player until the next KB build. Knowledge golden re-recorded: 7 of 196 records
  change, all in the two Alliance Dungeon guides with a BFD step (ETA, and the fact ids after the hidden step in
  Darkshore/Ashenvale).
- **R17, engine reads kb.db only through knowledge.api (contract 6 additive: no table or column change).** New
  KnowledgePack methods (`knowledge/api/lookup.py`, "the engine's reads"): `step_links`, `quest_steps`,
  `friendly_npcs`, `vendors_selling`, `continent_of`, `map_name`, `quest_name`, `map_areas`, `landmarks`,
  `rxp_upcoming`; briefing.py, reminders.py, coach.py and app.py no longer query kb.db. The pack is built with the
  coach's geo (`open_pack(api, kb, rxp, geo)`), and one `knowledge_layer.KnowledgeHandle` holds the current pack, api
  and reason for the coach, the app (`app.pack` / `app.kb_reason` are views of it; `app.kb` is gone) and the rules
  (`rules.kb_fn` is the handle). glide.db at runtime: geo.py's sub-zone boxes, landmarks and map names, the
  lookahead fallback's raw guide lines and the coach's quest-name fallback now come from kb.db (area_box/area,
  creature_spawn/creature/quest_giver, rxp_action, quest); glide.db is read for them only when there is no
  knowledge pack. Player-visible: landmark points are kb.db's creature_spawn (points rounded to 0.01 and merged per
  source: 88,665 kb rows vs glide.db's 115,757 spawn rows) instead of glide.db's spawns: on this PC the same NPCs
  (0 added, 0 lost over 43 maps), 7,429 vs 7,344 distinct points; sub-zone boxes identical. The lookahead fallback
  numbers lines by the guide's step index (it said "approximate" before too), and a quest with no objective spots in
  kb.db no longer falls back to glide.db's spawn centre for the "near the quest" briefing check. All goldens "same".
- **R17, kb.db schema stamp (contract 6 additive: a meta key, no table or column).** The build writes meta
  `schema_version` (`knowledge/store.py` `SCHEMA_VERSION`, now 1); a kb.db without it (built before) counts as 1.
  KnowledgePack refuses another version ("rebuild needed (schema N, engine expects M)", shown like the other
  knowledge-layer reasons: startup line, Status page, a hot swap keeps the old pack), and `build_kb.staleness` calls
  such a kb.db stale. The build adapter `kbdata.ConnKB.q` prints a failed query once per distinct error (as
  `store.KB.q` does) instead of swallowing it.
- **R30, one parameter registry read from the KB (no interface change; `p:` fact ids, the `param` and `heuristic`
  tables unchanged).** Analyzer parameters register in `knowledge/mechanics/params.py` (`register(..., lane=
  "analyzer")`; `knowledge/analyzer/params.py` is that lane's view, same functions). At runtime the KB in use
  answers: `kbdata.get_data(kb)` makes that KB's param table (plus meta `analyzer_params` for an older build) the
  current values, replacing the previous KB's (`use_kb`); before, the first KB's fitted analyzer values stayed in a
  process-global dict after a hot swap. The coach's heuristic rules and `h:` facts come from the KB's `heuristic`
  table (`KnowledgePack.rules()`); params.json and rules.json are build inputs (a KB without param or heuristic rows
  falls back to them). On this PC every parameter's value, status and evidence and all 321 rules are the same as
  before; all goldens "same".
- **R31, builders share one install and code fingerprint (knowledge/store.py).** `tools/build_db.py` now installs
  glide.db like build_kb installs kb.db (`store.install`: staged copy + `os.replace`; when the target stays open,
  `glide.new.db`, which `kb_refresh.promote` installs at the next engine start) instead of copying over the live
  file. Its input fingerprint now covers the content of every repo module it runs (`build_db.CODE`: store.py,
  _lua.py, wowfiles/wdb.py, glideconfig and its imports; checked by tests/test_build_install.py) instead of
  size+mtime of three files, and build_kb's staleness inputs gain `game file code` (`wowfiles/**/*.py`, content
  hashed). Effect for players: one rebuild of glide.db and kb.db at the next background refresh after the update
  (the fingerprint format changed). Command lines and exit codes unchanged.
- **R28, map_bounds fits only maps without an exact bound.** Recorder bounds are read before the fit; a map with a
  conf-1 bound (obs/db2) is no longer fitted (a fit, conf <= 0.95, never replaced it). meta `map_bounds:fit` lists
  those maps as `{"n", "fit": null, "exact": src}`; `GLIDE_MAP_FIT_ALL=1` restores the full validation fit (stats
  only, with `exact_err_pct`). The map_bounds and map tables of a full temp build are identical before/after;
  meta `map_bounds:continent` now validates the continents against the exact bounds. Knowledge golden "same".
- **R17, prompts: the quest rows come from kb.db (contract 6 wording; no table or column change).** The game context's
  quests (`prompt_context.game_context`: objectives with their spawn centres, givers, turn-ins and their places,
  level, zone, quest text) and its map names, and the dashboard's and the speech vocabulary's quest names and levels
  (app.py `_quest_basics`), come from the knowledge pack (`KnowledgePack.quest_detail(qid, near_map, detail)`, the
  shape `GlideDB.quest()` gives, and `quest_basics(qid)`); glide.db is read for them only without a pack. Contract
  6 now says glide.db is the knowledge build's input when a kb.db exists. `wow_text` moved to `knowledge/store.py`
  (glidedb.py imports it). The prompt layout, key order and caching split are unchanged; only the knowledge in the
  quest rows changes. On this PC, over the 7,406 kb quests: 1,976 rows identical; objective `type` uses the KB's
  words (kill, collect, interact, talk, explore, event, other instead of glide.db's monster, item, object, 10,
  19, ...; 6,251 objectives); `side` is capitalised (Alliance, Horde, Both; 4,575 quests); `giver_at` / `turn_in_at`
  added for 3,880 / 4,201 quests glide.db had no point for (the KB places the giver from its spawns), and an observed
  point (where the quest was seen taken or handed in) comes before a spawn-derived one (giver_at differs for 50,
  turn_in_at for 47, mostly by under 0.1%); giver / turn-in names are listed once (40 / 25 quests named the same
  poster or object twice); objective spawn centres come from kb.db's creature_spawn / gameobject_spawn rows (merged
  per source, plus RestedXP's pins): 59 centres moved (12 by more than 2% of the map, 2 onto another map), 73 spawn
  counts differ, 24 objectives gained a target and place (talk-to NPCs); 2 target names lost glide.db's cache
  garbage (quest 15 "Kobold Worker"); XP from the KB (329 added, many 0 for event quests; 15 changed to the KB's
  Forever values). A 10-quest Dun Morogh log's quest section went from 5,447 to 5,489 characters, an Elwynn one from
  5,040 to 5,528 (the added places). R34 fixture: every quest has the same keys, names, givers, places and objective
  targets; only the objective type and side words change, and one objective counts 13 spawn points instead of 12.
  All goldens "same" (no probe re-recorded).
- **R13, contract 8 (Status page, capture log and Game capture check): an addon in another strip version is named.**
  A frame whose length and Fletcher-16 hold but whose version byte isn't `protocol.PROTO` now decodes to
  `protocol.OtherVersion` (falsy, so readers that take good frames skip it; version 0 with no payload, what a black
  grid reads as, stays "unreadable"). The wire format is unchanged (contract 1). The capture loop counts those frames
  (`App.strip_other`) and logs "[capture] the game's Glide addon sends strip version N and this engine (Glide app)
  reads version 1: <fix>." instead of "no Glide strip found. Is the addon on ...". The Status page's game-link item
  then says, under the app, "The Glide addon doesn't match this app" / "The game's Glide addon sends strip version N
  and this Glide app reads version 1. Update the Glide app, or install its addon on the Addon page and fully restart
  WoW." (an older addon: "Install the update on the Glide app's Addon page, then fully restart WoW."); run on its own,
  "Glide addon speaks strip version N, the engine expects 1" with the developer fix (and the `addon_install` button
  for an older addon). Settings > Game capture's check answers `problem: "version"` with "The strip reads cleanly,
  but the addon speaks strip version N and this coach reads version 1: ...". Good frames after them clear it.
- **R13, contract 2/3/4 additive: the versions in play.** The desktop app passes its version to the engine (env
  `GLIDE_APP_VERSION`, beside `GLIDE_TOKEN`); `GLIDE_READY port=N app=V` (the app still matches `GLIDE_READY
  port=\d+`). The state and `GET /health` carry `versions` {`app` (null run on its own), `addon_expected`,
  `addon_installed`, `addon_running`, `strip` (PROTO), `strip_seen` (the version the addon last sent, null before any
  frame), `kb_schema`, `nav`}, typed `Versions` in `desktop/src/shared/engine.ts`; the dashboard fixture has it.
- **R13, addon golden: the addon's version is masked.** `probe_addon.py` makes the mocked addon report version
  `golden` (same length as a real one), so an addon version bump no longer changes the payloads (`gv=`), the strip
  paint or the window's version line. Re-recorded once: only `gv=0.15.2` -> `gv=golden`, the paint hashes and the
  window's "v0.15.2" -> "vgolden" change. The engine golden's `addon` section replays those payloads, so its
  `addon_version` reads "golden" (re-recorded for that only).
- **R26, contract 2 and 3: a slimmer state stream.** (1) `community` (about half the state) leaves the per-second
  `state` event: a new `event: community` (data: the notes, or null) is sent when they change, and a new client's
  `init` and `GET /api/state` still carry it inside the state (Hub keeps the last one). Its only reader, the
  dashboard, listens for that event (and still takes `community` from any state that has it). `EngineState.community`
  is now marked optional. (2) An additive light stream, `GET /events?lite=1`: the same events, but its `init` and
  `state` carry only `overlay_step`, `frames`, `level`, `zone`, `session_minutes`, `coach`, `models`, `refresh`,
  `navbuild`, `reminders` (dashboard.py `LITE_KEYS`, engine.ts `LiteState`) and it gets no `community` event; the
  app's overlay and its window (session bar, narrator) use it. (3) `tuning` stays in every state (no UI reads it) but
  is recomputed only when the settings, models or measurements it reads change, or once a minute
  (`tuning.SnapshotCache`). On the dashboard fixture: 3 x the full state per second before, the dashboard's
  state without community plus two light states after (numbers: `tests/test_state_stream.py`). No visible change.
- **R32, addon slash commands: one registry (contracts 7 and 8).** Every command is registered by its own module in
  Core.lua's `ns.Command(name, help, fn, aliases)`; `/coach` and `/glidecoach` are aliases of `/glide coach`, and
  `/glidelink <command>` is `/glide <command>`. Every command name and what it does are unchanged. Changed: `/glide
  help` (and an unknown command) lists every command sorted by name, one line each as `/glide <usage> - <what>`
  (before: the recorder's commands, then hand-written live-link lines); `/glidelink` alone or with an unknown
  command shows that same list (before: the live-link lines plus a strip status line, which `/glide dump` now
  prints); `/glidelink` also reaches the recorder's commands (`/glidelink status`); a live-link command that
  fails prints its error like the recorder's did. The buffs, gear, extras, coach and minimap diagnostics moved
  from Strip.lua to their own modules; their output is the same.
- **R32, addon 0.15.3: one error policy, reported on the strip (contracts 1, 3, 8; additive).** Every error the
  addon catches (a failing event handler of the recorder or the live link, a strip frame, a page or list build, a
  slash command) goes through Core.lua `ns.ReportError`: counted, the last kept, the session's first 50 saved in
  `GlideRecorderDB` (before: only the recorder's; the live link's were dropped unless `/glide debug`). An event the
  client doesn't have is no longer an error (listed by `/glide dump`; the recorder used to save it as one). New
  core key `er` (the count, sent only when there is one) and page `E` (`ex`, the last error's text); the state has
  `addon_errors` (`{count, last}` or null; desktop/src/shared/engine.ts, and the dashboard fixture got the key);
  health shows a warning "The addon reported N error(s)", detail "Last: <text>. In game, /glide debug prints each
  one as it happens; /glide dump shows the count and the last." `/glide dump` prints "errors: N, the last: ...".
- **R32, addon 0.15.3: no `fpn` ring event.** A new flight path on the taxi map no longer raises ring event `fpn`
  (nothing read it; a full taxi map was the one burst that could push real events out of the 3-slot ring). The
  registry keeps it, marked retired; the engine still ignores it from older copies. List `F` is unchanged.
- **R32, addon 0.15.3: State.lua builds its pages on its own 1 s clock** (Strip.lua only draws; before, the strip's
  frame rebuilt them at most once a second), and Extras.lua takes its events from the shared bus (`ns.On`) instead
  of its own event frame. tests/wowmock.py's `C_Timer.NewTicker` now honours its interval (it ran every ticker on
  every tick). Addon golden re-recorded: the version (`gv=0.15.3`, also in the window); the first three frames carry
  no page and `qn=0;qh=0;an=0;ak=0` (the first page build is 1 s after login), and the page order follows from that;
  from the QUEST_ACCEPTED frame on, the core carries `er=1` and page `E` goes out once: the mock lacks
  `C_QuestLog.GetTitleForQuestID`, so the recorder's QUEST_ACCEPTED handler fails there (it did before too: the
  session's error count is unchanged); the strip paint. Engine golden re-recorded: only the summary's new
  `addon_errors` key (null).
- **R32, sim: the quest log's hash.** companion/sim.py sent `qh` as the number of quests; it now sends the list
  hash of the Q0 chunk it sends, so the engine (which now checks it) takes the simulated quest log as before.
- **R32, engine: the quest log is checked against its hash.** `GameState` takes the quest log only when chunks
  `Q0..Qn-1` (core `qn`) are all there and their joined text matches core `qh`, like buffs and gear (before, a new
  chunk next to a stale one mixed two logs for a frame); a shorter log is taken as soon as the core says so.
- **R32, GlideRecorderDB readers check its layout.** tools/build_db.py and knowledge/sources/map_bounds.py read it
  through `knowledge/sources/_lua.py` `recorder_db()`, which warns (build log / KB notes) about a `schema` other
  than 1 or sessions recorded by an addon newer than the project's; the data is read as before. The layout is
  written down in docs/RECORDER.md.
- **R33, vendor treasures from the installed addon.** The addon names a nearby vendor by its index in its own
  Data/VendorTreasures.lua; companion/vendor_treasures.py now looks it up in the installed addon's copy
  (`<wow_dir>/Interface/AddOns/Glide/Data`), and in the project's only when there is none, re-reading a copy when
  its modification time changes. No wire change.
- **R28 follow-up, map_bounds reads only the recorder's mapBounds table.** `sv_bounds` still reads every recorder
  SavedVariables file (archive and live client) but no longer parses each whole ~5 MB file: a brace scan that skips
  strings and comments (`_lua.saved_variable_field`) finds `GlideRecorderDB["mapBounds"]` and parses only that
  table. Same rows (the player's 43 files: 2451 bound rows, identical hashes old vs new; `tests/test_sv_field.py`);
  that step went from ~44 s to ~1.4 s of each KB rebuild. No data change.
- **R37, contract 4 additive (desktop IPC): `app:logs`, and the logs on disk.** The Logs page's lines (the
  engine's stdout/stderr, tracebacks included, and the app's own lines) are now also appended to
  `userData/logs/engine-YYYYMMDD.log` (desktop/src/main/logfile.ts: a file per local day, the 7 newest kept,
  batched async writes, a failed write counted and dropped), with a `==== coach start | app | engine home | python`
  line per start and `==== coach ready | GLIDE_READY ...` once it listens. New channel `app:logs` (no arguments;
  answers shell.openPath's "" or the reason) opens that folder: the Logs page's new "Log files" button. Desktop
  golden re-recorded for the channel, the new `main/logfile.js` module and `paths.logsDir` only. Engine side (no
  interface change): status lines go through companion/log.py with their text unchanged, and the 21 silent
  `except Exception: pass` sites log their first error per site (`log_once`) and still carry on.
- **R25, one home for the shared helpers (contract 8: offline why wording).** The offline coach's "why this step"
  line now uses the knowledge layer's why phrases (knowledge/api/reasons.py: "on the way: little detour", "vendor /
  supplies" where it said "on the way, little detour" and "vendor and supplies"); its own copy, now the same words,
  is only the fallback when the knowledge layer did not load. No golden probe changed. Number parsing lives in
  knowledge/values.py (stdlib only) and every reader keeps its old results, except that an infinite or huge number
  (float('inf'), 1e400) now reads as "no number" in the int readers that used to raise OverflowError on it.
  Classes, races, factions and their bitmasks live in knowledge/vocab.py (bit 16 reads back as "Scourge", the game
  token, and both "Scourge" and "Undead" look it up, as before); feed message kinds in companion/msg_kinds.py; the
  core cf decoder in strip_spec.coach_flags. The knowledge loaders' and glide.db builder's code changed, so the
  next refresh rebuilds kb.db and glide.db once (code fingerprints).
- **R28 follow-up, faster Lua strings in every SavedVariables read.** `_lua._TOKEN`'s string tokens use the
  unrolled "plain run, then (escape, plain run)*" form `_HIDES` already used: the same strings (the player's 140
  recorder and RXPGuides SavedVariables files, 369 MB: tokens, `saved_variables` and `assignments` hash-identical
  old vs new; `tests/test_lua_strings.py`), tokenizing them in 8.7 s instead of 68 s and `saved_variables` in
  16 s instead of 77 s. No data change; the loaders' code changed, so the next refresh rebuilds once.
- **R25 follow-up, the knowledge side counts briefings and reminders as the coach's lines (contract 6: kb.db
  eval_flag).** knowledge/vocab.py `COACH_SAID` (callout, answer, review, briefing, reminder; kept equal to
  companion/msg_kinds.COACH_SAID by `tests/test_helpers.py`) replaces flags.py's `COACH_KINDS` (callout, answer,
  review) and run_eval.py's inline copy. A `/glide mark` made right after a quest briefing or a reminder now stores
  that line as its `eval_flag.text` instead of the older callout before it, and an eval replaying a session log
  keeps briefings and reminders among the coach's recent lines (`tests/test_coach_said.py`). The loaders' code
  changed, so the next refresh rebuilds kb.db once.
- **R37 follow-up, the broad excepts that fall back to a default log too.** The 23 engine sites that caught any
  error and silently used a fallback value (the state's community and tuning extras, the coach's history, talent
  pick, fact check and glossary, briefings, step videos, reminders, the knowledge extras, ...) now log their first
  error per site with `log_once` and fall back as before; four stay quiet because their error is the normal case
  (no knowledge or wowfiles package for `version_info`, the pre-8.1 Windows DPI call, a bad `POST /ask` body).
  `tests/test_log.py` fails on a new silent fallback. Only new console lines; no other change.
- **Contract 4 and files on disk additive (desktop IPC, `app-settings.json`): `sessionBarOrder`.** The session
  bar's pieces drag left and right (`desktop/src/renderer/session-bar-order.ts`), and their order is saved as
  `AppSettings.sessionBarOrder` (the pieces' `data-sb` keys) through the existing `app:settings:set`. A new
  optional key; nothing else changes. The player asked for it (2026-10-29), no review recommendation.
- **Contracts 3 and 4 (the coach screen's state, desktop IPC): the pre-0.8 addon cleanup is gone.** The Addon
  page's "Also remove the old GlideLink and GlideRecorder addons" switch, `addon:install`'s `removeOld` argument,
  `AddonStatus.leftovers`, the Status page's `addon_old` check, `versions.leftovers()` and the addon check's
  `kind: "old"` are removed: those addons predate 0.8.0 and no install has them any more (the player asked,
  2026-10-29). Old GlideRecorder.lua recordings are still read (archiver, build_db).
- **Player-visible text (the coach screen): panels show crests, not names** (`dashboard` golden re-recorded).
  The book's tabs, the docked sections, the coach chat and the route tracker no longer print their names
  ("Quest log", "Coach", "Route", ...): each carries a crest (its emblem in the HUD's round bezel) and the
  header row shows only its live reading; the names stay as accessible names (`.sr`, `aria-label`, the tab's
  tooltip). The player asked for it (2026-10-29), no review recommendation.
- **Contracts 2, 3 and files on disk additive: the community feed.** New notes reach players without an app
  update: the community feed in `feed/` of the public Fermin7c1/Glide-releases repo (a manifest with the feed
  file's sha256; the format is in `companion/community_feed.py`, which only reads it) is checked every
  `community.feed_minutes` (new settings `community.feed`, `community.feed_minutes`, `community.feed_url`),
  keeps it as `data/live/community_feed.json.gz` with its state in `data/live/community_feed.json`, and writes
  its notes into kb.db's community tables (again after a rebuild). The state gains `community_feed` (version,
  notes, checked, fresh, error); HTTP gains `POST /community/seen` and `GET /sound/<name>.ogg` (the game's
  level-up fanfare, read from the player's own client like the buff icons: a granted exception). The player
  asked for it (2026-10-29), no review recommendation.
- **Player-visible text (the coach screen and the session bar): no panel headers, the volume in the footer**
  (`dashboard` golden re-recorded). The panels' header rows are gone: each panel's emblem sits large and dim in
  the top right of its ground, it moves by an invisible grip along its top edge, and its name and reading stay as
  the accessible heading (`h2.sr`). The coach chat's volume bar is gone; the session bar's speaker raises a
  vertical volume bar (hover, wheel) and still mutes on a click. The coach screen no longer sends `volume` or
  `muted` messages to the app, only `ask`. The player asked for it (2026-10-29), no review recommendation.
- **Player-visible text and contract 2 additive (push-to-talk, the book, the minimap, the name).** The engine sends
  a `ptt` event (`listening`, `transcribing`, `idle`) to every client as it happens; the overlay, the session bar
  and the coach screen's ask box show it (`dashboard` golden re-recorded). The book's tabs move to a column of
  crests down its right edge; the minimap's zone name is engraved on the map (no header above it); every panel
  moves by its grip. The app shows its name as "Glide" (window, page and tray titles, shortcut and uninstall
  names); the product name, executable, install folder and settings folder stay "Glide to 60" so installs,
  settings and updates keep working. The update window now gets its arguments (a real UTF-8 BOM), opens over
  Glide's window, shows its icon and a filling bar, and Glide closes once it shows. The player asked
  (2026-10-29), no review recommendation.
- **Player-visible text (the coach screen): the book has no tabs** (`dashboard` golden re-recorded). The book is
  one page of its sections, one after another in the book's order, each named only by its emblem; a section
  moves by its grip (on the shelf or docked). The crest tabs, `#tabs`, the open-tab memory (`glide.tab`) and the
  tab keys are gone. The player asked for it (2026-10-29), no review recommendation.
- **Addon 0.16.0: the new-at-your-trainer window** (`addon` golden re-recorded for the TOC's new file). On a level
  up that brings spells or ranks (RestedXP's training list less the spellbook, Extras.lua), `Training.lua` shows
  them as the spellbook does, with what a new rank changes against the known one, the trainer costs the recorder
  has seen, the total and the player's gold; `/glide train` opens it, the Glide window switches the auto-open
  (`GlideLinkDB.train.auto`). The strip and everything it sends are unchanged. The player asked (2026-10-29).
- **Security review before the repository goes public (contracts 2, 4 and files on disk)** (`dashboard` and
  `desktop` goldens re-recorded). The engine's token is no longer in the served page: the app opens the coach
  screen at `/#t=<token>` (a URL fragment never reaches a server) and a standalone engine prints that link. The
  page gets a Content-Security-Policy (its own inline script by hash) and `nosniff`. The community feed refuses
  non-https addresses and redirects, inflates at most 64 MB and cleans note fields; community source links must be
  http(s). A custom endpoint only gets a built-in vendor's key at that vendor's own host. The app answers IPC only
  from its own pages, its windows show only its own pages, `shell:open` opens web pages and existing local folders
  only, saved keys must be named `*_KEY` / `*_TOKEN` (never PATH, PYTHON*, PIP_*, proxies), `endpoint:test`
  follows the vendor-key rule, `app:settings:set` and `data:import` refuse network shares, and the Whisper model
  name must be a known one. The coach can't change its memory in an answer that read web results. The TACT key list
  is fetched at runtime (wowdev/TACTKeys) instead of shipped; `tools/build_seed.py` runs third-party Lua in a
  sandbox (`tools/lua_sandbox.py`). Electron 44.10.0. The player asked (2026-10-30).
- **Electron's asynchronous safeStorage** (`desktop` probe re-recorded: `main/secrets.js` exports `loadSecrets`):
  Electron 45 deprecates the synchronous `safeStorage` calls and 46 removes them. The key store is decrypted once at
  start with the asynchronous ones (`loadSecrets`, before the engine starts) and kept in memory; `setKey` is async,
  still starts from the file on disk, runs one at a time, and a key Windows asks to re-encrypt is saved again.
  `secrets.json` keeps its format, and keys saved by earlier versions read as before. The player asked (2026-10-30).
- **Contract 4 and files on disk additive (desktop IPC, `app-settings.json`): first-run setup** (`desktop` probe
  re-recorded: the `keys:test` channel). A new install opens a guided setup (`desktop/src/renderer/setup.ts`): the
  game folder and addon, push-to-talk recognition, key and a microphone check, the coach's voice, and an AI provider
  (Anthropic, Gemini, OpenAI as the custom endpoint "openai" at api.openai.com, or OpenRouter) whose key is tested
  before it's saved (`keys:test(env, value?)`, answering the model ids the key can use) with
  balanced or best models. `AppSettings.setupDone` marks it finished or skipped; a player whose WoW folder was set
  before it existed gets `"before-setup"` at start and never sees it; Settings > General runs it again. Settings it
  writes go through the existing `config:set`, `keys:set` and `app:settings:set`. The player asked (2026-10-30).
- **Addon 0.17.0, contracts 4, 7 and files on disk additive: RestedXP guides added from the app** (`addon` and
  `desktop` goldens re-recorded: the TOC's two new files and the two new channels).
  The Addon page takes a RestedXP guide string (`addon:rxpImport(text)`, `addon:rxpImports()`), so a guide bought
  mid-session reaches the coach and RestedXP without the game's import window. `companion/rxp_import.py` opens it
  with the key RestedXP saved for the account on this PC (`RXPData.cache` in its account SavedVariables; never a
  typed BattleTag), keeps the guide texts in `data/rxp_imports/imports.json` (new; `tools/build_db.py` reads them
  like RestedXP's own cache) and writes the string to the installed addon's `Data/RXPImports.lua` (new, shipped
  empty; the app writes it again after an addon install and the engine at start). At login the addon's
  `RXPImport.lua` hands each waiting string to RestedXP's own `guideImporter:ImportString`, once per string and
  account, and notes the outcome in `GlideRecorderDB.rxpImports[id] = {t, ok, err}` (new field);
  `/glide rxpimport` (new command) tries failed ones again. The strip is unchanged. The player asked (2026-11-01).
- **Follow-up, player-visible text: a guide file too.** RestedXP delivers a guide as a downloaded file holding the
  string, so the Addon page's "Choose file" (or dropping the file on the box) reads it in the window and adds it
  through the same `addon:rxpImport(text)`; no channel or file changes. The engine's messages name the file as
  well as the pasted string. The player asked (2026-11-01).
- **Addon 0.17.1 and the app: Glide's official icon** (what the player sees). The player's pixel-art winged boot
  (`desktop/assets/icon-source.png`) is the app's icon (`icon.png`: window, installer, tray, update splash,
  README) and the addon's (`addon/Glide/Media/icon.tga`, 64 x 64: the AddOns list's `IconTexture` and the minimap
  button, shown whole: no game-icon frame to trim). The drawn `icon.svg` is gone; `render-icon.js` renders the artwork.
  The player asked (2026-11-01).
- **Addon 0.17.2, files on disk: RestedXP imports decrypted before loading into the game.** `companion/rxp_import.py`
  now writes the decrypted guide texts (`guides = { ... }`) to `Data/RXPImports.lua` instead of the encrypted string,
  and `RXPImport.lua` hands each plain guide text directly to RestedXP's `ImportGuide` in small batches instead of
  running `guideImporter:ImportString` (base64 + RC4 + zlib) inside the client. `rxp_import.py status` also includes
  per-guide summaries (`items`) and `rxp_import.py guide <id> <index>` (`addon:rxpImports(id, index)`) returns the
  parsed guide (`knowledge/rxp_parser`) for the Addon page's interactive RestedXP Guide & Route Explorer
  (`desktop/src/renderer/rxp-browser.ts`). The player asked (2026-11-01).
- **Follow-up, player-visible text: Skyborne under both factions in the Explorer's race picker.** Skyborne joins
  either faction, but the Guide & Route Explorer listed it only under Horde and read every Skyborne pick as Horde,
  hiding an Alliance Skyborne's steps. The picker now offers it under Alliance and Horde (values `Skyborne@Alliance`
  and `Skyborne@Horde`), and the `<<` condition check moved, unchanged otherwise, into
  `desktop/src/renderer/rxp-conditions.ts` (tested by `tests/test_rxp_conditions.py`). The player reported it
  (2026-11-01).
- **Follow-up, contract 4 additive: RestedXP's free guides in the Explorer.** A bought string holds the levels RestedXP
  sells (about 20-60 and the dungeon guides); the starter zones to about 20, Zephras Isle and the Mage AoE routes come
  free inside the RestedXP addon (`Guides/forever`), so the Explorer showed no 1-20 guides. `rxp_import.py status`
  now ends with one more entry for them (`{"id": "rxp-free", "free": true, "source", "items", ...}`, read from the
  installed RestedXP, else the seed snapshot, as the knowledge build does), `guide rxp-free <index>` opens one, and
  `RxpImport` gains the optional `free` and `source`. The Addon page's added list leaves that entry out; the Explorer
  lists each group in level order. A free guide with a bought guide's group and name is left out. The player reported
  it (2026-11-01).
- **Follow-up, knowledge parser and the Explorer's map: world-coordinate positions.** RestedXP's Forever guides give
  `.goto 1411/1,wy,wx` in world coordinates, which the parser keeps as wx/wy (the knowledge build's finalize step
  puts them on the map for the coach), but the Explorer parsed guides itself and read only map percent, so its map
  drew nothing. `rxp_import.py guide` now places them with kb.db's `map_bounds` the same way
  (`place_world_points`, finalize's swap detection), and answers `world_only` / `placed` so an unplaceable guide says
  why. The parser (`knowledge/rxp_parser/verbs.py` `_p_loop`) now reads a world-form `.loop 25,1411/1,...` as `.line`
  already did (`ui_map`, `continent`, `wpoints`); before, its world numbers became map-percent points on no map. A
  machine-tier `parser`/`knowledge` golden on the development PC differs only where a guide has a world-form `.loop`;
  re-record those probes there if they flag it. The player reported the empty map (2026-11-02).
- **Addon 0.17.3, files on disk: decrypted RestedXP packs go only to the account they were bought for.** Since 0.17.2
  the addon loaded the decrypted texts with RestedXP's `ImportGuide`, which runs no account check (the encrypted
  string carried it), into whichever account logged in first, and that account's note took the pack out of the file
  before the buyer's account had it (review of 7d21f49, three reviewers). Each pack in `Data/RXPImports.lua` now
  carries `k`, the buyer account's key check (`rxp_import.key_check`: Adler-32 of its 16-number RestedXP key); the
  addon computes the same from RestedXP's key for the account logged in (`ReadCacheData`, else `RXPData.cache`) and
  leaves other accounts' packs alone, with no note. `handed_over()` now answers per account and `note_for()` counts
  only the buyer account's note. The addon loads guides for at most 40 ms of RestedXP parsing per tick
  (`debugprofilestop`; one guide a tick without it, as RestedXP's own importer), counting a guide only once RestedXP
  took it, instead of 15 at a time (Hardcore aborts a script after 200 ms). The engine leaves the file alone while
  the installed addon is older than 0.17.2 (it reads encrypted strings).
- **Follow-up, what the player sees: the Explorer is its own page, "Guides".** The RestedXP Guide & Route Explorer moves
  off the Addon page (where it sat at the bottom, scrolled with the page and scrolled again inside its step list) to
  its own page in the rail, under Coach. The page is fitted to the window like the Coach page: the map keeps its shape
  at the largest size its column holds, the guide window fills the height, and only the step list scrolls (a long
  current step gets a capped box of its own); below 900 px wide the two columns stack and the page scrolls. The
  Addon page keeps adding guides, and its "Browse guides" opens the guide on the new page. No IPC, file or state
  change. Also from the review: walking the route stops when the player leaves the page and scrolls only the step
  list, the latest guide picked wins over a slower earlier one, a guide that fails to open says so, and a guide filed
  under both factions keeps the group it was picked from. The player asked (2026-11-02).
- **Follow-up, what the player sees: the Guides map zooms and pans, and done steps step aside.** The scroll wheel zooms
  toward the cursor (up to 8x), dragging pans, double-click (or the reset button, or 0) shows the whole zone, + and -
  zoom; pins, arrowheads and lines keep their size on screen. "Follow step" keeps the current step in the middle;
  pins of the steps before the current one are hidden unless "Done steps" is on. On the Guides page, away from a text
  box, Left/Right step through the route and Space walks it or stops. Arrowheads are drawn as their own shapes
  instead of line markers (markers grow with zoom). The player asked (2026-11-02).
- **Follow-up, what the player sees: one-sided filters and "Next guide" (review findings).** With only a race or only a
  class picked, a step now shows when a character that pick allows would see it (`rxp-conditions.ts`, Glide's
  race-class table from `knowledge/vocab.py`), so "<< !Warlock" no longer hides for "Human, all classes" and a Gnome
  never sees Druid steps. "Next guide" looks in the group RestedXP names, else the group being browsed, before any
  other group, so a Dungeon guide's next is the Dungeon guide. The player asked for the Explorer's review fixes
  (2026-11-02).
- **Follow-up, what the player sees: pin hover cards on the Guides map.** Hovering a pin (or reaching it with Tab) shows
  a card beside it with the step's number, instruction, quest and NPC badges and position; Enter or Space picks it.
  The pins' native tooltips (SVG `<title>`) are replaced by the card and an `aria-label`. The player asked for map
  interactivity (2026-11-02).
- **Desktop IPC, command lines and files on disk (additive): walking routes between a guide's steps.** A new channel
  `addon:rxpRoutes(id, index, req)` (contract 4; in `InvokeMap`, the preload allowlist and `main/app/ipc.ts`; types
  `RxpRoutesRequest`, `RxpRoutes`, `RxpRouteLeg`, `RxpRouteSegment` in `desktop/src/shared/ipc.ts`) runs a new
  subcommand `rxp_import.py routes <id> <index>` with the request as JSON on stdin (contract 7;
  `companion/rxp_routes.py`, which documents the answer: legs between consecutive stops of the visible steps by
  guide step number, walk / fly / hearth / death / boat / straight segments in map percent on every guide map of
  their continent, and `pending` legs to ask for again). It keeps walking geometry in a new cache,
  `data/rxp_imports/routes.db` (contract 5; sqlite3, safe to delete). `knowledge/api/navigation.py` gains an
  optional `max_expand` (None keeps the coach's searches as they are) and `walk_world`. Nothing is drawn yet: the
  Guides page uses it in phase 1 of docs/plans/guides-explorer.md (recommendation "Drawn walking routes" 1-8,
  2026-11-02).
- **Follow-up, contracts 4 and 7 additive: per-guide facts for the Guides page.** A new command
  `rxp_import.py facts <id> <index> [CLASS|-] [RACE|-]` answers `{"ok": true, "facts": ...}` and a new IPC channel
  `addon:rxpFacts(id, index, cls?, race?)` (types `RxpGuideFacts` and its parts in `desktop/src/shared/ipc.ts`) carries
  it: the kb match (exact, aligned by step text and quests, or none) with its idx map, per quest its accept/objective/
  turn-in/gate steps and, with kb, name, level, follow-ups and what skipping it loses (`simulate.unreachable`, the
  coach's skip rule), map targets for objective steps, the prep list (buys, Auction House collects, bank trips), the
  class's `step_brief` plan and hazards. Built read-only by the new `knowledge/api/explorer.py` `guide_facts` over the
  newest kb (`data/kb.db` or a staged `data/kb.next-*.db`, refusing another schema version); without kb it still
  answers quest steps and prep. `_entity_names` (the `guide` command's quest, NPC and item names) now reads kb first
  and glide.db second. No table, column or player-visible text changes; only the `desktop` golden probe is
  re-recorded (the new channel). Guides page plan, phases 3-5 (docs/plans/guides-explorer.md).
- **Follow-up, contract 4 additive and what the player sees: the Guides page shows the coach's steps and numbers.**
  `rxp_import.py guide <id> <index>` now expands `#include` over the guides the knowledge build reads (the free
  Dungeon guides showed one empty step instead of 62-847; "14-16 Darkshore" had 236 steps instead of 340), and sends
  which steps RestedXP shows for whom, worked out with `knowledge/rxp_parser` (new `companion/rxp_explorer.py`):
  `RxpGuideDetail` gains `players` (every race pick x class), `settings` (the 8 SSF / group quests / hardcore
  combinations), `vis_masks`, `defaults` (the coach's RestedXP options, config.json "rxp") and `maps` (each zone's
  size in yards and rectangle on its continent, from the newest kb.db including a waiting `kb.next-*.db`);
  `RxpGuideStep.vis`, `RxpGuideAction.vis`, `RxpGuideWaypoint.act` and `next_list[].vis` are new. The renderer no
  longer evaluates `<<` conditions (`rxp-conditions.ts` is replaced by `rxp-model.ts`, which reads the masks;
  `rxp-geometry.ts` is new): the steps and numbers match the coach for every character (they disagreed on 41 of 80
  free guides for a Human Mage), the toggles start from the coach's settings (SSF was off), steps RestedXP never loads
  (`step << skip`) never show, and "Show all <<" dims other characters' steps without numbers. The current step,
  the picked pin, ticked objectives and waypoint letters are kept by guide step number, so a filter change keeps the
  step. The arrow's "~N yd" uses the zone's real size (none shown without a knowledge base) and its bearing is in
  yards. Guides page plan, phase 0 (docs/plans/guides-explorer.md); the player asked (2026-11-02).
- **Follow-up, contract 4 (what a field holds): Guides map images by URL.** `RxpGuideDetail.mapImages` now holds
  `glide://map/<uiMapID>.png` URLs (for the guide's zones and their continents that have an image) instead of base64
  data URIs read and sent over IPC on every guide open. The main process registers the privileged `glide` scheme and
  serves `glide://map/<id>.png` from the engine home's `data/gamedata/maps` (read-only, contract 5), else the
  development checkout's; the renderer's CSP allows `img-src glide:`. The page prefers these URLs over the engine's
  `/map/<id>.png`, so maps show with the coach stopped and one decoded image serves every guide. The map image is
  stretched to the map (`object-fit: fill`, as on the coach screen) instead of cropped. Guides page plan, phase 0
  item 5 (map-imagery recommendation 1-2, 2026-11-02).
- **Follow-up, what the player sees: a tight Guides page.** The page header (title, lede and ten wrapping filters)
  becomes one slim toolbar row: the guide group and guide pickers, the guide filter, a "Character" chip that opens a
  popover with race, class, Solo self-found, Group quests, Hardcore and "Show all <<" (and says when the step
  numbers are RestedXP's own), and Follow step / Done steps (moved off the map). The guide window has one scroller:
  the current step opens inside the step list instead of its own capped box, and the list is a listbox
  (Up/Down/Home/End/PageUp/PageDown, the current step focusable) that is built once per guide or filter and only
  re-opens the step that changed. The timeline is one row: its ticks are glyph buttons (the pins' own ! ? ⚔ ✦ ⌂ ☠)
  above the slider, clickable, with a dashed line at zone changes; the colour legend row is gone and the position
  reads "12 / 334". The map inspector is a compact card without a scrollbar. Narrow windows narrow the guide window
  and the map (a container query) instead of stacking the columns, so the page never scrolls down to 900 x 600.
  No IPC, file or state change. Guides page plan, phase 0 item 6 (explorer-ui recommendations 7 and 9, 2026-11-02).
- **Guides map: the route drawn, objectives, and everything on the map explained (contract 8, player-visible).** The
  map draws every leg between the listed steps' stops from `addon:rxpRoutes` (asked nearest the current step first,
  one request in flight per guide, asked again while legs are pending): walked legs over the walking map (the coach
  screen's case + line, gold dashes flowing on the leg being walked), flights dotted cyan with a landing circle,
  hearths / deathskips / boats a faint arc with ⌂ ☠ ⚓, legs not worked out yet a faint dashed straight line. A step's
  pin sits on its first stop that isn't one of RestedXP's path points; the current step's path points are small dots,
  only its stops are lettered (and its waypoint buttons list only those, with the same letters). Legs crossing a
  zone edge are cut there with a "from <zone>" / "to <zone>" marker that opens that zone; a continent tab ("◎
  Kalimdor") appears when the guide spans zones on one, shows zone outlines (click opens the zone) and the whole
  route, and opens by itself for a step reached by a flight. The current step's kill / loot / use targets from
  `addon:rxpFacts` (`targets`) draw under the pins, a shape per kind and a dashed outline around each cluster. Pins,
  waypoints, legs (yards, time, compass directions, areas passed, what it was routed over), loops and objectives
  (target, level, rank) all show a card on hover or focus and open in the inspector on click; Escape closes it. The
  map is built in persistent layers (`#rxpb-l-route`, `-loops`, `-objectives`, `-leg`, `-pins`, `-active`, `-hl`),
  rebuilt only when the guide, map, steps, routes or targets change. Follow centres on the current target; the map
  is a raster layer only while it moves (sharp at rest); Ctrl+wheel is left to the app's zoom. Timeline ticks share
  two rows and the least important give way in a crowded hub; the guide picker shows an opened guide's real step
  count (its #include steps expanded). No IPC, file or state change. Guides page plan, phase 1 items 1-7.
- **Guides page follows the live character (contracts 3, 4 and 8).** The light stream (`/events?lite=1`, `LITE_KEYS`
  and engine.ts `LiteState`) also carries `name`, `class`, `race`, `faction`, `guide`, `guide_group`, `step` and
  `rxp_settings` (added keys, about 200 B a second); `EngineState.rxp_settings` is typed `{ssf, group_quests} | null`
  and the renderer keeps them in `live.game` (session-bar.ts writes it, null while the coach is stopped). `rxp_import.py
  status` items (`RxpGuideSummary`) gain `rxp_name` / `rxp_group`, the guide's name and group as the addon puts them on
  page G (RestedXP's zero-padded name, `; = |` as ',', 40 bytes). The Guides page finds the character's guide by those
  (its group first, then faction, a levelling guide before a dungeon one, the longer first), and the live step by the
  engine's masks for the character's race, class and settings (the addon's SSF / group quests, hardcore from
  config.json), whatever the pickers show. Player-visible: a "Your step N" button in the timeline bar (key L; "Open
  your guide" when another is open; disabled with "<guide> isn't among your guides here" for a guide the page doesn't
  have; hidden without the coach), a white notch over the slider and a white edge on the live step in the list
  (`aria-current="location"`), and on the page's first show with the coach running the race / class / SSF / group /
  hardcore pickers take the character's values and open their guide at their step. Below 960 px the timeline's
  position text leaves the row (still read to screen readers) and the button shortens to its notch and number. No
  new IPC channel; no golden probe records the light stream. Guides page plan, phase 2 (live-sync recommendations 1-3,
  6, 7, 9-12; numbering through phase 0's masks instead of a new `number` command).
- **Guides page: Narrate** (no golden probe changes): a "Narrate" toggle beside Follow step and Done steps
  (`rxpb-narrate`, kept for the session like the other toggles, off at start; no setting). With it on and the voice
  heard, Walk speaks each step's instruction (RestedXP's markup removed, names never ids) and, when there's a real way
  to go, the way to the next step ("Head north-east about 300 yards, past Razor Hill, to Kaltunk", "Take the flight
  to Orgrimmar", "Hearth to Razor Hill", "Die and run back from the graveyard"), waits for the voice, then moves on;
  muted or Narrate off, Walk keeps its timer. Pause, picking a step, changing guide or leaving the page cut the
  narration. The way between steps uses the map's legs from `addon:rxpRoutes` as far as they've come back (no
  request of its own), else a straight line. The player-visible words are built in `desktop/src/renderer/rxp-narration.ts`. The narrator gains
  `speakAndWait(text, tag)`, `cancel(tag)` and `canSpeak()`; the coach's lines are unchanged (tests/test_narrator.py).
  `nav.ts` gains `whenLeft(page, fn)`. No IPC, file or state change. Player request (Guides narration, 2026-11-02).
- **Guides map: steps ahead fade (contract 8, what the player sees; no text, no golden probe).** Pins and route legs
  of the steps ahead of the current one are drawn at `aheadOpacity(steps ahead)` (`desktop/src/renderer/rxp-route.ts`:
  the current step and the next full, then 1/x down to 0.07), and each layer is restacked so the nearest steps draw on
  top. Pointing at a pin or leg, or at a step in the list, focusing or picking it, brings that step (its pin and the leg
  into it) back to full. Steps done keep their look. Player request (the rest of the guide crowded the map, 2026-11-02).
- **Guides page lights a quest's chain (contracts 4 and 8).** Additive to contract 4: an action of `rxp_import.py
  guide` (`RxpGuideAction`) that names several quests (`.turninmultiple 101,200`, `.acceptmultiple`) carries
  `quest_ids` (every quest, the first being `quest_id`); actions naming one quest are unchanged. Player-visible: the
  accept / turn-in / objective / quest-collect badges in the step list are buttons, and their labels take the
  knowledge base's quest name (`addon:rxpFacts` `quests[*].name`) when glide.db has none. A click lights the quest's
  chain (back and forward through `quests[*].prev` / `next`, only the guide's quests; the quest alone without a
  knowledge base): its steps stay bright on the map (a gold connector through them in step order, "continues in
  <zone>" / "from <zone>" where it crosses zones, opening that zone), on the timeline (gold diamonds over the slider)
  and in the list, the rest fades. A slim chain bar over the step list says "Quest N of M in a chain", each quest's
  steps as buttons (RestedXP's number, ! ◎ ?; a click goes there), "goes on after this guide" when the last quest's
  follow-up isn't in it, and with the knowledge base what a skip loses (`lost_if_skipped`, the coach's rule): "Skip it
  and you lose X (step N) and Y (next guide)." / "Skipping it loses nothing later in this guide or the next." The ×,
  Escape or the badge again turn it off; a guide change drops it. DOM-free logic in `rxp-quests.ts`
  (`tests/test_rxp_quests.py`). Guides page plan, phase 3.
- **Guides page: before you start (contract 8).** Player-visible: a slim row at the top of the guide window lists
  what the guide has the picked character buy, buy at the Auction House or take from / put in the bank
  (`addon:rxpFacts` `prep`, which needs no knowledge base), from the current step on: "Before you start" while every
  item is ahead, "Still to get" once some are behind. Each chip shows ◈ / ⚖ / ⇡ / ⇣, the count and name ("10×
  Refreshing Spring Water") and where it's needed ("for step 15", "for the next guide"); its card says the step, who
  sells it and for how much (with the knowledge base) and the quest it's for; a click goes to the step that gets it.
  Items follow the Character picks and settings (the step's and the action's masks: an Auction House step drops with
  solo self-found); the same need offered at two steps counts once. A row too long for the window ends in "+N",
  which opens the rest ("Less" closes it). Collect badges take the prep list's item name when glide.db has none. No
  IPC, file or state change. DOM-free logic in `rxp-prep.ts` (`tests/test_rxp_prep.py`). Guides page plan, phase 4.
- **Guides page: time, XP and hazards on the timeline (contract 8).** `addon:rxpFacts` is now asked with the picked
  class and race (the existing optional arguments; one answer per guide and pick, the last ask winning), so the
  facts carry the coach's plan for that class. Player-visible: beside the timeline position the time to the next level
  at the coach's pace ("37 min to 7"; its card names the step and says "roughly" when the knowledge base has another
  version of the guide; hidden below 1150 px and without a class); while the slider moves, a bubble over it says the
  time and XP from where the scrub started ("+46 min · +3,510 XP", or "level 21 → 23" when a level is gained;
  "−" scrubbing back). Dangerous stretches (a hazard at the step, or the coach's risk at 0.6 or more; one safe step
  joins two) are red bands over the slider whose card says the hazards met ("Steps 12–15: Hogger, level 11 elite:
  patrols the road, calls for help"); a click goes to the stretch's first step. On the map the current step's hazards
  are a warning sign: on the way in for what's met on the road, beside the pin for its own elite target or a
  dangerous quest; hover or click says it in a line and where. The opened step lists up to two hazards the same way.
  The Character popover adds "Pick a class to see how long to each level at the coach's pace." when the knowledge base
  has plans and no class is picked. Without a knowledge base none of this shows. No IPC, file or state change.
  DOM-free logic in `rxp-plan.ts` (`tests/test_rxp_plan.py`). Guides page plan, phase 5.
- **Follow-up, contracts 3, 4, 5, 6 and the knowledge base (all additive): game icons for the Guides page's targets.**
  The player asked for images from the WoW client in place of the target glyphs (2026-11-02); the client has no
  creature portraits (they are drawn live from 3D models), so a target shows what the client does have
  (`wowfiles/target_icons.py`). The game-file refresh gains a stage `icons` (`tools/extract_maps.py --only icons`,
  in the default list after `spells`): it reads Creature, CreatureFamily and Item (new vendored `tools/dbd/Creature.dbd`,
  `CreatureFamily.dbd`, `Item.dbd` from WoWDBDefs, which knows WoW Forever's 1.60.1 layouts), checks them (twelve
  starting-zone creatures with known type and family, the Hearthstone's and the boar and wolf families' icons, icons
  that are real files) and leaves out any table that doesn't check out. It writes `data/gamedata/target_icons.json`
  (`{version, items {itemID: fdid}, families {familyID: fdid}, types {"Beast"|...: fdid}, named {vendor, repair,
  trainer, profession, flight, innkeeper, banker, stable, mailbox, auctioneer, quest_available, quest_turnin, elite,
  rare: fdid}, raw [fdid shown untrimmed], how, problems}`), `data/db2/Creature.csv` (`ID, CreatureType,
  CreatureFamily, Classification`) and copies of the three tables in `data/gamedata/db2/`, and decodes the family,
  type and role icons into `data/gamedata/icons/<fdid>.png` (the same cache as the buff icons). The knowledge build's
  DB2 import reads `Creature.csv` and fills `creature.type` ("Beast", "Humanoid", ...: the coach's `target_type`
  rules start to apply), `creature.family` ("Boar", ...) and `creature.rank` where kb had none. A new
  `rxp_import.py icons` subcommand (`{"fdids": [...]}` on stdin) decodes icons into the cache and answers
  `{"ok": true, "ready": [...], "failed": [...]}`. Guide facts gain `icons` (`"item:<id>" | "spell:<id>" | "npc:<id>"
  | "role:<name>"` -> fdid, `{}` before the refresh read the client) and each target an `item` (what it is looted
  for); `RxpGuideFacts.icons` and `RxpFactsTarget.item` are optional in `desktop/src/shared/ipc.ts`. The main process
  serves `glide://icon/<fdid>.png` from the engine home's (else the checkout's) `data/gamedata/icons`; a miss is
  decoded by one batched `rxp_import.py icons` run (`desktop/src/main/game-icons.ts`), so icons show with the coach
  stopped; an icon that can't be had answers 404 and the page keeps its glyph. The renderer's mapping is
  `desktop/src/renderer/rxp-icons.ts` (`tests/test_rxp_icons.py`; also `tests/test_target_icons.py`,
  `tests/test_game_icons.py`). Nothing from the client is shipped.
- **Follow-up, what the player sees: the Guides page draws the game's icons.** Map objectives show their target's
  icon (a creature's beast family or type, the item an object gives) once per cluster of spawns, in a frame of the
  role's colour (gold for an elite), the other spawns staying dots; step pins show the game's quest marks, the flight
  master, the Hearthstone or innkeeper, a fight's creature, a vendor's or trainer's mark; step badges, the hover card
  and inspector, and the "Before you start" row show the item, spell, creature or role icon. Each icon replaces its
  glyph only once its image has loaded; with no client data, or an image that fails, the glyph stays.
- **Addon 0.17.4, contract 4 additive and files on disk: only the latest RestedXP guide stays.** Adding a guide on
  the Addon page added a pack beside the earlier ones, so the page listed every pack, the coach learned them all and
  RestedXP got them all. Now `rxp_import.py add` replaces the account's earlier packs (`replace`): they leave
  `data/rxp_imports/imports.json` (so the knowledge build, the Guides page and the Addon page have the latest only),
  and a store with several packs per account is cut to each account's newest at the next `sync`/`status`
  (`latest_only`). A kept record gains `drop` (`[[group, name], ...]`: the replaced packs' guides it doesn't have,
  the name as RestedXP keys it) and, when it must reach RestedXP again, `hand` (the hand-off id, `<id>.<time>`; the
  id until then). `Data/RXPImports.lua` packs gain an optional `drop = { { group, name }, ... }` and are written under
  their hand-off id, which `GlideRecorderDB.rxpImports` notes. Once RestedXP took a whole pack, `RXPImport.lua`
  deletes from RestedXP's account-wide guide cache (`RXP.db.profile.guides`) the older copies of each guide it cached
  anew (`ImportGuide` caches a new copy every time; an older one beside it loaded instead at random at the next login)
  and the guides on `drop`; they leave RestedXP's guide list at the next /reload (RestedXP's own `RemoveGuide` finds
  nothing in this version). A pack a replaced one reached RestedXP before is handed over again under a new hand-off
  id, so the swap happens in the game; a pack RestedXP didn't take whole leaves the cache alone. `addon:rxpImport`
  answers `replaced` (how many guides the replaced packs held; `RxpImportResult.replaced` optional) and the page says
  "It replaces the N guides you added before". An older addon reads the new file as before (no `drop`). The player
  asked (2026-11-04).
- **Roleplay: the coach plays a character the player asks for (contracts 2, 4, 5 and 8; all additive).** For long
  sessions the player can say or type "talk like a pirate", "be my drill sergeant", "you're Thrall now" and the coach
  plays it with full commitment until told to drop the act. The model takes the request in any wording (a new Roleplay
  paragraph in `coach_prompts.SYSTEM`, and one line under the style rules lifting "plain and direct" while in
  character): it answers in character and adds `PERSONA: <brief>` / `VOICE: <voice direction>` lines (or `PERSONA:
  off`), stripped like WATCH lines (`companion/persona.py`, `Coach._directives`; `EarlySpeech` stops at them). They
  are kept in three new settings (contract 5): `llm.persona` and `llm.persona_voice` (live text: `tuning.apply` now
  takes live text keys; the state's `tuning` is unchanged) and `llm.persona_word_factor` (1.6: word limits stretch
  in character). While one is set, every call gets the persona block as its own cached system block after the
  memory's, and the request ends with a stay-in-character line. The engine's `speak` event (contract 2) gains an
  optional `style` (`SpeakEvent.style`), the narrator passes it on as `tts:synth`'s new optional second argument
  (contract 4), and the OpenAI voice performs it as its `instructions` in place of the configured ones; other voices
  speak the words as before. Player-visible (contract 8): the in-character lines themselves, an `info` chat line
  "Coach persona: <start of the brief>" / "Coach persona off", and a Roleplay group under Settings > Coaching
  (Character, Voice direction, Extra room for the act); `golden.py record desktop` re-recorded for those three
  fields alone. The reflection prompt tells the memory not to note a role. Offline (no model) there is no persona.
  Pinned by `tests/test_persona.py`.
- **Quest bursts make one summary callout (contracts 5 and 8; additive).** Turning in and picking up several quests
  at a hub used to come out one by one: the first event fired alone, the rest every `min_interval`, and a shared
  batch was told to keep only its most important trigger. Quest events (`coach.QUEST_KINDS`: `quest_turned_in`,
  `quest_removed`, `step_changed`, `reward_choice`, `turnin_decay_soon`, and the new coach-raised
  `quests_accepted`) now wait in `Coach._take_batch` until none came for `llm.settle_s` (2.5 s; at most
  `llm.settle_max_s`, 8 s, after the first) and while accepted quests are still gathering for their briefing; an
  urgent trigger meanwhile goes out alone, and a settled burst of two or more skips `min_interval`. A burst of pickups the briefings hold (`Briefings.hold`, its unspoken
  chat line unchanged) joins the burst as `quests_accepted` `{count, quests}` (it shows in the state's
  `recent_triggers`). A batch with two or more quest events gets `llm.summary_words` (30) and a note asking for one
  summary; the style rule in `coach_prompts.SYSTEM` makes that exception. New settings (contract 5): `llm.settle_s`,
  `llm.settle_max_s`, `llm.summary_words`, shown under Settings > Coaching > Callouts (contract 8; `golden.py record
  desktop` re-recorded for those three fields alone). Offline callouts gain "Turned in N quests." and "Picked up N:
  <names>.". The player asked (2026-11-05). Pinned by `tests/test_quest_burst.py`.
- **The quest log holds 40 on WoW Forever, not Classic's 20 (contracts 6 and 8; kb.db content).** The coach kept
  telling players their log was full at 20. The 20 came from the knowledge files, never updated after the community
  note n:20261021-quest-log-40: heuristics `route.quest-log-near-cap` (now 38+, was 18+) and `route.quest-log-full`
  (now 40, was 20) with new rationales, the glossary's "quest log cap" (40 on Forever; it went into the coach's
  system prompt), `quest_log_count`'s meaning, and two evals that expected 20. And the model only ever saw 20
  quests: `prompt_context` cut them at `[:20]`; now `QUEST_LOG_CAP + 5` (a full log plus the step's), and the
  player section says `quest_log: "<n> of 40 quests"`. rules.json and glossary.json are hashed kb.db inputs, so
  kb.db rebuilds with them. The addon was right all along (its pages carry the whole log). The player asked
  (2026-11-05). Pinned by `tests/test_quest_log_cap.py`.
- **In character, the coach speaks up unprompted and builds a bond with the player (contracts 2, 3, 5 and 8; all
  additive).** While a persona is set, `companion/banter.py` (a coach-thread ticker) has the fast model say one aside
  about every `llm.banter_minutes` (8, randomised x0.6-1.4), only in a calm moment: a live frame, no pending
  trigger, question or briefing, nothing said for 60 s, not in combat or dead; the model may answer SKIP. Asides are
  a new feed message kind `banter` (spoken, never urgent; in `msg_kinds.COACH_SAID`/`RECENT` and the knowledge
  side's `vocab.COACH_SAID`; the coach screen labels it "Coach"). The bond: the memory gains a sixth kind,
  `rapport` ("Your bond with them"), and `coach_memory.json` a `bond` object `{hours, sessions, asides, replied}`
  (contract 5, files on disk: an older engine ignores it); the request carries the bond's line and stage (new,
  getting to know each other, familiar, old friends: 3 / 15 / 50 hours) with the memory's rapport notes.
  Reflection learns rapport from how the player reacts (`REFLECT_PROMPT`; unanswered asides are marked "(no
  reply)", and four asides are enough to start a reflection). The player's say: a reply's `BANTER: less / more /
  off / on` line (the Roleplay paragraph of `coach_prompts.SYSTEM`) scales `banter_minutes` by 1.6 or switches
  `llm.banter`. New settings (contract 5): `llm.banter` (live; the state's `tuning.toggles` gains it, contract 3),
  `llm.banter_minutes` (live), `llm.banter_words` (25), under Settings > Coaching > Roleplay (contract 8;
  `golden.py record desktop` re-recorded for those three fields alone). Chatting: a question within 180 s of an
  aside or a chat reply (the coach's last line) is a chat (`Banter.in_chat`): the fast model, the voice lookahead,
  no "max 25 words, answer first" note but `banter.CHAT_NOTE` (react, encourage, end on a hook; answer a game
  question first), `llm.banter_reply_words` (35) and kind `banter`, so the player's next reply carries it on; a
  reply's `CHAT: end` line (the player wrapped up) makes it a plain `answer` again. Answers and banter both count as
  the conversation's replies (`coach.REPLIES`: RECENT CONVERSATION keeps the whole chat, `answer_once` returns
  either). The player asked (2026-11-06). Pinned by `tests/test_banter.py`.
- **The character feels there: moments, a diary, a mood, its own story, per-line delivery, learned timing (contracts
  2, 3, 5 and 8; all additive).** `companion/banter.py`: asides now come mostly from moments `Banter.observe` sees
  in every frame (`Coach.push`): deaths and death streaks (3 in 30 min), level-ups, close calls (a fight survived at
  15% health or less), elite / rare kills, first visits to a zone, long grinds, flights, the hearth, each with a
  chance and a cooldown (`MOMENTS`), spoken 8 s after the coach's own line; a greeting at the first live frame of a
  session (picking up from the diary and the time since `bond.last_seen`) and a goodbye when frames stop for 45 s
  after 10+ minutes together; between moments the timer, 0.4x in downtime (flying, dead, eating or drinking) and
  scaled by the replies per activity (`bond.by_activity`: >=50% answered 0.7x, <15% 2x, after 4 asides). The mood
  (moments push it, it settles after 20 min) rides on every request's stay-in-character line; each aside asks for
  a weighted shape (`SHAPES`) and names overused openers and question endings (`variety_note`), chat replies too.
  `coach_memory.json` gains `diary` (newest 80 moments with their session) and `characters` (by name: brief,
  voice, lore, newest 12), `bond.last_seen` and `bond.by_activity` (contract 5, files on disk; an older engine
  ignores them). Persona lines: `NAME:` (setting `llm.persona_name`, live text), `LORE:` (kept per character and
  sent as "YOUR OWN STORY SO FAR"), `PERSONA: recall <name>` (the memory block lists "CHARACTERS YOU HAVE PLAYED"),
  and a leading `TONE:` line on every reply in character: stripped, read early by `EarlySpeech.tone`, put on the
  message as `tone` (`CoachMessage.tone`, contract 3) and added to the speak event's `style` ("<voice>. This line:
  <tone>."). Reflection also sees the diary. Pinned by `tests/test_banter_moments.py` (and test_banter, test_persona).
- **The coach says when a Glide update is out, and reminds every 10 minutes (contracts 5 and 8; additive).**
  `desktop/src/renderer/update-voice.ts` (`UpdateVoice`, wired in `updates.ts`): on `update:status` turning
  `available` the narrator says "A Glide update is out: version X. Download it from the footer button when it suits
  you.", on `ready` "Glide X is downloaded. Restart to update from the footer button when it suits you."; while it
  waits, every 10 minutes "Reminder: Glide X is still waiting to be downloaded, footer button." / "Reminder: restart
  Glide to finish updating to X, footer button." (paused while downloading; cleared on none, idle, disabled, error
  or checking; a newer version starts over), in the character's voice when one is played. The app setting
  `updateVoice` (app-settings.json, default on; Settings > General "Say when an update is out") switches the spoken
  lines off; the toasts and the footer button stay. The player asked (2026-11-06). Pinned by
  `tests/test_update_voice.py`; `golden.py record desktop` re-recorded for the new field.
- **The coach remembers like a friend: the story so far (contracts 5 and 8; additive). Social Phase 1.**
  Reflection (`Reflector`, `coach_prompts.REFLECT_PROMPT` and its schema, `coach_tools.apply_reflection`) also
  writes a "chapter" of each stretch together (what happened, the jokes, what the player shared) with its topics,
  and opens or closes "threads" (promises, questions left hanging, plans); a session's end (the goodbye) asks for
  one at once (`Reflector.soon`). `coach_bond.json` gains `chapters` (16 kept whole; past that the oldest 8 fold
  into an era: their first sentences and topics), `eras` (24), `threads` (12, ids `t<n>`, `next_thread`) and
  `convo` (the conversation's last 60 lines, kept every minute and at the goodbye, restored into `Coach.convo` at
  start); characters gain storage for the next phase (`samples`, `favorites`, `plot`, `lines`, `role`). Every part
  is checked on load (times finite and not in the future), keys of the file this engine doesn't know are written back
  as they were; "forget everything" clears them, the coach's live conversation and what it recalled, and sets
  `cleared_t` (nothing older is kept again). In character, `companion/recall.py` picks what the moment brings back
  (the diary, the chapters and eras, the fact / goal / rapport notes; matched on whole words of the player's words or
  the moment, never the coach's own lines; the place and the target weigh a little, and bring a memory back alone
  only in a quiet-moment aside; relevance, then recency with a 10-day half-life; what came up lately only when
  nothing else fits; never the moment itself or what the prompt already holds): asides and questions get a "THIS
  BRINGS BACK" section, asides and chats the latest chapters and the open threads, questions what the last lines drew
  on ("why did you bring that up?"). One reflection runs at a time; the goodbye's only when you talked. Pinned by
  `tests/test_story_memory.py`.
- **Talk over the coach, and each line's tone acted (contracts 2, 4 and 8; additive). Social Phase 3 (voice).**
  Holding push-to-talk works like talking over someone on a call. Held 150 ms (a brush of the key isn't talking:
  `app.BARGE_S`, coach.ts `BARGE_MS`), the coach screen's narrator stops the line being said and drops the queued
  ones (`speech.bargeIn()`), the engine's own speech process does the same (`Voice.hush()`, its "!" cancel-all, never
  waiting long on its lock), and the reply on its way stops (`Coach.barge()`: its early speech, and the final
  message's `speak` goes false). While the key is down an answer waits and is said at key-up, anything else is
  dropped, and a streamed sentence isn't said or counted (`speak_early` -> False); key-up is "transcribing", or
  "idle" with `PushToTalk.key_down` false (the previous press's "idle" can come mid-press); a hold past 120 s counts
  as released. The Guides walk pauses on the step talked over (`speech.bargedSince`), a waiting `speakAndWait`
  resolves. The engine's `speak` event (contract 2) gains an optional `tone` (`SpeakEvent.tone`), the in-character
  line's delivery, on a reply's first sentence only (it stays folded into every sentence's `style`); a blank tone
  counts as none. `tts:synth` (contract 4) takes it as an optional third argument (`(text, style?, tone?)`, capped
  at 200 characters in main). An ElevenLabs model whose id contains "v3" (`eleven_v3`, now in Settings > Voice's
  ElevenLabs model menu) performs it as an inline audio tag, `[<tone>] <text>` (up to 6 lower-case words of letters
  of any script, apostrophes and hyphens; a longer tone gets no tag: `tts.v3Text`); other ElevenLabs models, OpenAI
  (its `instructions` are still `style`), Azure, Google and Windows voices are unchanged. `golden.py record desktop`
  re-recorded for the new `v3Text` export of main/tts.js. Pinned by `tests/test_voice_barge_in.py` and
  `tests/test_voice_tone.py`.
- **The coach chat follows the newest message again (contract 8, what the player sees; a fix).** It followed only
  when the scroll stood at the bottom as a message came, so once the feed's size changed under it (the window or the
  app's frame resized, longer lines wrapping, old messages trimmed) it stayed short of the bottom for good. Whether it
  follows is now kept (`feedFollows` in dashboard.js) and only the player's scrolling changes it (wheel, touch, keys,
  the scroll bar); other scrolls and resizes put it back on the newest message; right at the bottom, or pushing down
  there, follows again. The player asked (2026-11-07). Pinned by `tests/test_dashboard_page.py` (resized, read up,
  back down).
- **Recall by meaning, optional (contracts 5 and 8; additive). Social Phase 1, deeper.** `companion/embeddings.py`:
  with `llm.recall_meaning` set (`off` by default; `openai` with OPENAI_API_KEY, `gemini` with GEMINI_API_KEY, or
  `ep:<endpoint id>`, an OpenAI-compatible custom endpoint such as a local Ollama, which needs `llm.recall_embed_model`)
  recall also brings a memory back for what the moment means (`recall.closeness`: a memory counts when its cosine
  closeness to the moment stands 0.1 above the median over all memories and over `llm.recall_min_similarity`, 0.3).
  The memories are embedded in the background (`Banter._warm`: every 10 minutes in character, a minute after a failed
  one; batches of 16, 45 s each) and kept beside the memory in `coach_embeddings.json` (contract 5, a new file on
  the player's disk: `{source, model, vectors: {sha1: base64 float32}}`, only the current memories' vectors,
  dropped with another source or model and by "forget everything"); a line embeds only its own words (4 s at most,
  kept in memory, never on disk), and only once something is embedded. Gemini gets RETRIEVAL_QUERY / DOCUMENT task
  types and the key in a header, nomic models their search prefixes. An odd, failing or missing service gives
  nothing and recall goes by words (a failing query backs off a minute). Settings > Coaching > Roleplay: "Remember by
  meaning", "Embedding model", "How close in meaning" (contract 8; `golden.py record desktop` re-recorded for them).
  The player asked (2026-11-07). Pinned by `tests/test_recall_meaning.py` (a fake embeddings server: no paid API).
- **A new Glide, told with what's in it (contracts 2, 4, 5 and 8; additive).** release.yml writes the version's
  What's new (its changelog.json entry, from next-release.js) into the release's notes: "Glide <v>: <title>", the
  items as a list, then "source: <sha>" as the last line (the workflow and next-release.js still read it back). The
  app reads them from electron-updater's releaseNotes (`desktop/src/main/release-notes.ts`: GitHub's HTML, markdown
  or a list per version) into `UpdateStatus.title` / `.notes` (contract 4, `update:status`). The update notice hands
  its announcement to the engine's new `POST /announce` `{what: "available" | "ready", version, title, notes}`
  (contract 2; token-checked like every POST; `dashboard.announce_route`): `Coach.announce_update` queues it on the
  coach thread (never over another line), where the fast model says it in the coach's own voice (in character when
  it plays one), the one to three things the player would care about, in `llm.announce_words` (45, contract 5);
  without a model, or with no notes, a plain line names the first items. Without the engine, the window says that
  plain line itself (`whatsNew`). The 10-minute reminders stay short; "Say when an update is out" off silences all
  of it. The player asked (2026-11-07). Pinned by `tests/test_update_notes.py` and `tests/test_update_voice.py`.
- **A character that stays itself (contracts 5 and 8; additive). Social Phase 2.** A new character gets its own
  sample lines and two story threads once, in the background, consistent with what it already said about itself
  (`banter.Banter._seed`, `persona.seed_request`, the fast model; retried when a character set in Settings is first
  kept); an aside the player answered becomes one of its favourites (only while that character is still played).
  They ride in the persona block as "HOW YOU SOUND", with "YOUR OWN STORY THREADS", "WHO THEY ARE TO YOU" (a reply's
  `ROLE:` line) and "THE SCENE YOU'RE PLAYING TOGETHER" (`SCENE: <premise>` / `SCENE: end`: the character's own,
  lapsing after 6 hours unplayed; a role or scene said again in other words changes nothing; "Scene: <premise>" /
  "Scene ended" in the chat on a real change). The phrases the character keeps saying (3 words in 3 or more of its
  last 60 asides and chat replies; never the coaching's words or a name) are named to rest on every request in
  character. Persona directive keywords other than PERSONA must be upper-case (an answer's "Role:" line stays the
  answer's); one written mid-line is split off and never spoken. A character's first 8 LORE facts are never
  dropped; greetings no longer ask for new lore. `coach_bond.json` characters fill `samples`, `favorites`, `plot`,
  `lines`, `role` and gain `scene`, `scene_t` (contract 5). The player asked (2026-11-07). Pinned by
  `tests/test_character_depth.py`.
- **Presence across the climb (contract 8; additive). Social Phase 4.** Every 10 levels is a milestone (an aside
  with twice the words, looking back on the stretch: `MOMENTS["milestone"]`); level 60 is the end of the climb, told
  with THE ROAD SO FAR (every era and chapter, oldest first, each in brief) in four times the words. Asides the player
  lets pass in a row stretch the wait between quiet-moment asides (x1.5 each, x4 at most; a reply resets it:
  `Banter.unanswered`). Greetings say how long the two have been at it (days, sessions; a week to the day is marked).
  The player asked (2026-11-07). Pinned by `tests/test_presence.py`.
- **The Memory page: search, and the characters (contracts 2 and 8; additive).** `GET /memory` (and every `POST
  /memory` answer) also carries `characters` (each kept character: `name`, `brief`, `voice`, `role`, `scene`, `lore`,
  `plot`, `last`; most recently played first) and `playing` (the one played, by name, or ""); `POST /memory` gains
  `{"op": "character", name (exactly), field: "brief"|"voice"|"role"|"scene"|"lore"|"plot", text, index?, was?}` (one
  line: "" clears it or removes the item; `index` null adds to its past or its threads (one like it there already:
  nothing); `was`: the item's words when the page was drawn, found by them if the list moved, else refused; the played
  character's description and voice change the act too) and `{"op": "character_forget", name}`. The Memory page
  (contract 8) gains "Search memories" (Ctrl+F; every word typed, in the notes and the characters' lines, marked; Esc
  clears) and "Characters the coach has played": each one's lines to edit or clear, "Add to <name>'s past", Forget
  (pressed twice). The player asked (2026-11-07). Pinned by `tests/test_memory_characters.py` and
  `tests/test_memory_page.py`.
- **What an older version left is taken out at start (contract 5).** Settings no longer used (`settings.RETIRED`)
  are dropped from the config the engine reads and taken out of config.json when the engine starts (written only
  then; never in sim). `coach_bond.json` is read without fields this version doesn't keep, without a second character
  that version kept beside the one played and that was never played itself, and without lines its replies gave
  another speaker (`[Name] ...`, out of the characters' lines and the kept conversation); it is saved once, cleaned,
  and a clean one is never rewritten. The player asked (2026-11-07). Pinned by `tests/test_older_leftovers.py`.
