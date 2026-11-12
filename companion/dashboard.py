"""Local dashboard: http://127.0.0.1:8765 (state + coach feed over Server-Sent Events, ask box)."""
import base64
import hashlib
import hmac
import json
import os
import re
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import icons
import sounds
import jsonfile


# the desktop app's windows (main window, overlay, the dashboard frame) connect at about the same moment; the first
# client's hook waits this long so all of them are listening when it runs
FIRST_CLIENT_DELAY_S = 1.5
PING_S = 15                     # an idle event stream gets a comment line this often (and notices it was dropped)
SHUTDOWN_POLL_S = 0.5           # how soon the server notices shutdown() (the engine never calls it; tests lower it)


# The light stream (/events?lite=1) for the desktop app's overlay and session bar: its "state" events and init carry
# only these keys (desktop/src/shared/engine.ts LiteState): the overlay's step line and waiting text, the footer's time,
# cost, models and mode, the walking map and rebuild progress and the reminders, and who is playing which RestedXP
# guide step (the Guides page's "Your step"). Everything else on that stream ("message", "speak") is as on the full one.
LITE_KEYS = ("overlay_step", "frames", "level", "zone",
             "session_minutes", "coach", "models", "refresh", "navbuild", "reminders",
             "name", "class", "race", "faction", "guide", "guide_group", "step", "rxp_settings")
# State keys that change rarely and are large (the community notes are half the state): they go out on their own
# event when they change, not with every state. A new client's init and /api/state still carry them in the state.
SIDE_KINDS = ("community",)


class Hub:
    def __init__(self):
        self.clients = []
        self.lite = set()               # the clients on the light stream
        self.lock = threading.Lock()
        self.messages = []
        self.snapshot = {}
        self.side = {}                  # the last of each SIDE_KINDS event: part of the state for init and /api/state
        self._first_client = []         # one-shot callbacks for the first event-stream client
        self._spoken = None            # serialized completion, replayed after init; never a speech command

    def publish(self, kind, data):
        """"state" replaces the snapshot, "message" joins the feed, a SIDE_KINDS kind ("community") replaces that key
        of the state; "voice_spoken" retains the last completion for reconnecting clients. Other kinds ("speak")
        only go to clients connected now. The light stream gets the state's LITE_KEYS and no SIDE_KINDS events.
        A client too slow to keep up (its queue full) is dropped; its stream then ends so it reconnects."""
        payload = sse(kind, data)
        lite = payload
        if kind == "state":
            lite = sse("state", lite_state(data))
        elif kind in SIDE_KINDS:
            lite = None
        with self.lock:            # with connect(): a message is either in a new client's init or in its queue
            if kind == "message":
                self.messages = (self.messages + [data])[-60:]
            elif kind == "state":
                self.snapshot = data
            elif kind in SIDE_KINDS:
                self.side[kind] = data
            elif kind == "voice_spoken":
                if self._spoken is not None and data["at"] < self._spoken[0]:
                    return
                self._spoken = (data["at"], payload)
            for q in list(self.clients):
                p = lite if q in self.lite else payload
                if p is None:
                    continue
                try:
                    q.put_nowait(p)
                except queue.Full:
                    self.clients.remove(q)
                    self.lite.discard(q)

    def state(self):
        """The whole state: the last "state" with the last of each SIDE_KINDS event in it (init, /api/state)."""
        with self.lock:
            return {**self.snapshot, **self.side}

    def connect(self, lite=False):
        """A new event-stream client: its queue and its "init" event (the state and the recent messages; on the
        light stream, the state's LITE_KEYS)."""
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.clients.append(q)
            state = {**self.snapshot, **self.side}
            if lite:
                self.lite.add(q)
                state = lite_state(state)
            init = sse("init", {"state": state, "messages": self.messages})
            if self._spoken is not None:
                q.put_nowait(self._spoken[1])
            hooks, self._first_client = self._first_client, []
        for fn in hooks:
            t = threading.Timer(FIRST_CLIENT_DELAY_S, fn)
            t.daemon = True
            t.start()
        return q, init

    def connected(self, q):
        with self.lock:
            return q in self.clients

    def disconnect(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)
            self.lite.discard(q)

    def on_first_client(self, fn):
        """Run fn once, shortly after the first event-stream client connects (a "speak" published before anyone
        listens is lost: the start-up announcement)."""
        with self.lock:
            self._first_client.append(fn)


