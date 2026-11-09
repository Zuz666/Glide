# Glide: technical guide

This is the how-it-works reference for Glide. For the overview, see [README.md](../README.md); for development (running the engine, tests, releases), see [CONTRIBUTING.md](../CONTRIBUTING.md).

Glide is an AI leveling coach for WoW Forever. It merges the community's quest data and what you record in game into `data/glide.db`, builds a knowledge base from it (`data/kb.db`: world facts, game mechanics fitted to Forever, every RestedXP step analysed for XP, time, dependencies and alternatives, expert heuristics), reads your live game state through the Glide addon, and coaches you in real time.

## Desktop app

The desktop app is the main way to run Glide. It's a Windows installer, published on the releases of the public [Fermin7c1/Glide-releases](https://github.com/Fermin7c1/Glide-releases) repo, and installed apps update themselves. It holds the coach screen, settings, API keys, model endpoints, the addon installer, the overlay and voice in one window, and ships with its own Python. See [desktop/README.md](../desktop/README.md).

The command-line way below (`start-glide.cmd`) is the development path. It runs the same engine from a source checkout.

## What's here

| Path | What it is |
|---|---|
| `desktop/` | The desktop app (Electron, TypeScript). It runs `companion/app.py --app` as its engine. |
| `addon/Glide/` | The in-game addon. It records quests, NPCs, XP and your route while you play, sweeps every quest ID, copies your RestedXP guides, and streams live state to the engine. Before 0.8.0 this was two addons, GlideRecorder and GlideLink. |
| `companion/` | The engine: the Python process that reads the addon's strip, tracks state, runs the rules and the coach, and serves the coach screen (`companion/app.py`; the screen's files are `companion/dashboard.py` and `companion/static/`). The folder keeps its old name. Its settings (defaults, ranges, which apply live) are listed once in `companion/settings_manifest.json`; `companion/glideconfig.py` reads `config.json`; `companion/strip_spec.py` lists every key of the addon's strip (`docs/STRIP.md` is generated from it). |
| `tools/archive.ps1` | Standalone archiver, for when the engine isn't running (development only, not in the installer). It copies SavedVariables and `Cache\WDB` into `data/archive` whenever they change. |
| `tools/wdb.py` | Decodes the client's quest/creature/object cache, which holds what the server sent (the reader is `wowfiles/wdb.py`). |
| `wowfiles/` | Readers for the game client's own files (CASC storage, DB2 tables, textures, map tiles, the caches, the walking map's format), shared by the engine, the knowledge build and `tools/`. |
| `tools/build_db.py` | Merges all sources into `data/glide.db`. |
| `knowledge/` | The knowledge layer: `build_kb.py` builds `data/kb.db` from glide.db, the RestedXP addon's data files, your recordings and logs. `knowledge/analyzer/` is the route analysis (its `route/` package does the per-step work), `knowledge/api/` is what the coach reads and its tools, `knowledge/mechanics/` the fitted game rules. See "The knowledge base" below. |
| `build-kb.cmd` | Rebuilds the knowledge base by hand (the engine does it in the background when needed). |
| `tools/kb_report.py` | Prints what the knowledge base knows, what it doesn't, and whether it is stale (development only). |
| `tools/build_seed.py` | Builds `data/seed/seed.json.gz` from community datasets. It needs `lupa` (development only). |
| `data/seed/seed.json.gz` | Community data: ForeverGuide (quests, NPCs, objects and items, from Questie with a Forever overlay), ForeverQuestMarker's lists of quest IDs new or changed in Forever, and the free RestedXP Forever guides. |
| `config.json` | The location of the WoW client and the engine's settings (not in the repository; API keys are never written to it). |
| `requirements.txt`, `requirements-dev.txt` | The pinned Python packages: what the engine uses beyond the standard library, and what the tests and linters need. |
| `docs/` | `TECHNICAL.md` (this guide), `REFACTOR-CONTRACTS.md` (the interfaces that must not change without a recorded decision), `STRIP.md` (the strip's keys, generated), `RECORDER.md` (what the recorder saves). |
| `CONTRIBUTING.md` | How to run the engine, the tests, the golden masters and the linters, and how releases happen. |
| `LICENSE`, `THIRD_PARTY_NOTICES.md` | Glide's license (GPL-3.0 or later), and the sources and licenses of the data and assets that aren't Glide's own. |

## In game

The addon is installed at `<client>\Interface\AddOns\Glide` (the app's Addon page installs and updates it). Its settings and recordings are saved in `SavedVariables\Glide.lua`. `/glide` opens the Glide window with every switch (see below); `/glide help` lists the chat commands.

| Command | What it does |
|---|---|
| `/glide scan all` | Asks the server about all 6,633 quest IDs in the addon's list. New Forever IDs go first. It runs at up to 12 requests per second, backs off when the server stops answering, pauses in combat, and skips IDs already cached, so you can run it again after a logout. |
| `/glide scan new` | Scans only the 2,785 IDs believed to be new in Forever. |
| `/glide scan range 90000 99999` | Scans any ID range. |
| `/glide scan status` / `stop` | Shows progress / stops the scan. |
| `/glide rxpdump` | Copies the RestedXP guides you own, including premium ones, into the recorder. Personal use only. |
| `/glide status` | Shows what has been recorded this session. |
| `/glide save` | Writes everything to disk now (this reloads the UI). |
| `/glide repair` | Repairs everything at the open merchant. Auto repair is on by default: opening a merchant who repairs repairs all your gear with your own gold, when you can pay the whole bill. `/glide repair off` or the Glide window (`/glide`, or a left-click on the minimap button) turns it off. |
| `/glide trash` | Deletes the cheapest RestedXP junk in your bags now; `/glide trash list` shows the pick. |
| `/glide train` | Opens the "New at your trainer" window (see below). |

Recording runs without any command. It captures:

- **Quest text:** description, objectives, progress and completion text, and rewards for every quest window you open.
- **Givers and turn-ins:** the NPC or object ID and its position.
- **Gossip offers:** which quests each NPC offers, even ones you don't take.
- **Objective locations:** where each objective advanced and which mob counted for it.
- **Kills and XP:** XP per kill, including rested bonus.
- **Route timeline:** an XP timeline, a position trail every 3 seconds, deaths, level-ups, hearths, flights, and zone changes.
- **Services:** vendors, trainers and flight paths.
- **Blizzard's waypoints:** the server's quest waypoint arrows.

## After a session

1. Log out, or use `/glide save`. The client writes `Cache\WDB` and SavedVariables when you log out.
2. The engine merges the new recordings into `data/glide.db` and rebuilds the knowledge base in the background while you play (see **Fast start**), and switches over when they're ready. By hand: `python tools\build_db.py` then `build-kb.cmd`. When nothing it reads has changed, `build_db.py` says "up to date" and skips the rewrite (`--force` rebuilds anyway).

## Community intelligence

The coach knows what the WoW Forever community finds: patch and hotfix changes, XP and rested findings, route tricks, class leveling power, dungeon changes.

- **Where the scan runs.** A scheduled task on the project owner's own PC scans the community several times a day. It doesn't run in the app or on players' PCs, and nothing in the repository publishes its results. Its method and note format are in `knowledge/community/README.md`.
- **Where it looks.** Blizzard's development notes, forum posts and news. The Arctium blue-post tracker. The community bug tracker, where Blizzard designers answer. News and guide sites, wikis and patch-note aggregators. Forum discussions. Reddit, YouTube and X threads, through search and the articles that quote them. Datamining. New addons and data projects. It follows each find to its primary source and to a second, independent one. Every source is listed in `knowledge/community/sources.json`, and runs add new ones.
- **What it keeps.** Leveling-relevant notes go in `knowledge/community/intel/<date>.jsonl`: XP, rested and camps, quests, dungeons, respawns, routes, class leveling power, phases. Each note is one checkable claim with its levels, zones and classes, its sources, and a status: reported, corroborated, official, contradicted or superseded. A readable digest goes in `knowledge/community/digests/<date>.md`.
- **How notes reach players.** The notes are published as a feed in `feed/` of the public `Fermin7c1/Glide-releases` repo. Every 30 minutes (`community.feed_minutes`) the engine reads the feed's manifest. A new feed is downloaded over https, checked against its SHA-256, kept as `data\live\community_feed.json.gz`, and its notes are written into `data\kb.db` at once, so new notes arrive without an app update. The engine only reads the feed (`companion/community_feed.py`). `community.feed` set to `false` turns it off.
- **Data refresh.** Once a day the owner's scan also pulls the community data repos, rebuilds `data/seed/seed.json.gz` and `addon/Glide/Data/QuestIDs.lua` when one moved (`tools/refresh_community.py`, then `tools/build_seed.py`), and records what changed in `data/seed/seed_report.json`. That includes the quest IDs datamined from the Forever client by ForeverQuestMarker. These reach players with the next release, not through the feed.
- **How the coach uses it.** Every callout gets the notes that match your zone, level or class. Questions can search all notes with the `community` tool. The coach cites them as `n:<id>` and says when something is only reported. The coach screen's Community section lists the newest notes with their status and source.
- **Limits.** Discord needs a logged-in account and isn't read. Discord findings arrive only when a public source reports them. Reddit and YouTube can't be fetched by the scan, so they're found through search.

## When RestedXP updates

1. **In game, right away:** after the WoW restart, Glide reads the new version's current step, training list and flight paths directly.
2. **Guide capture, at the first login on the new version:** 15 seconds after login, out of combat, Glide copies RestedXP's guide cache, including premium guides, the same as `/glide rxpdump`. It prints one line saying so. The texts are written to disk at logout or `/reload`.
3. **Next engine start (background rebuild):**
   - `build_db.py` reads the free Forever guides from the installed addon (`RXPGuides\Guides\forever`), falling back to the seed snapshot only when the addon isn't readable. For each guide it keeps the newest text, whether from the addon files, a capture or RestedXP's own cache, so a guide that got shorter still replaces the old one.
   - The knowledge base records RestedXP's version and a hash of its DB and guide files, and rebuilds when they change. `build_kb.py --check` then says "RestedXP was updated (old -> new)".

## Known beta quirks

- **SavedVariables:** they are written on logout but not read back after a cold start (build 69913 and later). That's why the engine archives them (and `archive.ps1` exists), and why the scan uses `HaveQuestData` and not saved progress to resume.
- **Protected values:** Forever uses Midnight's addon restrictions. Every value the addon reads is checked with `issecretvalue`, and protected values are skipped, never computed on.

## Live coaching

```
WoW with the Glide addon -> pixel strip -> engine (capture -> decode -> state -> rules -> coach) -> voice, overlay, coach screen
                                                                              ^ glide.db (quests, spawns, your RestedXP guides)
```

| Path | What it is |
|---|---|
| `addon/Glide/` (live-link part) | Streams live state through a 288×48 px strip of fully-off/fully-on coloured cells (they survive HDR) in the window's top-left corner, 5 times a second, plus three thin bars under it (health, power, target health). It reads the game only. |
| `companion/app.py` | The engine. It captures the strip, tracks state and XP/hour, runs the rules, calls the AI model, and speaks. |
| `start-glide.cmd` | Starts the engine from a source checkout (the development path). It also archives SavedVariables and the quest cache in the background, so no separate window is needed (`--no-archive` turns this off). |

**Addon version check.** The engine reads the addon version it expects from `addon\Glide\Glide.toc` and compares it with two others:

- the copy installed in the game's AddOns folder;
- the version the running addon reports.

The app's Addon page shows the installed version and whether it's up to date. On a mismatch the coach screen shows a red banner saying what to do (update the addon, or fully restart WoW), and the coach tells you once every 10 minutes.

### One-time setup (command line)

The desktop app needs none of this. For a run from source:

1. **Python 3.12 or newer:** `winget install -e --id Python.Python.3.12`, then open a new terminal, and `py -3 -m pip install -r requirements.txt` for numpy (maps and walking routes; the engine runs without it, with simpler directions) and faster-whisper (push-to-talk). `start-glide.cmd` and the engine stop with a message on an older Python.
2. **Claude API key:** run `setx ANTHROPIC_API_KEY "sk-ant-..."` in a terminal, then open a new one. Without a key the coach runs offline with simple rule-based callouts. (The app keeps its keys encrypted under Settings > AI models & keys.)
3. **Display mode:** set WoW to Windowed or Windowed (Fullscreen). Exclusive fullscreen can capture as black.

### Run (command line)

1. Start WoW with the Glide addon enabled. You should see a strip of coloured squares in the top-left corner.
2. Double-click `start-glide.cmd`.
3. Open http://127.0.0.1:8765 for the coach screen: the current RestedXP step, XP/hour, time to level, the route, the quest log, the map, and the coach chat with an Ask box. The app shows the same screen in its own window.

The overlay shows the current step and the last coach line. Normally the mouse goes straight through it to the game. While you hold **Alt**:

| Action | What it does |
|---|---|
| Alt + drag | Moves the overlay. |
| Alt + mouse wheel | Makes it wider or narrower. |
| Alt + right-click | Collapses it to just the step line, or expands it again. |

Run from `start-glide.cmd`, the overlay keeps its position, width and collapsed state in `data\live\overlay.json`. To use another key, set `"overlay": {"drag_key": "SHIFT"}` (or `"CTRL"`). To turn the overlay off, set `"overlay": {"enabled": false}` or use `--no-overlay`. The app's overlay has its own settings (Settings > Overlay) and reads only `drag_key` from `config.json`. Voice uses Windows speech.

**Directions instead of coordinates.** Waypoints, quest givers, turn-ins and objective areas are described the way you'd find them in game, for example: `Goldshire, at Marshal Dughan · 590 yd south (straight ahead)`. Each description combines three things:

- **Sub-zone:** taken from the map's area outlines.
- **Nearest landmark:** the closest quest giver, trainer, innkeeper or flight master.
- **Distance and direction:** yards, compass direction, and where it is relative to the way your character is facing.

The addon reports the map size in yards so the distances are real. If the game doesn't provide it, the engine estimates it from your running speed. The coach only reads out coordinates if you ask for them.

### Health, resource and class

Forever hides health and power numbers from addons, but a status bar can still display them. The addon draws three 200×3 px bars under the strip: green for your health, blue for your mana, rage or energy, and red for your target's health. The engine measures how full each one is.

The coach gets health %, power % with its type (mana, rage or energy), the target's health %, and your class. The coach screen shows health and power as bars next to your level. Keep the area under the strip visible, the same as the strip itself.

### Coach switches (in game)

**The Glide window.** `/glide`, or a left-click on the minimap button, opens it. It holds every switch the addon has:

- **Coach:** the master switch for commentary, then each kind (route, combat, buffs, gear, pace, upkeep, voice). The kinds grey out while the master switch is off; "Turn all on" resets them.
- **Helpers:** auto repair, bag cleanup when the bags are full, and "New spells on level up" (the trainer window below).
- **Link to the Glide app:** the data strip on or off, the strip's cell size (1 to 8 px), and the minimap button.

Hover any option for what it does. Escape closes the window, and it remembers where you dragged it.

**Minimap button.** Left-click opens the Glide window, right-click turns coach commentary on or off, drag moves it around the minimap. A grey icon means commentary is off; a red icon means the data strip is off, so the engine sees nothing. `/glide minimap on` brings back a hidden button.

Type `/coach` to see what's on. These switches decide what the coach talks about. Answers to your own questions always come through.

| Command | Effect |
|---|---|
| `/coach off` / `/coach on` | All commentary off or on. Only the outdated-addon warning still comes through. |
| `/coach combat off` | No low-health, death, ability or pull advice. |
| `/coach route off` | No next-step, turn-in, off-route or stuck callouts. |
| `/coach buffs off` | No buff reminders. |
| `/coach gear off` | No upgrade or quest-reward picks. |
| `/coach pace off` | No pace reviews, level-up or new-spell callouts. |
| `/coach upkeep off` | No bags-full or repair reminders. |
| `/coach voice off` | Callouts show as text only; answers to push-to-talk questions are still spoken. |
| `/coach <type>` | Toggles that type. |
| `/coach reset` | Turns everything back on. |

`/glide coach …` does the same thing. The switches are saved with the addon's settings, and the coach screen shows which types are muted in the coach chat's header. The coach is also told which topics are muted, so they don't creep into other callouts.

### Buff reminders

The addon streams your buffs (helpful auras plus weapon enchants like poisons and sharpening stones) with their time left. The coach screen shows them as icons by your character at the top, with the full list in the Buffs section; ones you cast yourself have a gold outline.

- **Automatic reminders:**
  - Your own buffs lasting 2 minutes or more get one reminder 15 seconds before they run out, in or out of combat.
  - When one drops, you get a "Rebuff: …" callout once you're out of combat and alive, and only if it's still missing 4 seconds later. Recasting within those 4 seconds cancels it.
  - Otherwise the coach doesn't mention buffs unless you ask.
- **On request:** say or type "remind me to reapply Battle Shout" and the coach adds it to a watch list, kept in `data\live\watched_buffs.json`. That works for short buffs too. Say "stop reminding me about …" to remove one.
- **Settings:** `config.json` → `"companion"` → `"rules"` → `"buffs"` (in the app: Settings > Buffs & gear):
  - `enabled`: default on.
  - `min_duration`: default 120 seconds.
  - `warn_before`: default 15 seconds.
  - `drop_grace`: seconds a buff must stay missing before the drop reminder; default 4.
  - `remind_others`: also remind about buffs other players cast on you; default off.
  - `ignore`: buff names to skip.

Forever returns no aura data at all in combat, and for a moment after it. So the addon freezes the last good list during combat and publishes a new one only once two reads out of combat agree. The coach screen and the coach treat the list as stale meanwhile. Your own buff casts do come through in combat, so a recast refreshes the buff right away. Reminders only cover buffs you can cast yourself; buffs from NPCs or other players are skipped unless you set `remind_others`. `/glide buffs` shows what the addon can read right now and its state (`list`, `settling`, `frozen (combat)` or `unreadable`).

### Gear upgrades

The addon sends your equipped items, the equippable items in your bags, and the reward choices while a quest-complete window is open. It includes each item's stats, plus "Equip:" bonuses such as crit, hit, spell damage and attack power that it reads from the tooltip.

- **Scoring:** the engine scores items with RestedXP's Forever stat weights for your class and level band, from `companion\data\stat_weights.json`. The coach screen's Gear section shows which profile is in use.
- **Trained skills:** the addon sends the weapon and armor skills your character has actually trained, and the ranks where the client still shows them. An item of a type you haven't trained is marked "can't use (not trained: Two-Handed Swords)", and the coach won't tell you to equip it. When you train a new weapon skill, your bags are checked again. `/glide gear` shows what the addon reads. If the game doesn't report skills, the class rules below are used instead.
- **Usability:** it skips items you can't use, checking armor and weapon proficiency the way RestedXP does (for example plate at 35 for warriors and paladins) and what the game reports. Items above your level are flagged "upgrade at level N", and you're told again when you reach that level.
- **Bag drops:** when something that drops into your bags is an upgrade, the coach tells you to equip it, naming the slot and what it replaces. This waits until you're out of combat.
- **Everything in your bags:** the coach also sees every equippable bag item with a verdict: upgrade, small upgrade, not better, or can't use (with the reason). An item going into an empty slot always counts as an upgrade. Items that aren't upgrades are listed dimmed at the bottom of the Gear section.
- **Checking what the addon sees:** `/glide gear` prints your worn items and the equippable items in your bags, as the addon sends them.
- **Quest rewards:** when a reward window opens, the coach says which reward to take right away, ranked by upgrade value.
- **Settings:** `config.json` → `"companion"` → `"gear"`:
  - `spec`: for example `"Enhancement"` or `"Frost"`. The default is the first profile for your class.
  - `kind`: `"Speedrun"` (the default) or `"Hardcore"`.
  - `min_gain_pct`: default 3.

### Training, flight paths, supplies, target and corpse

Since addon 0.10, the addon also sends:

- **Training:** RestedXP's per-level training list (class and race) checked against what your character knows. It covers spells at or below your level that you haven't trained. A lower rank is left out once you have a higher one. Talent points you haven't spent are also included.
- **Flight paths:** the flight paths you know. They come from every flight map you open, saved per character, plus RestedXP's own list.
- **Consumables:** food, drink, bandages, potions, poisons, stones and oils, ammo, soul shards and reagents in your bags, with counts and the level each needs.
- **Target:** whether someone else tapped it (no XP), and whether it counts for one of your unfinished quest objectives.
- **Death:** your corpse's position while you're dead, and Resurrection Sickness time afterwards.

With the knowledge base, the coach gets names and distances instead of IDs. It gets:

- the spells you're missing and what arrives in the next two levels;
- which flight paths you have, with an unknown flight master within 80 yards flagged;
- supply counts;
- the corpse distance, comparing a corpse run with the spirit healer.

Callouts from these, each said once:

- `training_available`: once per level.
- `talent_points_unspent`: once per level, 30 seconds after the points appear.
- `consumables_low`: when food drops to 4, drink to 4 (mana users) or ammo to 200 (hunters).
- `flight_path_nearby`: once per flight master.
- `target_tapped`: you target a mob someone else tapped (at most once a minute per mob).

The first three are "upkeep" commentary, the flight path one is "route" and the tapped one is "combat", so the in-game switches mute them. `/glide extras` prints what the addon sees.

**New at your trainer (addon 0.16).** When a level brings spells or ranks your class trainer teaches, a window shows them the way the spellbook does: the icon, the name and rank, and the spell's own text. Each shows what the new rank changes against the one you know (the numbers that changed, and by how much), and its cost once a trainer has shown it to you, with the total and your gold. It decides nothing: you see whether the trip to town is worth it. It opens by itself after such a level up (never in combat: it waits), unless "New spells on level up" is off in the Glide window. `/glide train` opens it any time.

### Push-to-talk

Hold **F8** while WoW is focused, ask your question out loud, and release.

1. You hear a high beep when it starts recording and a low beep when it stops.
2. Your words are turned into text, on your PC unless you pick an online engine.
3. The question goes to the fast model, which answers in about 40 words or fewer.
4. The answer is spoken right away, cutting off any callout in progress.

Questions typed on the coach screen still use the deep model.

- **Better recognition (recommended):** the app installs `faster-whisper` and its speech model the first time push-to-talk needs it (Settings > Voice). English defaults to `small.en`; other selected languages use multilingual `small` instead of English-only weights. Both are about 480 MB. From the command line, `setup-voice.cmd` installs `faster-whisper` and `small.en`. Recognition then runs offline on your CPU. Without Whisper, the engine uses an installed Windows recognizer for the selected language.
- **Name hints:** your current zone, target, and quest names are passed to the recognizer so it spells them right.
- **Settings:** under `config.json` → `"companion"` → `"ptt"` (in the app: Settings > Voice):
  - `key`: `F8`, `MOUSE4`, `MOUSE5`, `CTRL+Q`, and so on.
  - `engine`: `auto` (whisper if installed, else Windows), `whisper`, `windows`, or an online one: `openai`, `groq` or `deepgram`, each with its own API key. An online engine that fails falls back to the local one for that question.
  - `whisper_model`: `small.en` is the English default; `base.en` is faster. Selecting a non-English language replaces missing or English-only models with multilingual `small`, retaining an already selected multilingual model.
  - `only_when_wow_focused` (default on), `beep` (default on).
- **Off for one run:** `--no-ptt`.

The key is read passively. Nothing is sent to the game, but WoW also sees the key, so pick one you haven't bound in game.

### Language and speech

Settings > **Language** selects the coach and speech language (`companion.voice.language`). The picker covers the WoW client languages: Chinese, English, French, German, Italian, Korean, Portuguese, Russian and Spanish. **Interface language** is separate: English only, disabled and marked WIP.

- Connected AI models answer in the selected language. Deterministic coach messages use the configured **fast model** for translation; model-generated answers are not translated a second time. Disabled or unavailable translation leaves the original English text.
- The startup announcement is the first named text resource, `voice.ready` ("Glide Voice is running!"), and uses that same translation path.
- Recognition receives the selected language. Whisper uses multilingual weights for non-English speech; Windows needs a matching installed recognizer. Online recognition still requires its provider's API key and may incur charges.
- **Edge-TTS** is a free, online, unofficial Microsoft speech service, without an API key. Its live voice catalogue is filtered by the selected language; **Show all voice languages** exposes the full catalogue. Language changes retain a compatible voice or select one from the catalogue. Narration text is sent to the service; availability and rate limits are outside Glide's control.
- Windows narration uses installed voices. OpenAI TTS supports multiple languages, with voices optimized for English; ElevenLabs support depends on its model and voice. Glide's Azure and Google voice pickers currently list English voices, although those services support other languages.
- In the provider help, **Windows speech → Open Windows speech settings** opens Windows' voice selection and voice-package management page. Settings links beneath the selected model/service names navigate to the corresponding Glide section.
- The Language panel shows the selected translation, recognition and narration tools, links to their settings, and a **Language support** popup with setup guidance. Save language changes and restart the coach.
- **Translate news and community notes** is a separate disabled **Off / WIP** control. Community-feed news, tips, data and mechanics remain in their original language; no translation requests are made for this feature yet.
- The footer's **AI → language** indicator shows when the running engine can use its fast model for coach-message translation. Its tooltip names the model and explains costs and English fallback; clicking it opens Language settings. An unavailable model shows **AI off · language**. The indicator is hidden for English or a stopped coach. Provider language help opens from the circular **i** button beside **Speech output**.
- **Voice diagnostics:** Logs records each online synthesis request with a session-local `TTS #` id, provider/voice, text length, active request count, elapsed milliseconds and audio bytes. Edge failures distinguish empty audio, known Python/network exception classes, HTTP handshake status, missing runtime/module and timeout; partial audio is counted but never played as a successful result. The matching fallback line identifies Windows speech; playback completion, pause and media errors are logged separately. Toasts remain rate-limited, logs do not. Diagnostic records exclude narration text, raw stderr/tracebacks, request URLs and token values; ordinary coach-message logs are unchanged. No retry, endpoint or timeout change is implied. Logs are also saved in the app's existing daily `logs/engine-YYYYMMDD.log`.

### Options

- `start-glide.cmd --sim`: simulated Skyborne Alliance warrior. It starts at Zephras Isle and follows your RestedXP guides' next-guide chain to 60, with no game needed, and exercises the whole pipeline. Time is compressed, so XP/hour reads high. To run it faster, `set GLIDE_SIM_SPEED=10` first; above 1, only every 20th frame goes through the pixel encode/decode. In the app's source checkout, `npm run sim` does the same.
- `start-glide.cmd --calibrate`: saves what the capture sees to `data\live\capture.png` and tries to decode it.
- `--no-voice` and `--no-overlay` turn those off. `--port` picks the coach screen's port (default 8765).
- **In game:** `/glide help` shows all commands. `/glide strip on|off` turns the data strip on or off, `/glide cell 4` makes the cells bigger if decoding is flaky, and `/glide dump` prints what the strip is sending. The old `/glidelink …` still works.

Every frame, trigger and coach line is logged to `data\live\<session>.jsonl` for review.

### The knowledge base

`data\kb.db` is what makes the coach reason instead of paraphrase. The first start builds it (the app, or `start-glide.cmd`); after that the engine rebuilds it in the background when its inputs changed (glide.db, your recordings and session logs, the rules, the code). `build-kb.cmd --check` says whether it is stale; `python tools\kb_report.py` (in a source checkout) prints coverage and the open unknowns.

What goes in, with provenance on every fact (source tag, confidence, verified-on-Forever flag):

- **World:** quests (XP, level, givers, objectives, prerequisites, chains), creatures with their spawn points, drop rates, items, vendors, trainers, flight paths with times, graveyards, RestedXP's dangerous-mob notes, map bounds for 51 maps.
- **RestedXP guides:** all the guides you own, fully parsed: every step's text, the author's `--XX` XP notes, conditions, and every action with arguments.
- **Mechanics fitted to Forever** (`knowledge/mechanics/`): kill XP, quest XP and its over-level decay, the XP table, rested XP, run/ghost/flight speeds, the hearth cooldown, death costs. Each parameter is marked classic, forever-fitted, forever-verified or unverified; the coach is told which. Findings so far: Forever's kill XP for lower-level mobs uses `5 × mob level + 45` (Classic used the player's level) and rounds half to even; the rested bonus is `min(kill XP, pool/2)`; Well Fed adds 5%; the XP table matches vanilla through level 12 (8,800 at 11, 10,100 at 12); a campfire adds 5% of a level to the rested pool; Camp Benefits does nothing to kill XP; the hearthstone cooldown is 60 minutes. `knowledge/VERIFY.md` lists the experiments that would settle the rest (elite multiplier, gray level, mount, res sickness, aggro radius...).
- **Route analysis** (`knowledge/analyzer/`, with the per-step work in `knowledge/analyzer/route/`): for every step of every guide and class, the expected level, XP now, XP it unlocks later, time (travel, kills, interactions), XP per minute, why the step exists (hub turn-in, stacks objectives, enables a chain, on the way, level gate, hearth cycle...), what it depends on and enables, the alternatives nearby and why RestedXP skips them, and dangerous mobs around. A simulator answers "what if": skip a quest, grind here, hold a turn-in, leave the zone early, run a dungeon.
- **Expert heuristics:** 321 rules with conditions (class, level band, situation), advice, rationale and source, plus a 96-term glossary. `knowledge/heuristics/SOURCES.md` lists where they come from.
- **Your play:** sessions, kills with time-to-kill, deaths and corpse runs, quest XP actually paid, time spent per RestedXP step. The model is calibrated against it.

**What the coach does with it.** Every callout gets a compact knowledge block: the current step's analysis (kind, XP now, what it unlocks, why codes, RestedXP's notes, risks, the two best alternatives), your level against the plan, pace against a benchmark, the applicable heuristics, and a card for your target. The coach screen shows the same in its Why section. Questions (typed, or push-to-talk) run a tool loop: the coach can call 21 knowledge tools (`community`, `quest_info`, `creature_info`, `step_brief`, `why_this_step`, `skip_impact`, `what_if`, `alternatives_here`, `eta`, `kill_xp`, `quest_xp`, `path_time`, `walk_route`, `route_lookahead`, `guide_summary`, `heuristics`, `glossary`, `session_review`, `search`, ...) plus its reminder and memory tools, and must cite the facts it used; the ids show under each message, unknown ones in red, and `data\live\grounding.jsonl` keeps the record. Spoken questions allow 2 tool calls and 12 seconds; typed questions 6 calls and 40 seconds.

