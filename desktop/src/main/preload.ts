// The renderer's only door to the app: window.glide.invoke(channel, ...args) and window.glide.on(channel, fn).
import { contextBridge, ipcRenderer } from "electron";
// Type-only: a sandboxed preload can't load local modules, so the channel map must not exist at run time.
import type { EventChannel, InvokeChannel } from "../shared/ipc";

// What the window may call (main/app/ipc.ts answers) and listen to, by area (shared/ipc.ts gives each its arguments
// and answer; a name not there fails to compile). Anything else is refused here.
const INVOKE = new Set<InvokeChannel>([
  // the coach process
  "engine:get", "engine:log", "engine:start", "engine:stop", "engine:restart",
  // the app: its info and settings, the voice, the resource meter, updates and release notes
  "app:info", "app:settings:get", "app:settings:set", "app:note", "app:logs", "app:changelog", "app:mute",
  "tts:synth", "tts:voices", "tts:windowsSpeak", "tts:windowsStop", "tts:edgeStart", "tts:edgeDecoded", "tts:edgeCancel", "res:watch",
  "update:get", "update:check", "update:install", "update:download",
  // config.json, API keys and custom endpoints
  "config:get", "config:set", "keys:list", "keys:set", "endpoint:test", "keys:test",
  // the addon and folders
  "addon:status", "addon:detect", "addon:install", "addon:rxpImport", "addon:rxpImports", "addon:rxpRoutes",
  "addon:rxpFacts", "dialog:folder", "data:import", "shell:open", "shell:speechPanel",
  // offline speech recognition
  "speech:status", "speech:install", "speech:models", "speech:download", "speech:delete",
  // the overlay
  "overlay:unlock", "overlay:resize", "overlay:width", "overlay:dragStart",
]);
const EVENTS = new Set<EventChannel>([
  "engine:state", "engine:logline", "app:settings", "app:notice", "update:status", "speech:status", "res:sample",
  "overlay:unlocked", "overlay:held", "tts:edgeEvent",
]);

contextBridge.exposeInMainWorld("glide", {
  invoke: (channel: string, ...args: unknown[]) => {
    if (!INVOKE.has(channel as InvokeChannel)) return Promise.reject(new Error(`unknown channel ${channel}`));
    return ipcRenderer.invoke(channel, ...args);
  },
  on: (channel: string, fn: (payload: unknown) => void) => {
    if (!EVENTS.has(channel as EventChannel)) return () => undefined;
    const h = (_e: unknown, p: unknown) => fn(p);
    ipcRenderer.on(channel, h);
    return () => ipcRenderer.removeListener(channel, h);
  },
});