def sse(kind, data):
    """One server-sent event."""
    return "event: %s\ndata: %s\n\n" % (kind, json.dumps(data, default=str))


def lite_state(s):
    """The light stream's state: LITE_KEYS of a state (the keys it has; before the first state, none)."""
    return {k: s[k] for k in LITE_KEYS if k in s}


MAPS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "gamedata", "maps")


PANEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def load_layout(path):
    """The saved panel layout {"cols": [[panel ids], ...], "collapsed": [panel ids]} or {} (defaults)."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return clean_layout(json.load(fh))
    except (OSError, ValueError):
        return {}


def clean_layout(d):
    if not isinstance(d, dict):
        raise ValueError("layout must be an object")
    cols = d.get("cols") or []
    if not isinstance(cols, list) or len(cols) > 4:
        raise ValueError("cols: up to 4 lists of panel ids")
    seen, out = set(), []
    for col in cols:
        ids = []
        for p in col if isinstance(col, list) else []:
            if isinstance(p, str) and PANEL_RE.match(p) and p not in seen:
                seen.add(p)
                ids.append(p)
        out.append(ids)
    collapsed = sorted({p for p in d.get("collapsed") or [] if isinstance(p, str) and PANEL_RE.match(p)})
    lay = {"cols": out, "collapsed": collapsed}
    strip = d.get("strip")         # the order of the top bar's tiles
    if isinstance(strip, list):
        tiles = [t for t in strip if isinstance(t, str) and PANEL_RE.match(t)]
        lay["strip"] = list(dict.fromkeys(tiles))[:16]
    split = d.get("split")         # the first column's share of the width (the draggable divider), 0.25..0.8
    if isinstance(split, (int, float)) and not isinstance(split, bool) and 0.25 <= split <= 0.8:
        lay["split"] = round(float(split), 4)
    arr = d.get("arrange")         # the HUD's arrangement: each group's order, and which panel sits in which slot
    if isinstance(arr, dict):
        lists = {}
        for g, ids in (arr.get("lists") or {}).items() if isinstance(arr.get("lists"), dict) else []:
            if isinstance(g, str) and PANEL_RE.match(g) and isinstance(ids, list):
                lists[g] = list(dict.fromkeys(i for i in ids if isinstance(i, str) and PANEL_RE.match(i)))[:16]
        slots = {}
        for s, p in (arr.get("slots") or {}).items() if isinstance(arr.get("slots"), dict) else []:
            if isinstance(s, str) and PANEL_RE.match(s) and isinstance(p, str) and PANEL_RE.match(p) \
                    and p not in slots.values():
                slots[s] = p
        lay["arrange"] = {"lists": dict(list(lists.items())[:12]), "slots": dict(list(slots.items())[:12])}
    return lay


def save_layout(path, d):
    lay = clean_layout(d)
    if path:
        jsonfile.write_atomic(path, lay, indent=1)
    return lay


class Request:
    """One request as a route sees it: `path` without the query, `query` ({name: first value}), and json()."""

    def __init__(self, method, path, query, read_body):
        self.method, self.path, self.query = method, path, query
        self._read_body = read_body

    def json(self):
        """The body as JSON ({} when there is none); raises ValueError when it isn't JSON."""
        raw = self._read_body()
        return json.loads(raw.decode("utf-8")) if raw.strip() else {}


class Route:
    """A route's handler, fn(Request) -> (status, body): a dict or list is sent as JSON, a str as it is. token: the
    request must carry this run's token (None: POSTs do, GETs don't)."""

    def __init__(self, fn, token=None):
        self.fn, self.token = fn, token


def get_json(fn, on_error=None):
    """A GET handler answering fn(query) as JSON; with on_error, an exception answers 200 on_error(e) instead."""
    def handle(req):
        try:
            return 200, fn(req.query)
        except Exception as e:  # noqa: BLE001
            if on_error is None:
                raise
            return 200, on_error(e)
    return handle


