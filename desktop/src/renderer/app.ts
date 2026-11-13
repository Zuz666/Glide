// The main window: the coach console (the engine's dashboard), settings, addon & game, and logs. It also speaks
// the coach's lines ("speak" events on the engine's event stream) with the voice picked in Settings > Voice.
//
// This module puts the window together: each part wires its own controls and events, the Settings page gets its
// custom panels, and the start sequence below reads what the main process knows.
import { wireActivity } from "./activity.js";
import { refreshAddon, wireAddon } from "./addon.js";
import { refreshGuides } from "./rxp-browser.js";
import { applyAppSettings, wireAppSettings } from "./app-settings.js";
import { call } from "./bridge.js";
import { onEngine, wireCoach } from "./coach.js";
import { $, toast } from "./dom.js";
import { wireDateTimes } from "./date-time-bridge.js";
import { loadLog, renderLog, wireLogs } from "./logs.js";
import { refreshMemory, wireMemory } from "./memory.js";
import { show, whenShown, wireNav } from "./nav.js";
import { wireSessionBar } from "./session-bar.js";
import { renderCaptureCheck } from "./settings/capture.js";
import { renderDataHome } from "./settings/data-home.js";
import { renderEndpoints } from "./settings/endpoints.js";
import { renderKeys } from "./settings/keys.js";
import { renderLanguage } from "./settings/language.js";
import { renderModelPicker } from "./settings/model-picker.js";
import { renderOverlayUnlock } from "./settings/overlay-position.js";
import { renderSetupAgain } from "./settings/setup-again.js";
import { registerPanels, wireSettings } from "./settings/page.js";
import { renderVoices } from "./settings/voices.js";
import { renderWalkMap } from "./settings/walk-map.js";
import { renderSpeechInstall, wireSpeechStatus } from "./settings/whisper.js";
import { live } from "./state.js";
import { openSetup, wireSetup } from "./setup.js";
import { refreshStatus, wireStatus } from "./status.js";
import { UpdateStatus } from "./types.js";
import { onUpdate, wireUpdates } from "./updates.js";
import { catchUpNotes } from "./whats-new.js";

// the Settings groups that draw their own content (settings-schema.ts Group.special)
registerPanels({
  keys: renderKeys,
  "model-picker": renderModelPicker,          // the coach's /models list, with prices
  endpoints: renderEndpoints,
  language: renderLanguage,
  voices: renderVoices,
  "speech-install": renderSpeechInstall,
  "capture-check": renderCaptureCheck,
  "walk-map": renderWalkMap,
  "data-home": renderDataHome,
  "overlay-unlock": renderOverlayUnlock,
  "setup-again": renderSetupAgain,
});

wireDateTimes();
wireNav();
whenShown("addon", refreshAddon);
whenShown("guides", refreshGuides);
whenShown("logs", () => renderLog(true));
whenShown("status", refreshStatus);
whenShown("memory", refreshMemory);

wireActivity();
wireSessionBar();
wireAppSettings();
wireLogs();
wireSettings();
wireSpeechStatus();
wireAddon();
wireMemory();
wireStatus();
wireSetup();

// ---- start ------------------------------------------------------------------------------------------------
(async () => {
  const info = await call("app:info");
  if (info) $("app-ver").textContent = `v${info.version}${info.dev ? " dev" : ""}`;
  live.config = (await call("config:get")) || {};
  applyAppSettings((await call("app:settings:get")) || {});
  wireCoach();
  wireUpdates();
  await catchUpNotes(String(info?.version || ""));
  loadLog((await call("engine:log")) || []);
  onEngine((await call("engine:get"))!);
  onUpdate((await call("update:get")) || ({} as UpdateStatus));
  live.speech = (await call("speech:status")) || {};
  // a new install walks through setup first; one that skipped it starts on the Addon page until the game is set
  if (!live.appSettings.setupDone) openSetup();
  else if (!live.config.wow_dir) { show("addon"); toast("Start by choosing your World of Warcraft folder, then install the addon.", false, 10000); }
})();