**Marking a moment.** `/glide mark [note]` in game (or a key bound under Key Bindings > AddOns > Glide) flags the last callout with the full state into `data\live\flags.jsonl`, which the next knowledge build turns into evaluation items. Use it whenever the coach is wrong.

**Evaluation** (source checkout only; the installer doesn't ship `knowledge/eval`). `python knowledge\eval\run_eval.py --dry --limit 5` shows the knowledge packs for the first items of `knowledge/eval/questions.jsonl` (232 questions, 110 of them your own from the logs) without calling the API; without `--dry` it asks the coach and scores the answers (`--model fast|deep`, `--judge`). Reports go to `data\eval\`.

**Map bounds.** The addon records the world coordinates of every map's corners at login (`/glide maps` shows how many); the knowledge build uses them to place RestedXP's world-coordinate steps exactly. Until a session has done that, fitted bounds are used.

### Tuning (`config.json` → `"companion"`)

Every engine setting, with its default, its range and whether it applies without a restart, is listed once in `companion/settings_manifest.json`; the engine reads settings through it and the desktop app's Settings page is generated from it, so a hand-edited value outside its range reads as the nearest end of it, and one of the wrong kind as the default.

```json
"companion": {
  "llm": {"fast_model": "claude-haiku-4-5-20251001", "deep_model": "claude-sonnet-5-5", "min_interval": 20},
  "rules": {"stuck_minutes": 5, "review_minutes": 15},
  "voice": {"enabled": true, "rate": 1},
  "overlay": {"enabled": true, "x": 20, "y": 140, "width": 520}
}
```

**Out of credits or bad key:** the first such error stops all API calls; nothing retries in the background. You hear one message explaining why, and the coach switches to basic rule-based callouts. The coach screen shows `offline: out of API credits` as the coach's mode. After topping up at console.anthropic.com, ask the coach any question: that one call tests the API and resumes live coaching if it works. Restarting the engine also works. Temporary errors (overloaded, rate limits, network) pause AI calls for 15 s, then 30 s, and so on up to 10 minutes.

**Choosing models.** In the app, Settings > AI models & keys picks the model for each path:
- **Callouts and spoken questions** use the fast model (`voice_question_model` can send spoken questions to the deep one).
- **Typed questions and reviews** use the deep model.

The lists come from Anthropic's model API (needs `ANTHROPIC_API_KEY`), Google's Gemini API (needs `GEMINI_API_KEY`; models show as `gemini:<id>`, e.g. `gemini:gemini-2.5-flash`, and are called directly through Google's OpenAI-compatible endpoint, with thinking turned down for callouts and spoken answers), OpenRouter's public model list, and any OpenAI-compatible endpoint you add (models show as `ep:<endpoint>:<model>`). They're cached in `data\live\models.json` and refreshed daily, or with **Refresh the list**.

Each option shows:
- its price per million tokens in and out;
- its context size;
- whether it supports tool use;
- an estimated cost per hour, from this session's measured usage (typical usage until there's enough).

The page also shows what this session has spent so far. A choice applies to the next call and is saved in `config.json` (`companion.llm.fast_model` / `deep_model`), so the next start uses it too.

Switching is announced: the coach says which model and API each path now uses (for example, "Model switched. Callouts and spoken questions now use Gemini Flash Latest via Gemini API."). It also warns when that provider's key isn't set or the model can't use tools. At startup the console and the coach chat list the saved models. Errors and status messages name the API that was actually called (Claude API, Gemini API or OpenRouter). Being out of credits or having a rejected key stops calls only to that provider, so switching to another provider takes effect immediately. If a Gemini model refuses the low-thinking setting, the coach tries the next level down quietly and remembers what worked. A busy model (429/5xx) gets one retry a second later before the error is reported.

**Quest briefings.** When you accept a quest, the coach reads RestedXP's own instructions for it: the text of the guide's steps that work on the quest, from your current step on (for example "Kill Wolves. Loot them for Meat."). Video and "click here" lines are left out, and a step repeated later in the guide is read once. A quest with no such step (a delivery) gets its turn-in step; a quest the current guide doesn't handle gets the game's objective text. The coach then adds a spoken explanation, up to `briefing_words` (40) words, of how to do it: where to go, what to kill, collect or use, drop rates worth knowing, anything easy to miss, and whether the RestedXP route does it now or later.

- **Several quests at once** (one quest giver, a hub) are one briefing. The first `briefing_max_quests` (4) are read in full and the rest by name only.
- **Model:** briefings use the fast model. Without an AI, the explanation is a rule-based line from the knowledge base (drop source and rate, nearest spawns).
- **Instead of a callout:** accepted quests get a briefing instead of the usual one-line callout. They're muted with `/coach route off` like other route talk.
- **Repeats:** a quest re-accepted within 10 minutes isn't briefed again.

**Step videos.** Some RestedXP steps link a YouTube clip ("CLICK HERE for an example").

- **When it opens:** when the guide moves to such a step, the clip opens in a new browser window. The default browser is started with `--new-window` when it's Chrome, Edge, Firefox, Brave, Vivaldi or Opera; set `video_browser` to a browser's .exe to use another. If the browser can't be found, the link opens the usual way.
- **Not on the step you start the session on**, since you were already on it.
- **Never in combat, on a taxi or dead.** The clip waits until that's over.
- **Each clip opens once per session.** A step with several clips opens the first and lists the others in the coach chat, which has an "open" link for every clip.
- **What it shows:** with `video_tips` on, the coach also says what the clip shows (where to go, what to do).

Both are switches in the app's Settings > Coaching > Quest briefings and step videos, saved in `config.json` as `llm.quest_briefings` and `llm.step_videos`. You can also say or type:
- "quest briefings off" / "briefings on"
- "stop the videos" / "step videos on"

These are handled locally, without an API call, and saved.

**Reminders.** Ask the coach to remind you of things, out loud or typed:
- "remind me to repair when I'm near a repair vendor"
- "remind me to buy 20 Ice Cold Milk"
- "when I get to Ironforge remind me to train"
- "remind me to go back to Loch Modan at level 18"
- "remind me in 15 minutes to check the mail"

Each reminder applies when one of these is true:
- **Near an NPC:** within 40 yards (`reminder_near_yd`) of a friendly NPC of a role: repair (armor and weapon merchants, smiths), vendor, food and drink, ammo, reagents, innkeeper, trainer, your class trainer, flight master, banker or stable master. Or near one that sells the item you want to buy; if no vendor there is known to sell it, any vendor of that kind counts.
- **In a place:** a zone, town or sub-zone by name.
- **At a level.**
- **After some minutes.**

How the coach says them:
- **On arrival:** when the condition becomes true, with the NPC's name, distance and direction, for example "Reminder: Repair. Durnan Furcutter (Cloth & Leather Armor Merchant) is 20 yards east."
- **Once per arrival:** it re-arms after you've been away for 2 minutes.
- **Nudges:** every 20 minutes it nudges you about open reminders ("Still to do: ..."); the coach can set another interval, or 0 for none.
- **Never in combat, on a taxi or dead.**

Closing reminders:
- **Repairs close themselves** when your durability goes back up.
- **Everything else** closes when you say "done with the repair reminder" or "clear reminders", or with the ✓ next to it on the app's Memory page, under Reminders. That page also has a box to add one.
- **Listing:** "what are my reminders" reads them.

Who files them:
- **Clear requests are filed locally**, without an API call.
- **Unusual phrasings go to the AI**, which files them with its reminder tools.
- **Buff reminders** ("remind me to reapply Battle Shout") still use the buff watch.

Reminders are kept in `data\live\reminders.json`.

**Follow-up questions.** Every question carries the recent conversation, so "and how long does the second one take?" or "what did you mean by that?" work:
- **What's included:** your last questions with the coach's answers, plus what else it said in the last 5 minutes (up to 4 callouts, briefings or reminders).
- **How much:** two settings under Settings > Coaching > Question context set it: how many past questions (`conversation_turns`, default 6, 0 = off) and from how far back (`conversation_minutes`, default 20).
- **Saving:** both are saved in `config.json`.

**Web search.** When the knowledge base and the coach's tools don't have the answer (a Forever hotfix, a bug or trick the community found, a detail the KB lacks), the coach can search the web.
- **Claude models:** use Anthropic's built-in web search: at most 2 searches per question (`web_search_max`), $10 per 1,000 searches plus the tokens, counted in the session cost. If your API organization has web search turned off (Claude Console → Settings → Capabilities), the coach says so once and answers without it.
- **Gemini and OpenRouter models:** use a search API key you set yourself, like the other keys never written to a file: `setx TAVILY_API_KEY ...` (preferred: page extracts) or `setx BRAVE_API_KEY ...`. Without one they don't search.
- **Sources:** the answer names its web sources (FACTS `web:<domain>`), and the coach chat links them.
- **Switches** in Settings > Coaching > Web search: web search for typed questions (on) and for spoken questions (off by default, since a search adds a few seconds). Pace reviews and callouts never search.

**Coach screen layout.** The middle holds the current step, a book of sections (Why, Quests, Gear, Talents, Buffs, Community, Session) and the coach chat; the map and the route tracker sit at the side.
- **Book:** one tab per section. With room to spare, sections dock into always-open columns on the left.
- **Moving:** sections move by their grips at any time. **Layout** in the bottom bar turns on arrange mode, where every piece can be dragged within the top bar, the middle or the bottom bar (Alt and the arrow keys move the focused piece).
- **Saving:** the arrangement is saved by the engine (`data\live\dashboard_layout.json`), so it's the same next session and in any browser. **Reset layout** in arrange mode restores the default.

The **map** zooms up to 8x:
- **Zoom:** mouse wheel at the cursor, or the + and − buttons.
- **Pan:** drag the map.
- **Whole zone:** double-click, or the whole-zone button.
- **Follow:** keeps you in the middle while zoomed; it turns off when you drag the map. The zoom level and the follow setting are remembered.

**Game maps.** Glide reads the game's own map data straight from your client's archive (`World of Warcraft\Data`). It only reads; nothing in the game folder is changed. It needs `numpy` (`py -3 -m pip install -r requirements.txt`; the desktop app bundles it). It runs in the background refresh and only does the work when the game was patched since the last run. By hand: `python tools\extract_maps.py` (`--force` re-extracts).

- **Zone layout:** the client's map tables (`data\db2\*.csv`, imported into the knowledge base) give exact map bounds for every zone, area names and level ranges, every flight master with flight costs and paths, and 370 named places (towns, camps, landmarks) the coach can name and route to. The exact sub-zone shapes come from the world map's explored-area overlays (`data\gamedata\areas.npz`), so "where am I" and every "where" the coach says use the real borders, not rectangles.
- **Map images:** every zone and continent map, the in-game world map with all areas revealed (`data\gamedata\maps\<uiMap>.png`). The coach screen's map shows the current zone with you (and your facing), the RestedXP waypoints, and the walking route to the current step.
- **Terrain:** height, steepness, water and terrain holes of every open-world continent at 8.3-yard resolution (`data\gamedata\terrain\<map>.npz`, from the map tiles). Routes use it until the walking map is built.
- **Walking map:** a 3D map of where you can walk (`data\gamedata\nav\<map>.npz`, built by `tools\navmesh.py`). It's made from:
  - the terrain's full triangle mesh, sampled every yard with the exact slope under each point;
  - every collidable triangle of every building, city, cave, tunnel, bridge and wall (WMO), and of every tree, rock, fence and cart with a collision mesh (M2), including the ones a building places itself;
  - water, which is swum at its surface.

  It keeps up to four floors per spot (a street, the rooftop above it, the tunnel under the mountain, the river under the bridge), and knows which floor you're on from your height (addon 0.14) or, failing that, from how you've moved. The movement rules were measured on recorded play: slopes up to 50 degrees are walked (no sustained climb on recorded runs went past 47.5), anything up to 1.5 yards is hopped (curbs, stones, the rim of a platform, short steep bumps), drops of up to 8 yards are taken one way, and standing needs 2 yards of headroom.

  It's built once per install, in the background at idle priority with a pause after each map square, your current continent first; the old terrain grid is used until then. Game patches don't trigger a rebuild. The walking map's format (`wowfiles\navformat.py`) has two versions:
  - **`NAV_VERSION`:** raise it when the builder improves. Players whose map is older only see a "Better walking routes are available" suggestion in Settings > General > Walking map and on the Status page, and rebuild when they choose.
  - **`NAV_REQUIRED`:** raising it makes every install rebuild once, by itself. It's raised only when a rebuild has to be forced.

  Settings > General > Walking map has a button that rebuilds it. By hand: `python tools\navmesh.py [--maps 0,1] [--force]`.
- **The route to the step:** the coach screen's map and the coach plan the way to the current RestedXP step (`plan_to_step` in `knowledge\api\navigation.py`):
  - **Walk:** to the waypoint RestedXP's arrow points at, recomputed as you move, straightened into the lines you'd run, each shortcut checked against the map.
  - **Fly step:** walk to the flight master, then the flight along the path the gryphon really flies (the client's taxi path nodes), with stops when the game routes it through them. The destination comes from RestedXP's `.fly` (addon 0.14), or from the guide.
  - **On a flight:** the rest of the flight to where you land.
  - **Faster by flight:** when a flight you know beats running there by more than 30% and a minute, the coach is told.

  The coach's `walk_route` tool and the "walk_to_step" context of every question get the legs (direction, yards, the area each ends in), the areas passed through, swimming and the flight.
- **Encrypted content:** some Forever content (parts of the area and map tables) is encrypted with keys the client gets from the server. What no known key opens is reported and left out. The community's TACT key list ([wowdev/TACTKeys](https://github.com/wowdev/TACTKeys)) isn't shipped: `wowfiles/casc.py` downloads it when first needed into `data\gamedata\tactkeys.txt`, refreshes it weekly, and decrypts everything it covers.

**Fast start.** The engine doesn't rebuild the databases before starting; that used to take about a minute and a half on most starts, because every play session changes their inputs.

- **Startup:** the engine starts right away on the current `data\glide.db` and `data\kb.db`.
- **Background rebuild:** 20 seconds after startup it rebuilds them in the background, as `data\glide.next-<time>.db` and `data\kb.next-<time>.db`. This runs at below-normal priority so the game keeps its frame rate. Each build is skipped when nothing it reads has changed. The running session's own log and the grounding log don't count as changes.
- **Switch-over:** when the rebuild is done, the coach switches to the new files without a restart; the coach chat says so. The next start promotes them over the old ones.
- **First start:** only a first start without `data\kb.db` builds before the engine starts.
- **`--no-kb`:** turns the background rebuild off.

**Prompt caching.** Every request starts with the same prefix: the tool definitions plus the system prompt (rules, knowledge rules, glossary; about 7,500 tokens). Only the game state after it changes. How each provider handles it:

- **Claude (direct), and Anthropic or Google models through OpenRouter:** the request marks the end of that prefix, so repeats are read from cache at a tenth of the input price or less.
  - Callouts send the same tool definitions as questions, with tools disabled for that call, so callouts and spoken questions share one cached prefix. Without that they would need two, and the question one would usually expire.
  - The deep model's prefix uses the 1-hour cache lifetime, because pace reviews and typed questions come further apart than 5 minutes. A 1-hour write costs 2x instead of 1.25x but is reused for the whole hour.
  - In a tool loop (Claude direct), each round also marks the conversation so far, so the next round reads it back instead of paying for it again.
  - A callout's game state is never marked, since it's never sent twice.
- **Gemini (direct) and other OpenRouter models** cache repeated prefixes automatically. Nothing is marked.

Settings under `"llm"`:
- `prompt_cache` (default `true`)
- `cache_ttl_fast` (`"5m"`) and `cache_ttl_deep` (`"1h"`)
- `callout_tools` (`true`)

The coach screen's Session section and the app's session bar show the cache hit rate. Each API call's tokens (read from cache, written to cache, uncached, output) and its cost are logged as `{"api": ...}` lines in the session log in `data\live`.

OpenRouter models (ids with a `/`, e.g. `google/gemini-2.5-flash`) need the `OPENROUTER_API_KEY` environment variable (the app sets it from its saved keys). The key is never written to a file. A model without tool use answers questions from the knowledge block alone.

Live callouts use the fast model, at most one every `min_interval` seconds; deaths and low health go out immediately. Callouts are one short sentence about the trigger, capped at `callout_words` (default 18). Spoken answers are capped at `answer_words` (30), and typed answers and reviews at `typed_answer_words` and `review_words` (70). All four go under `"llm"`. The coach answers `SKIP` when it has nothing useful to add. Pace reviews every `review_minutes`, and your questions, use the deep model. Questions also get the upcoming RestedXP route: the current step plus the next 10 for spoken questions (`voice_lookahead_steps`) and the next 40 for typed questions and reviews (`lookahead_steps`). Each step comes with its RestedXP step number, XP, quests, time and direction. Both can be changed on the fly in the app's Settings > Coaching > Question context, and changes are saved in `config.json`.

## Tests

`py -3 tests/run_all.py` runs every test (`tests/**/test_*.py`, each in its own process) and prints a summary (add words to run only matching ones, e.g. `py -3 tests/run_all.py trash kb`). A test that can't run on this machine (no `data/kb.db`, no game data, no node, no browser, no linter) prints `SKIP: <reason>` and exits with 77; the summary lists those apart from the passes and failures. The knowledge tests in `tests/kb` use a committed fixture corpus (`tests/kb/fixture.py`), so they mean the same everywhere; `py -3 tests/run_all.py --smoke` runs the report-only checks against your own data (RestedXP guides, recordings, `data/kb.db`), which print findings and never fail. The tests need the packages in `requirements-dev.txt` (lupa runs the addon's Lua against a mocked WoW API; ruff and pyright are the linters `tests/test_lint.py` and `tests/test_types.py` run).

The golden masters (`tests/golden/golden.py check`) compare what the addon, the desktop app, the coach screen, the engine, the knowledge layer and the guide parser do with recorded snapshots; `--portable` runs only the three that need nothing but the repository. CI (`.github/workflows/ci.yml`) runs the tests, the portable golden masters, ruff, luacheck and the desktop build on every push, and before every release. [CONTRIBUTING.md](../CONTRIBUTING.md) has the details.