def post_json(fn, reply=lambda res: res):
    """A POST handler: fn(the JSON body, {} unless it is an object) answers {"ok": true, **reply(result)}; a bad body
    or an exception answers 400 {"ok": false, "error"}."""
    def handle(req):
        try:
            body = req.json()
            res = fn(body if isinstance(body, dict) else {})
        except Exception as e:  # noqa: BLE001
            return 400, {"ok": False, "error": str(e)}
        return 200, {"ok": True, **(reply(res) or {})}
    return handle


def ask_route(on_ask):
    """POST /ask {"q"}: hands the question to the coach; always {"ok":true} (an empty or bad body asks nothing)."""
    def handle(req):
        try:
            q = str(req.json().get("q", "")).strip()
        except Exception:  # noqa: BLE001 - a bad body is the caller's; it asks nothing
            q = ""
        if q:
            on_ask(q)
        return 200, '{"ok":true}'
    return handle


def announce_route(on_announce):
    """POST /announce {"what": "available" | "ready", "version", "title", "notes": [what's new]}: the desktop app's
    update notice; the coach tells the player, in its own voice (Coach.announce_update). Always {"ok":true} (a body
    without a version says nothing)."""
    def handle(req):
        try:
            d = req.json()
            d = d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001 - a bad body is the caller's; it says nothing
            d = {}
        version = str(d.get("version") or "").strip()[:40]
        if version:
            notes = d.get("notes")
            notes = notes if isinstance(notes, list) else []
            on_announce("ready" if d.get("what") == "ready" else "available", version,
                        str(d.get("title") or "").strip()[:120], [str(x)[:400] for x in notes if str(x).strip()][:12])
        return 200, '{"ok":true}'
    return handle


def _layout_post(layout_path):
    """POST /layout: save the panel layout (cleaned); a body that isn't a layout answers 400."""
    def handle(req):
        try:
            lay = save_layout(layout_path, req.json())
        except Exception as e:  # noqa: BLE001
            return 400, {"ok": False, "error": str(e)}
        return 200, {"ok": True, "layout": lay}
    return handle


# GET files by name: (pattern, where from, content type, the 404 text)
FILE_ROUTES = (
    (re.compile(r"^/fonts/([a-z0-9-]{1,40}\.woff2)$"), "fonts", "font/woff2", "no such font"),
    (re.compile(r"^/map/(\d{1,6})\.png$"), "map", "image/png", "no map image"),
    (re.compile(r"^/icon/(\d{1,7})\.png$"), "icon", "image/png", "no icon"),
    (re.compile(r"^/sound/([a-z]{1,20})\.ogg$"), "sound", "audio/ogg", "no sound"),
)


