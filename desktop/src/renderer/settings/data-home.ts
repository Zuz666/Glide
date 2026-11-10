// Settings > General > Data: the engine home (the coach's working folder), importing another Glide folder's data,
// and the runtime versions.
import { call } from "../bridge.js";
import { $, esc, icon, toast } from "../dom.js";
import { Group } from "../settings-schema.js";
import { live } from "../state.js";
import { renderSettings } from "./page.js";

export async function renderDataHome(_g: Group, host: HTMLElement): Promise<void> {
  const info = await call("app:info");
  if (!info) return;
  host.innerHTML = `<div class="field"><div class="lab">Engine home<small>The coach's working folder: data\\ (databases, recordings, logs) and config.json.${info.checkout ? " This is a project checkout, so its own code is used." : " Glide installs the coach's code there too (companion, knowledge, tools and addon folders): pick an empty folder or one Glide set up. It never replaces code folders it didn't install."} Folder changes save immediately and take effect when the coach restarts.</small></div>
    <div class="ctl"><input type="text" class="wide" id="dh-path" value="${esc(info.home)}" readonly>
      <button class="btn" id="dh-open">${icon("folder-open")}Open</button><button class="btn" id="dh-change">Change</button>${live.appSettings.engineHome ? `<button class="btn" id="dh-default" title="Save the default folder immediately; restart the coach to use it">Use default folder</button>` : ""}</div></div>
    <div class="field"><div class="lab">Import data<small>Copy data\\ and config.json from another Glide folder (e.g. your old project folder) into the engine home. The other folder isn't changed.</small></div>
    <div class="ctl"><button class="btn" id="dh-import">${icon("download")}Import from a folder</button></div></div>
    <div class="field"><div class="lab">Runtime</div><div class="ctl help">Glide ${esc(info.version)}${info.dev ? " (development)" : ""}, Electron ${esc(info.electron)}, ${esc(info.python.bundled ? "bundled Python" : "system Python (" + info.python.cmd + ")")}</div></div>`;
  $("dh-open").onclick = () => call("shell:open", info.home);
  $("dh-change").onclick = async () => {
    const p = await call("dialog:folder", "Engine home folder", info.home);
    if (p && await call("app:settings:set", { engineHome: p })) {
      toast("Engine home saved. Restart the coach to use it.");
      renderSettings();
    }
  };
  if (live.appSettings.engineHome) $("dh-default").onclick = async () => {
    if (await call("app:settings:set", { engineHome: null })) {
      toast("Default engine home saved. Restart the coach to use it.");
      renderSettings();
    }
  };
  $("dh-import").onclick = async () => {
    const p = await call("dialog:folder", "Folder to import data from");
    if (p && (await call("data:import", p))) { toast("Imported."); renderSettings(); }
  };
}
