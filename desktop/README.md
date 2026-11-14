# Glide: desktop app

One Windows app for the whole coach. It has these parts:

- **Coach screen:** the engine's own page, in the app's window.
- **Voice output:** the app speaks the coach's lines.
- **Overlay:** a click-through line over the game.
- **Settings:** a page for everything in `config.json` (the engine's settings come from `companion/settings_manifest.json`; `npm run build` generates the page's schema from it with `scripts/gen-settings.js`).
- **API keys:** stored encrypted.
- **Model endpoints:** plug in any OpenAI-compatible server.
- **Addon installer:** puts the addon in your WoW folder.

It ships as one installer, with its own Python, so nothing else needs to be installed.

```
Electron app (TypeScript)                                     Python engine (companion/, knowledge/, tools/, wowfiles/)
  main process ── spawns ──> python companion/app.py --app --port N
    engine.ts      start / restart / stop (stdin closes -> engine exits), first-start KB build, logs
    secrets.ts     API keys (Windows DPAPI) -> engine environment
    addon.ts       WoW folder detection, addon install / update
  main window ── iframe ──> http://127.0.0.1:N/            (the coach screen)
              ── events ──> /events "speak"                 (voice output: SAPI5 or online audio; Edge streams fragments)
  overlay     ── events ──> /events state.overlay_step, message
```

The engine is the same Python code that `start-glide.cmd` runs. With `--app` it changes three things:

- It normally publishes speech as `speak` events instead of speaking itself (`voice.output = "app"`). Selecting `voice.output = "windows"` keeps the engine's own SAPI5 speech process.
- It leaves the overlay to the app.
- It exits when the app closes.

To use Glide without the desktop app, follow the [command-line setup and launch guide](../docs/TECHNICAL.md#one-time-setup-command-line).

Push-to-talk, screen capture, the knowledge base and the background rebuilds all stay in Python.

## Develop

```
cd desktop
npm ci             # the locked packages (package-lock.json)
npm start          # build and run on the project folder's own data/ and config.json
npm run sim        # same, with the simulated character (no game needed)
npm run build      # build only (also what CI and the golden check run)
```

In dev mode the app uses the project folder as the engine home. If `desktop\build\python` exists, it uses that bundled Python; otherwise it uses `py -3` (the engine needs Python 3.12 or newer and says so otherwise). For a test copy, keep the API keys blank, the voice muted (`"muted": true`) and the window hidden: see [CONTRIBUTING.md](../CONTRIBUTING.md).

The engine's state stream and messages are typed in `src/shared/engine.ts`, the IPC channels in `src/shared/ipc.ts`; tests check both against the engine (`tests/test_engine_contract.py`, `tests/test_ipc_contract.py`).

## Build the installer

```
npm run python     # once: embeddable Python 3.12, pip and numpy into build\python (about 70 MB)
npm run dist       # release\Glide to 60 Setup <version>.exe (not published)
npm run dist:dir   # unpacked build in release\win-unpacked, for testing
```

`npm run python` (`scripts/fetch-python.ps1`) checks every download: the embeddable Python against a pinned SHA-256, pip as a pinned wheel with its SHA-256, and the MSVC runtime DLLs it copies from the build machine's System32 (for compiled packages like Whisper's) must be Microsoft-signed 14.x builds. faster-whisper isn't bundled: the app installs the version pinned in `src/main/speech.ts` (the same as `requirements.txt`) into its `pylib` folder the first time push-to-talk needs it. `-Models small.en` puts a Whisper model in `build\models` for dev runs.

**What the installer ships** (`build.extraResources` in `package.json`): the engine (`companion/`, `knowledge/`, `tools/`, `wowfiles/`), the addon, the community seed and the bundled Python. Development-only files stay out: `knowledge/eval/` and, from `tools/`, `build_seed.py`, `refresh_community.py`, `step_videos.py`, `kb_report.py` and `archive.ps1`. Nothing the engine imports or runs is among them; a new dev-only script in those folders should be added to the exclusions.

The installer is per-user and unsigned, so Windows SmartScreen shows "Unknown publisher" the first time.

## Releases and auto-update

The source stays private in `Fermin7c1/Glide`. Installers are published to the public repo `Fermin7c1/Glide-releases`, which only holds release files.