def serve(hub, port, routes=None, layout_path=None, token=None):
    """Serve the dashboard on 127.0.0.1:port: the page, its event stream (/events), /api/state, /layout and the
    static files, plus `routes` ({(method, path): Route or handler}: the engine's own, app.py App.http_routes). A
    path matches exactly (the query goes to the handler); a known path asked with another method answers 405,
    anything else 404. Every request must name this server in its Host header (so a web page can't reach it through
    a rebound DNS name). With a token, every POST and each route marked token=True (/api/state, and the engine's
    GET /memory, /models and /calibrate) must carry it in X-Glide-Token. The page never carries it: whoever opens
    the page hands it over in the URL's fragment (/#t=<token>), which a browser never sends to a server, so neither
    another web page nor another program that fetches the page gets it. token=None (the tests) asks for none."""
    page = PAGE
    # the page's security policy: only its own inline script (by hash) runs, and it loads only from this server
    script = page[page.index("<script>") + len("<script>"):page.rindex("</script>")]
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    csp = ("default-src 'self'; script-src 'sha256-%s'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
           "media-src 'self'; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
           "form-action 'none'" % digest)
    table = {
        ("GET", "/"): Route(lambda req: (200, page)),
        ("GET", "/index.html"): Route(lambda req: (200, page)),
        ("GET", "/api/state"): Route(lambda req: (200, {"state": hub.state(), "messages": hub.messages}), token=True),
        ("GET", "/layout"): Route(lambda req: (200, load_layout(layout_path))),
        ("POST", "/layout"): Route(_layout_post(layout_path)),
    }
    for key, r in (routes or {}).items():
        table[key] = r if isinstance(r, Route) else Route(r)
    methods = {"/events": {"GET"}}                  # path -> the methods it answers (for 405)
    for method, p in table:
        methods.setdefault(p, set()).add(method)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _body(self):
            """The request body, read once (a reply sent with it unread resets the connection before the client
            sees it)."""
            if not hasattr(self, "_raw"):
                n = int(self.headers.get("Content-Length", 0) or 0)
                self._raw = self.rfile.read(n) if 0 < n <= 1 << 20 else b""
            return self._raw

        def _host_ok(self):
            p = self.server.server_address[1]
            return self.headers.get("Host", "") in ("127.0.0.1:%d" % p, "localhost:%d" % p)

        def _token_ok(self):
            return token is None or hmac.compare_digest(self.headers.get("X-Glide-Token", "").encode("utf-8"),
                                                        token.encode("utf-8"))

        def _send(self, code, body, ctype, cache="no-store", headers=()):
            b = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def _send_json(self, code, obj):
            self._send(code, json.dumps(obj, default=str), "application/json")

        def _dispatch(self, method):
            self._body()
            if not self._host_ok():
                return self._send_json(403, {"ok": False, "error": "forbidden"})
            url = urlsplit(self.path)
            route = table.get((method, url.path))
            if route is None:
                if method == "GET" and url.path == "/events":
                    return self._events(parse_qs(url.query).get("lite") == ["1"])
                for pattern, where, ctype, missing in FILE_ROUTES if method == "GET" else ():
                    m = pattern.match(url.path)
                    if m:
                        return self._file(where, m.group(1), ctype, missing)
                if url.path in methods:
                    return self._send(405, "method not allowed", "text/plain",
                                      headers=[("Allow", ", ".join(sorted(methods[url.path])))])
                return self._send(404, "not found", "text/plain")
            if (route.token if route.token is not None else method == "POST") and not self._token_ok():
                return self._send_json(403, {"ok": False, "error": "forbidden"})
            query = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
            try:
                code, body = route.fn(Request(method, url.path, query, self._body))
            except Exception as e:  # noqa: BLE001 - a broken handler answers; it doesn't drop the connection
                code, body = 500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            if body is page:
                return self._send(code, body, "text/html; charset=utf-8", headers=(("Content-Security-Policy", csp),))
            if isinstance(body, str):
                return self._send(code, body, "application/json")
            return self._send_json(code, body)

        def _file(self, where, name, ctype, missing):
            if where in ("icon", "sound"):
                data = icons.icon_png(name) if where == "icon" else sounds.sound_ogg(name)
                return self._send(200, data, ctype, "max-age=86400") if data else self._send(404, missing, "text/plain")
            path = os.path.join(STATIC, "fonts", name) if where == "fonts" else os.path.join(MAPS_DIR, name + ".png")
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    return self._send(200, fh.read(), ctype)
            return self._send(404, missing, "text/plain")

        def _events(self, lite=False):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q, init = hub.connect(lite)
            try:
                self.wfile.write(init.encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=PING_S)
                    except queue.Empty:
                        msg = ": ping\n\n"
                    if not hub.connected(q):
                        break       # dropped for falling behind: end the stream so the client reconnects
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.disconnect(q)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, args=(SHUTDOWN_POLL_S,), daemon=True).start()
    return srv


STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def build_page():
    """The dashboard as one document: static/dashboard.html with the shared tokens (glide.css), its own styles and
    script, and the icon sprite inlined (the desktop app shows it in an iframe; fonts load from /fonts/)."""
    def read(name):
        with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
            return fh.read()
    page = read("dashboard.html")
    for mark, name in (("/*GLIDE_CSS*/", "glide.css"), ("/*DASHBOARD_CSS*/", "dashboard.css"),
                       ("<!--ICONS-->", "icons.svg"), ("/*DATE_TIME_JS*/", "date-time-display.js"),
                       ("/*DASHBOARD_JS*/", "dashboard.js")):
        page = page.replace(mark, read(name), 1)
    return page


PAGE = build_page()