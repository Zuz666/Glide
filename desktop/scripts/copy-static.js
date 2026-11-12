// Copies the renderer's HTML and CSS next to its compiled scripts (tsc only emits .js), plus what it shares with the
// engine's dashboard (companion/static: glide.css, the fonts, the icon sprite, inlined into each page) and
// the app icon.
const fs = require("fs");
const path = require("path");

const desk = path.join(__dirname, "..");
const src = path.join(desk, "src", "renderer");
const out = path.join(desk, "dist", "renderer");
const shared = path.join(desk, "..", "companion", "static");
fs.rmSync(path.join(out, "fonts"), { recursive: true, force: true });   // fonts the app no longer ships don't linger
fs.mkdirSync(path.join(out, "fonts"), { recursive: true });
const sprite = fs.readFileSync(path.join(shared, "icons.svg"), "utf8");
for (const f of fs.readdirSync(src)) {
  if (/\.html$/.test(f)) fs.writeFileSync(path.join(out, f), fs.readFileSync(path.join(src, f), "utf8").replace("<!--ICONS-->", sprite));
  else if (/\.(css|svg|png)$/.test(f)) fs.copyFileSync(path.join(src, f), path.join(out, f));
}
fs.copyFileSync(path.join(shared, "glide.css"), path.join(out, "glide.css"));
fs.copyFileSync(path.join(shared, "date-time-display.js"), path.join(out, "date-time-display.js"));
for (const f of fs.readdirSync(path.join(shared, "fonts"))) fs.copyFileSync(path.join(shared, "fonts", f), path.join(out, "fonts", f));
fs.copyFileSync(path.join(desk, "assets", "icon.png"), path.join(out, "icon.png"));