1. **Bump the version.** Change `version` in `desktop/package.json` (e.g. 0.2.0 -> 0.2.1) and push to main.
2. **GitHub builds it.** `.github/workflows/release.yml` first runs the CI checks (`.github/workflows/ci.yml`: tests, the portable golden masters, linters, the desktop build), then builds the installer on a Windows runner and publishes it with `latest.yml` as release `v<version>`. If that version is already released, it does nothing. You can also start it by hand from the Actions tab. CI on every push warns when engine or addon files changed since the last version bump, since only a release brings them to players.
3. **Installed apps update themselves.**
   - **When it checks:** at start and every 4 hours.
   - **Download:** a newer version downloads in the background, with progress in the sidebar.
   - **Install:** only when you choose. The footer shows the download, then a "Restart to update" button (also in the sidebar, the Status page and the tray menu). The installer shows its progress and Glide opens again on the new version. Nothing installs unseen, not even on quit.
   - **What's kept:** `data\`, `config.json`, your keys and settings. The engine code is refreshed on the new version's first start.

Switch: Settings > General > Update automatically. Click the version under the sidebar to check now.

**One-time setup:**
- **Releases repo:** create the public repo `Fermin7c1/Glide-releases`, initialized with a README so it has a branch to tag.
- **Token:** create a fine-grained token at github.com > Settings > Developer settings > Fine-grained tokens.
  - Repository access: only `Glide-releases`.
  - Permissions: Contents, Read and write.
- **Secret:** in `Fermin7c1/Glide` > Settings > Secrets and variables > Actions, add it as a secret named `RELEASES_TOKEN`.

`npm run release` publishes from your PC instead; it needs `GH_TOKEN` set to that token.

## Where things live (installed)

The folders keep the app's first name, "Glide to 60" (its `productName`): renaming them would leave every player's
settings, saved keys and downloads behind in the old folder.

