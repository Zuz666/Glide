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
              ── events ──> /events "speak"                 (voice output: Windows voices via speechSynthesis)
  overlay     ── events ──> /events state.overlay_step, message
```

The engine is the same Python code that `start-glide.cmd` runs. With `--app` it changes three things:

- It publishes speech as `speak` events instead of speaking itself (`voice.output = "app"`).
- It leaves the overlay to the app.
- It exits when the app closes.

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

Each reasoning-effort explanation links Auto to **Automatic thinking tuning**. Hover or focus **lightweight** to see the Fast/Deep policy by provider. The switch only supplies automatic options for supported direct Claude/Gemini models; explicit effort takes priority, and compatible endpoints/OpenRouter are unaffected. Turning tuning off leaves Auto to the provider.

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

- **Coach voice:** Settings > Voice > Coach voice. The services:
  - Windows voices: offline and free.
  - OpenAI: gpt-4o-mini-tts or tts-1.
  - ElevenLabs.
  - Azure Speech: neural voices.
  - Google Cloud Text-to-Speech.

  Keys go under Settings > API keys. If a service fails, that line is spoken with the Windows voice.
- **Push-to-talk recognition:**
  - **Offline:** Whisper (model small.en by default, downloaded on first use, about 460 MB) or the Windows recognizer. Until the model is on the PC, push-to-talk uses the Windows recognizer.
  - **Online:** OpenAI (gpt-4o-mini-transcribe), Groq (whisper-large-v3-turbo) or Deepgram (nova-3).

  An online engine that fails falls back to the offline one for that question.
- **Whisper set-up:** the model isn't in the installer. The app checks Whisper at every start, downloads the picked model into `%APPDATA%\Glide to 60\models` (kept across updates and reinstalls), and restarts the coach to use it. Progress shows in the footer. Settings > Push-to-talk lists every model with its size, to download ahead of time or delete.

## Zoom

**Ctrl + mouse wheel** zooms the whole window, the coach screen included, like a browser. Ctrl+= and Ctrl+- also zoom, and Ctrl+0 resets. The zoom level is remembered.
