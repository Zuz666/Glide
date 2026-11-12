// The app's own settings (userData/app-settings.json) as this run holds them: loaded once at start, saved whole
// after every change, and sent to the windows by whoever changed them.
import type { AppSettings, AppSettingsPatch } from "../../shared/ipc";
import { appSettingsProblem, deepMerge, loadAppSettings, Obj, saveAppSettings } from "../store";

class SettingsStore {
  current: AppSettings = {};

  load(): AppSettings {
    this.current = loadAppSettings();
    return this.current;
  }

  save(): void {
    saveAppSettings(this.current);
  }

  /** A patch from a window (objects merge; a null value removes the key), saved at once. */
  merge(patch: AppSettingsPatch): AppSettings {
    const next = deepMerge(this.current as Obj, patch as Obj) as AppSettings;
    // Runtime mute/bounds still work when a corrupt startup file deliberately blocks persistence.
    // Explicit renderer saves reject that condition in saveAppSettingsPatch before changing either file.
    if (!saveAppSettings(next) && !appSettingsProblem()) throw new Error("The app settings could not be saved.");
    this.current = next;
    return this.current;
  }
}

export const settings = new SettingsStore();