| What | Where |
|---|---|
| App and bundled engine and Python | `%LOCALAPPDATA%\Programs\Glide to 60` (or the folder you picked) |
| Engine home: `data\`, `config.json` | `%LOCALAPPDATA%\Glide\engine`. On first run and after each update, the engine code is copied here. `data\` and `config.json` are never touched. |
| App settings (window, overlay, voice) | `%APPDATA%\Glide to 60\app-settings.json` |
| API keys (encrypted) | `%APPDATA%\Glide to 60\secrets.json` |
| Extra Python packages (Whisper) | `%APPDATA%\Glide to 60\pylib` |

**Settings > General > Data** has two options:

- **Change the engine home**, for example to point it at a project checkout. A checkout's own code is used as it is.
- **Import data:** copies `data\` and `config.json` in from another Glide folder.

## Saving settings

Ordinary fields use the same draft workflow for manual edits and **Use default**: changes stay unsaved until **Save** or **Save and restart coach**, and **Revert** cancels them. Saving a default removes the persisted override rather than pinning today's default value. If there was no stored override, Use default only cancels an unsaved edit; no save is needed. A failed save keeps the draft.

Live settings apply to the next call; fields marked ↻ require a coach restart. Save without restarting reports that requirement. Model catalogue picks and Coach voice controls retain their immediate-save behavior; API keys and endpoints retain their own save actions. Engine home folder changes and **Use default folder** save immediately and both report that a restart is needed.

## Models and agents

Settings separates **API keys** (credentials for AI, speech and search services) from **AI models** (model selection and behavior). Under AI models, **Custom endpoints** follows **Model ids**; **AI behavior & requests** contains spoken-question routing, the AI switch, request timing and automatic thinking tuning.

Each reasoning-effort explanation links Auto to **Automatic thinking tuning**. The switch shows the selected Fast/Deep models and their requested automatic effort and thinking mode, using the engine's request policy. Unsaved model, effort and switch edits update the preview; Save still applies them. Explicit effort is labelled as overriding the switch, and provider-default or disconnected/unknown cases never invent a numeric level. Hover or focus **model-specific levels** to see the broader provider policy. Compatible endpoints/OpenRouter are unaffected; turning tuning off leaves Auto to the provider. Requested automatic options may fall back if the provider rejects them; this preview is not a measurement of a completed call. Session-bar effort badges share a right edge.
For Gemini, the preview also uses the active provider's remembered accepted automatic option for that model and role, including a fallback to `low` or to provider defaults.
Only Auto roles with tuning On and a known automatic override are highlighted green; explicit effort, Off, provider defaults and unavailable policy remain neutral. Each status-bar model tooltip also explains the live automatic policy for its role. When spoken questions use Deep, the Fast tooltip names that Deep model and its selected effort.

**AI request timeout (s)** limits each request to an AI model, including coach answers and message translation; retries and multi-step answers can take longer in total. It is separate from the desktop's local settings-save timeout. Saving a model change does not wait for translation of its announcement: the selection is applied and acknowledged while the notice is localized in the background.

Every provider the console supports works here:

- **Anthropic:** `claude-…`
- **Gemini:** `gemini:<id>`
- **OpenRouter:** `vendor/model`

On top of those, **custom endpoints** (Settings > AI models) add any OpenAI-compatible server: Ollama, LM Studio, OpenAI, Groq, Mistral, DeepSeek and others.

- **Config:** they are saved in `companion.llm.endpoints`.
- **Model ids:** `ep:<endpoint>:<model>`.
- **Picking one:** the endpoint's models appear under Settings > AI models > Choose models. You can also click one in the endpoint's test list.
- **Keys:** set credentials in Settings > API keys; a key is optional for local servers. After adding or editing endpoints, click **Save endpoints** first: endpoint-to-keys links stay disabled until saved, so navigation cannot discard those edits and new endpoint credentials appear in the key list.

## Overlay

The overlay is click-through. To change it, hold **Alt** with the mouse over it, in game too:

- **Drag:** moves it.
- **Mouse wheel:** changes its width.
- **Right-click:** collapses it to the step line, or expands it again.

The key can be changed under Settings > Overlay (`companion.overlay.drag_key`, as before). **Ctrl+Alt+O** or the tray menu unlocks it without holding a key. Opacity and font size are under Settings > Overlay too.

## Voice

### Choosing how the coach speaks

Open **Settings > Voice > Voice output > Spoken by**:

- **this app** is the default. Use it to choose either an installed Windows voice or an online service under **Coach voice > Voice service**.
- **Windows speech process** lets the coach use its own Windows voice instead of the selected Voice service. Choose it if you want the coach's callouts to use a separate Windows voice. Online services are not used for those callouts.

For a separate voice, select **Windows speech process**, then under **Coach voice** turn on **Use a separate voice for the Windows speech process** and choose a voice. Leave that switch off to use the app's Windows voice instead. Click **Save and restart coach** to apply the output change.

If you run Glide from source with `start-glide.cmd`, without the desktop app, the coach uses Windows speech automatically. You can use the coach screen in your browser; see the [command-line guide](../docs/TECHNICAL.md#run-command-line).

**Test voice:** choose **Template phrase** or **Last spoken line**, or edit the text beneath the play button. Editing selects **Custom text** automatically; the test speaks that text as written, without translation. Your custom text is kept while switching sources or revisiting settings in the current app session, but is not saved for the next launch. Click the language name to open **Language > Coach and speech language**.

### Native speech markup

Windows SAPI5 speech can carry optional delivery controls for rate, pitch, sentence/paragraph boundaries and native pauses. Feature choices belong to the actual native voice, including when Windows default is selected. Unknown voices start with no enabled effects: opt in to experiment. Permission is not evidence of support; a successful synthesis does not establish that the engine honored a pitch change or requested pause. In particular, an installed Silero wrapper need not implement the controls of the newest direct Python model.

Native markup controls appear only when **Windows voices (SAPI5, offline)** is the app-selected Voice service. Online services hide the automatic-markup mode, budget and native effect permissions without discarding their saved values. The permissions section keeps a single **Effect support unverified** status visible beside the effect choices. Expand **Voice details** for the verification explanation, selectable native Windows voice ID and **Refresh voice list** action. Technical details start collapsed; enabling an effect does not change the verification status.

Automatic markup has three modes: **AI with local fallback**, **Local rules only**, and **Off**. Local rules use known event/instruction context rather than guessing emotion from arbitrary text. Translation precedes span binding; uncertain source-language controls are discarded. Online services continue receiving ordinary text, not native markup.

For eligible native AI replies, **Light**, **Standard** and **Expanded** add 64, 128 or 256 output tokens per request and permit at most 1, 3 or 6 hints. Standard is the default. This increases the total output ceiling, not a reserved markup pool; reasoning can consume that allowance too. Existing word limits, request timeouts and model-call counts do not increase. The choice is retained but inactive in local/off mode or without an eligible native destination.

In **Test voice**, Windows SAPI5 offers an explicit **Format** choice. **Plain text** offers Template phrase, Last spoken line and Custom text sources, plus editable text examples without effect permissions. **SSML** offers Last spoken line and Custom SSML sources, with its own examples and document editor; it does not offer a plain-text template. Each mode keeps its own source and custom draft; switching formats never reclassifies Template phrase as Custom text or copies plain words into an SSML document. Both example sets use the saved speech language offline (English, Russian, German, French, Spanish, Portuguese, Italian, Korean or Chinese); SSML also sets the matching `xml:lang`. Labels remain in the interface language. Selecting or editing never plays audio or translates a custom draft. Only **Play** submits the current mode's draft to the app-selected voice, independently of the coach's Windows-process voice. Invalid or disallowed markup is rejected before interrupting speech. Online providers show only plain-text sources and examples, without a Format selector. Switching online restores the plain draft; the separate SSML source and draft remain available when returning to Windows and explicitly selecting SSML.

In **SSML > Last spoken line**, the editor shows the latest line's saved document after successful native completion. If that line has no SSML, Play is disabled with an explanation; it never substitutes an older line's markup. Editing selects Custom SSML, preserving the edited draft independently of later speech. Replay validates the saved document against the currently selected voice and permissions; a new last-spoken document invalidates pending preparation rather than playing stale XML.

**Plain text** is also the delivery contract for a voice test: it bypasses automatic speech markup, including local sentence boundaries, without changing the coach's markup mode or per-voice permissions. This applies to every plain-text source and native fallback from an online test. The last-spoken record changes after a new line starts or completes; switching the editor format alone does not rewrite the previous line's history.

**Last 3 spoken lines** keeps the three most recent phrases in this app session, newest first. Each has its own voice, timing, format and local timestamp introduced by **at**. Start and completion events update the same retained entry even when independent engine speech arrives between them, without duplicating or reordering comparison entries. Repeating the same words in a new request creates another entry. Edge fragments extend one entry's spoken prefix instead of replacing it with only the final fragment; its generation timing still updates if another voice has spoken meanwhile. Settings rerenders and provider changes retain the history; restarting the app clears it.

History and **Plain text > Last spoken line** replay show words without SSML control tags, preserving their text and decoding XML character references. This does not change the original speech submission or interpret literal tags as delivery effects. **SSML submitted** means the line used validated native markup; **Plain text** means it did not. Each native entry can separately expose **SSML sent to SAPI5**, the exact compiled document shown as inert text and available to the SSML replay source while it is the latest line. A later plain/online entry never inherits that document; it stays with the earlier entry until that entry leaves the three-line history. Submission does not establish that the voice realized an effect. Observations without format/document metadata make no such claim.

For SAPI5, successful completion can add **first audio ≈** and **last audio ≈**, both measured from native request submission. These estimate when audio reached SAPI's playback buffer, not model computation or audible onset. The existing worker polls the output write position during playback; the final observation becomes the last one only after successful completion. Polling, cursor-update granularity, output backpressure and transport delay limit precision. The native output route, buffer settings and polling cadence are unchanged.

**Speech completed in** separately includes playback. When an audio endpoint pair is available, it replaces the first-word proxy in the caption; otherwise a completed native entry says **audio timing unavailable** and retains **first word reported in** only if a word was actually observed. No word report is substituted for an audio endpoint. Missing/invalid observations, cursor resets, file output and warmup provide no endpoint pair. Cancelled or failed turns acquire neither a completion duration nor final audio estimates. Each history entry retains its own measurements, including delayed completions; online synthesis keeps its existing generation metric.

Internally, canonical words remain separate from bounded native controls. A leading AI `SPEECH:` header proposes exact quoted spans; the shared native worker constructs escaped SSML only after validation. Queued lines retain their voice and effects when settings change. Early reply text already accepted for handling is never replayed merely to apply late hints or recover from a native failure. Native pauses are not emulated with timers or additional playback jobs.

### Voice services and recognition

- **Coach voice:** Settings > Voice > Coach voice. The services:
  - Windows voices: offline SAPI5, including installed third-party voices.
  - Edge-TTS: experimental, free online speech without an API key, including multilingual voices.
  - OpenAI: gpt-4o-mini-tts or tts-1.
  - ElevenLabs.
  - Azure Speech: neural voices.
  - Google Cloud Text-to-Speech.

  Keys go under Settings > API keys. Other online services fall back to the Windows voice for a failed line. Edge-TTS falls back only for the text that has not started playing; an already playing fragment finishes without repeating its beginning.

  Edge-TTS uses one persistent speech worker with two pools of three connections. Short sentences stay whole; long sentences split near natural punctuation or word boundaries. Two copies of each fragment run in parallel, and Chromium must decode a completed copy before it can win. Fragments play in text order as soon as they are ready. Each pool independently makes at most two attempts per fragment, with a short retry delay when the remaining budget permits; a silent copy in one pool does not block the other's retry. All attempts share the same 15-second online-synthesis deadline, including worker startup, queueing and decoding. Playback time is not part of that deadline.

  Idle connections receive configuration messages approximately every 20 seconds; broken connections reconnect with backoff. Losing copies may finish in the background to preserve their connections, but yield to queued work and the original deadline. Push-to-talk, urgent speech and tag cancellation also cancel synthesis. Muting, switching away from Edge-TTS or quitting closes the worker. No external FFmpeg executable is required. This is an unofficial service: connection renewal and duplicate requests cannot guarantee availability.
- **Push-to-talk recognition:**
  - **Offline:** Whisper (model small.en by default, downloaded on first use, about 460 MB) or the Windows recognizer. Until the model is on the PC, push-to-talk uses the Windows recognizer.
  - **Online:** OpenAI (gpt-4o-mini-transcribe), Groq (whisper-large-v3-turbo) or Deepgram (nova-3).

  An online engine that fails falls back to the offline one for that question.
- **Whisper set-up:** the model isn't in the installer. The app checks Whisper at every start, downloads the picked model into `%APPDATA%\Glide to 60\models` (kept across updates and reinstalls), and restarts the coach to use it. Progress shows in the footer. Settings > Push-to-talk lists every model with its size, to download ahead of time or delete.

### Edge speech ownership

`companion/edge_tts_worker.py` owns request scheduling, fragment acceptance and retry policy. Its private transport in `companion/edge_tts_pool.py` accepts immutable attempt context and owns connections, slot acquisition/release, turn timeouts and draining losers. Connections are released before waiting for Chromium's decoder verdict; a completed request can still have a losing turn draining within its original deadline.

The transport publishes aggregate connected capacity and transport counters, not individual slot events. Connected capacity includes busy and draining connections. `desktop/src/main/edge-tts.ts` validates these snapshots and the complete fragment protocol, retains the independent pre-spawn watchdog, and records safe request failure tallies and late drain failures. It does not reconstruct pool state.

The renderer trusts main's validated fragment plan, but still owns local decoding, accepted-candidate identity, ordered playback and the exact unstarted text suffix used by SAPI5 fallback. Cross-provider status composition remains in main: audible playback outranks background synthesis, and readiness updates do not extend fallback/error holds. Public speech IPC and player settings are unchanged.

## Dates and times

Glide follows your Windows regional date and time formats, including custom formats and your current time zone. Changes apply without restarting the app. The Coach screen inside Glide uses the same formats; opened separately, it follows your browser's preferences instead. Interface and speech languages do not override these choices.

Release and publication dates keep their original calendar day. Durations and countdowns are unchanged; copied diagnostic logs retain ISO timestamps.

## Zoom

**Ctrl + mouse wheel** zooms the whole window, the coach screen included, like a browser. Ctrl+= and Ctrl+- also zoom, and Ctrl+0 resets. The zoom level is remembered.
