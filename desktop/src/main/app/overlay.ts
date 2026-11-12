// The click-through overlay over the game (renderer/overlay.html): opt-in, always on top, and moved either by
// unlocking it (Ctrl+Alt+O, the tray, Settings) or by holding the drag key over it.
import { BrowserWindow, screen } from "electron";
import path from "path";
import type { EventChannel, EventMap } from "../../shared/ipc";
import { dragVk, keyDown, keysAvailable, VK } from "../keys";
import { PRELOAD, RENDERER, isOwnPage } from "./assets";
import { companionSection } from "./config";
import { settings } from "./settings";

export class Overlay {
  win: BrowserWindow | null = null;
  unlocked = false;
  // Hold the drag key (Alt; config companion.overlay.drag_key) with the mouse over the overlay: it takes the mouse
  // while the key is down, so drag moves it, the wheel changes its width and right-click collapses it, like the old
  // Python overlay. The window is moved here, following the cursor, until the left button is released.
  private held = false;
  private drag: { cursor: Electron.Point; pos: number[] } | null = null;
  private dragKeyVk = VK.ALT;

  /** broadcast: send to every window; onLockChange: the unlocked state changed (the tray shows it). */
  constructor(private broadcast: <K extends EventChannel>(channel: K, payload: EventMap[K]) => void,
              private onLockChange: () => void) {}

  send<K extends EventChannel>(channel: K, payload: EventMap[K]): void {
    if (this.win && !this.win.isDestroyed()) this.win.webContents.send(channel, payload);
  }

  /** Open or close it as the settings say (opt-in: Settings > Overlay or the tray). */
  sync(): void {
    const want = settings.current.overlay?.enabled === true;
    if (want && !this.win) this.create();
    if (!want && this.win) this.win.close();
  }

  private create(): void {
    const o = settings.current.overlay || {};
    const win = new BrowserWindow({
      x: o.x ?? 20, y: o.y ?? 140, width: o.width || 560, height: o.height || 120,
      transparent: true, frame: false, resizable: false, movable: true, skipTaskbar: true, focusable: false,
      alwaysOnTop: true, hasShadow: false, show: false, title: "Glide overlay",
      webPreferences: { preload: PRELOAD, contextIsolation: true, sandbox: true,
                        backgroundThrottling: false },
    });
    this.win = win;
    win.setAlwaysOnTop(true, "screen-saver");
    win.setIgnoreMouseEvents(true);
    win.setVisibleOnAllWorkspaces(true);
    win.loadFile(path.join(RENDERER, "overlay.html"));
    // the overlay shows its own page only: no new windows, no navigation away (a dropped file, a link)
    win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    win.webContents.on("will-navigate", (e, url) => { if (!isOwnPage(url)) e.preventDefault(); });
    win.once("ready-to-show", () => this.win?.showInactive());
    win.on("closed", () => (this.win = null));
  }

  /** Unlocking enables mouse editing without taking the game's keyboard focus (push-to-talk is focus-gated).
   * Transparent windows can't be resized by their edges on Windows. */
  setUnlocked(on: boolean): void {
    if (!this.win) return;
    this.unlocked = on;
    this.win.setIgnoreMouseEvents(!on && !this.held);
    if (!on) this.saveBounds();
    this.broadcast("overlay:unlocked", on);
    this.onLockChange();
  }

  private saveBounds(): void {
    if (!this.win) return;
    settings.current.overlay = { ...settings.current.overlay, ...this.win.getBounds() };
    settings.save();
  }

  /** The window follows its content's height (the renderer measures it). */
  fitHeight(height: number): void {
    if (!this.win) return;
    const b = this.win.getBounds();
    const hgt = Math.max(24, Math.min(600, Math.round(height)));
    if (hgt !== b.height) this.win.setBounds({ ...b, height: hgt });
  }

  /** Wider or narrower (its buttons, or the wheel while held), remembered. */
  changeWidth(delta: number): void {
    if (!this.win) return;
    const b = this.win.getBounds();
    this.win.setBounds({ ...b, width: Math.max(240, Math.min(1600, b.width + Math.round(delta))) });
    this.saveBounds();
  }

  /** The left button went down on it while it takes the mouse: follow the cursor until the button is released. */
  startDrag(): void {
    if (!this.win || !(this.held || this.unlocked)) return;
    this.drag = { cursor: screen.getCursorScreenPoint(), pos: this.win.getPosition() };
  }

  /** Re-read the drag key from config.json (after a Settings save). */
  readDragKey(): void {
    this.dragKeyVk = dragVk(companionSection("overlay").drag_key);
  }

  /** Poll the drag key and the mouse button (the game has the focus, so no key events reach the overlay). False
   * when this PC can't read the keys. */
  startKeyPolling(): boolean {
    if (!keysAvailable()) return false;
    setInterval(() => this.pollKeys(), 25);
    return true;
  }

  private pollKeys(): void {
    const win = this.win;
    if (!win || win.isDestroyed()) return;
    if (this.drag) {
      if (!keyDown(VK.LBUTTON)) {
        this.drag = null;
        this.saveBounds();
      } else {
        const c = screen.getCursorScreenPoint();
        win.setPosition(this.drag.pos[0] + c.x - this.drag.cursor.x, this.drag.pos[1] + c.y - this.drag.cursor.y);
      }
    }
    const c = screen.getCursorScreenPoint(), b = win.getBounds();
    const inside = c.x >= b.x && c.x < b.x + b.width && c.y >= b.y && c.y < b.y + b.height;
    const held = keyDown(this.dragKeyVk) && (inside || !!this.drag);
    if (held !== this.held) {
      this.held = held;
      if (!this.unlocked) win.setIgnoreMouseEvents(!held);
      this.broadcast("overlay:held", held);
    }
  }
}
