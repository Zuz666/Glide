#!/usr/bin/env python3
"""The engine's HTTP server keeps other web pages out: a request naming another host (DNS rebinding) gets 403; with
a token, a POST (or /api/state, GET /memory) without it gets 403, including a cross-site form's text/plain body that
happens to be valid JSON; the page, the event stream and static files need no token; the served page carries the
token; the desktop window opens only http(s) links outside. The routes are one table (dashboard.serve's own plus
App.http_routes): a path matches exactly with its query parsed (/xph?x answers, /modelsfoo doesn't), another method
on a known path answers 405, bodies are parsed once, and the GETs that run something (/calibrate, /models?refresh=1)
need the token too. Also the hub: a client that falls behind is dropped
and its stream ends; a message is either in a new client's init or in its queue; the first-client hook runs once
(and so the app hears the startup greeting).

Run: python3 tests/test_dashboard_http.py   (prints OK)
"""
import http.client
import json
import os
import socket
import sys
import threading
import time

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import dashboard  # noqa: E402
dashboard.SHUTDOWN_POLL_S = 0.02     # shutdown() returns at once instead of after up to 0.5 s
from voice import HubVoice  # noqa: E402

TOKEN = "t-0123456789abcdef"
asked, muted = [], []
hub = dashboard.Hub()
hub.snapshot = {"level": 7}
calibrated, refreshed = [], []
routes = {
    ("POST", "/ask"): dashboard.ask_route(asked.append),
    ("GET", "/memory"): dashboard.Route(dashboard.get_json(lambda q: {"notes": []}), token=True),
    ("POST", "/mute"): dashboard.post_json(lambda body: muted.append(bool(body.get("muted"))) or {"muted": muted[-1]}),
    ("GET", "/calibrate"): dashboard.Route(dashboard.get_json(lambda q: calibrated.append(1) or {"ok": True}),
                                           token=True),
    ("GET", "/models"): dashboard.Route(dashboard.get_json(lambda q: refreshed.append(q.get("refresh") == "1")
                                                           or {"models": []}), token=True),
    ("GET", "/xph"): dashboard.get_json(lambda q: {"samples": [], "q": q}),
    ("POST", "/boom"): dashboard.post_json(lambda body: 1 / 0),
    ("GET", "/broken"): dashboard.get_json(lambda q: 1 / 0),
}
srv = dashboard.serve(hub, 0, routes, token=TOKEN)
port = srv.server_address[1]


def request(method, path, body=None, headers=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", host if host is not None else "127.0.0.1:%d" % port)
    for k, v in (headers or {}).items():
        c.putheader(k, v)
    data = body.encode("utf-8") if body is not None else b""
    if body is not None:
        c.putheader("Content-Length", str(len(data)))
    c.endheaders(data if body is not None else None)
    r = c.getresponse()
    out = (r.status, r.read())
    c.close()
    return out


ok = {"X-Glide-Token": TOKEN}
# DNS rebinding: the browser sends the attacker's name (and port) as Host
for host in ("evil.example:%d" % port, "127.0.0.1:%d" % (port + 1), "127.0.0.1", ""):
    assert request("GET", "/", host=host)[0] == 403, host
    assert request("GET", "/api/state", headers=ok, host=host)[0] == 403, host
    assert request("POST", "/ask", json.dumps({"q": "rebound"}), ok, host=host)[0] == 403, host
assert request("GET", "/", host="localhost:%d" % port)[0] == 200

# no token, a wrong one, or a cross-site form posting text/plain ("{"q":"x", "y":"=}" is valid JSON)
assert request("POST", "/ask", json.dumps({"q": "no token"}))[0] == 403
assert request("POST", "/ask", json.dumps({"q": "wrong"}), {"X-Glide-Token": "nope"})[0] == 403
assert request("POST", "/ask", '{"q":"form", "x":"=}', {"Content-Type": "text/plain"})[0] == 403
assert request("POST", "/mute", json.dumps({"muted": True}))[0] == 403
assert request("POST", "/layout", "{}")[0] == 403
assert request("GET", "/api/state")[0] == 403 and request("GET", "/memory")[0] == 403
assert request("GET", "/api/state?x=1")[0] == 403, "a query doesn't get round the token"
# the GETs that run something: a cross-site page could trigger them without the token
assert request("GET", "/calibrate")[0] == 403 and request("GET", "/models?refresh=1")[0] == 403
assert asked == [] and muted == [] and calibrated == [] and refreshed == [], (asked, muted, calibrated, refreshed)

# with the token
assert request("POST", "/ask", json.dumps({"q": "which way?"}), ok) == (200, b'{"ok":true}')
assert asked == ["which way?"]
assert request("POST", "/mute", json.dumps({"muted": True}), ok)[0] == 200 and muted == [True]
status, body = request("GET", "/api/state", headers=ok)
assert status == 200 and json.loads(body)["state"] == {"level": 7}
assert request("GET", "/memory", headers=ok)[0] == 200
assert request("GET", "/calibrate", headers=ok) == (200, b'{"ok": true}') and calibrated == [1]
assert request("GET", "/models?refresh=1", headers=ok)[0] == 200 and request("GET", "/models", headers=ok)[0] == 200
assert refreshed == [True, False], refreshed

# one table: exact paths with their query parsed, 405 for another method, consistent 404s and errors
status, body = request("GET", "/xph?since=5")
assert status == 200 and json.loads(body)["q"] == {"since": "5"}, body
assert request("GET", "/modelsfoo", headers=ok)[0] == 404 and request("GET", "/memoryx", headers=ok)[0] == 404
assert request("GET", "/index")[0] == 404 and request("GET", "/index.html")[0] == 200
status, _ = request("GET", "/ask", headers=ok)
assert status == 405, status
c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
c.request("POST", "/api/state", body="{}", headers={"X-Glide-Token": TOKEN})
r = c.getresponse()
assert r.status == 405 and r.getheader("Allow") == "GET", (r.status, r.getheader("Allow"))
r.read()
c.close()
assert request("POST", "/nowhere", "{}", ok)[0] == 404
assert request("POST", "/mute", "not json", ok)[0] == 400, "a bad body answers 400"
assert request("POST", "/mute", None, ok)[0] == 200 and muted[-1] is False, "no body reads as {}"
status, body = request("POST", "/boom", "{}", ok)
assert status == 400 and json.loads(body) == {"ok": False, "error": "division by zero"}, body
status, body = request("GET", "/broken")
assert status == 500 and json.loads(body)["ok"] is False, body

# the engine's table names every route of contract 2 (the dashboard's own page, stream, state and layout aside)
import app as glide_app  # noqa: E402


class AnyApp:
    """Stands in for App: every attribute is something callable that has attributes (coach.ask, health.snapshot)."""
    def __getattr__(self, name):
        return AnyApp()

    def __call__(self, *a, **k):
        return {}


fake = AnyApp()
table = glide_app.App.http_routes(fake)
assert {p for _, p in table} | {"/", "/events", "/api/state", "/layout"} == {
    "/", "/events", "/api/state", "/ask", "/settings", "/models", "/memory", "/reminders", "/layout", "/health",
    "/refresh", "/calibrate", "/mute", "/xph", "/community/seen", "/announce", "/localize"}, sorted(table)
needs = {k for k, r in table.items() if (r.token if isinstance(r, dashboard.Route) and r.token is not None
                                         else k[0] == "POST")}
assert {k for k in needs if k[0] == "GET"} == {("GET", "/models"), ("GET", "/memory"), ("GET", "/calibrate")}, needs
# ... and every client sends the token on those GETs (the renderer through engineGet)
state_ts = open(os.path.join(ROOT, "desktop", "src", "renderer", "state.ts"), encoding="utf-8").read()
assert "export const engineGet = (path: string) => fetch(`${live.engineUrl}${path}`, { headers: tokenHeader() });" in state_ts
for rel, call in (("settings/capture.ts", 'engineGet("/calibrate")'), ("settings/model-picker.ts", "engineGet(`/models"),
                  ("memory.ts", 'engineGet("/memory")')):
    assert call in open(os.path.join(ROOT, "desktop", "src", "renderer", *rel.split("/")), encoding="utf-8").read(), rel
dash_js = open(os.path.join(ROOT, "companion", "static", "dashboard.js"), encoding="utf-8").read()
assert "fetch('/models" not in dash_js and "fetch('/calibrate" not in dash_js and "fetch('/memory" not in dash_js

# the page, its fonts and the event stream need none; the page never carries the token (whoever opens it passes it
# in the URL's fragment), so a program that fetches the page doesn't get it
status, page = request("GET", "/")
assert status == 200 and TOKEN.encode() not in page and b"glide-token" not in page, "the token isn't in the page"
assert request("GET", "/fonts/geist.woff2")[0] == 200
s = socket.create_connection(("127.0.0.1", port), timeout=5)
s.sendall(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % port)
got = b""
while b"event: init" not in got:
    got += s.recv(65536)
assert got.startswith(b"HTTP/1.0 200"), got[:80]

# a client that falls behind (its queue full) is dropped, and its stream ends so it reconnects
dashboard.PING_S = 0.2
# The client never reads: once the socket's buffers are full the server's writer blocks and the queue fills. How many
# messages that takes depends on the OS's buffers and on how fast the writer drains them (a busy machine, a parallel
# test run), so publish until the client is dropped, within a bound, rather than a fixed count that sits at the edge.
deadline, sent = time.time() + 20, 0
while hub.clients and time.time() < deadline and sent < 20000:
    hub.publish("speak", {"text": "x" * 20000, "interrupt": False})
    sent += 1
assert not hub.clients, "the slow client was dropped (after %d messages)" % sent
s.settimeout(0.5)
deadline, closed = time.time() + 10, False
while time.time() < deadline and not closed:
    try:
        closed = s.recv(1 << 20) == b""          # drain what was queued; the server then closes
    except socket.timeout:
        pass
assert closed, "the dropped client's stream ended"
s.close()
srv.shutdown()

# without a token (the tests' servers) POSTs work as before
srv2 = dashboard.serve(dashboard.Hub(), 0, {("POST", "/ask"): dashboard.ask_route(asked.append)})
port = srv2.server_address[1]
assert request("POST", "/ask", json.dumps({"q": "open"}))[0] == 200 and asked[-1] == "open"
assert b"glide-token" not in request("GET", "/")[1]
srv2.shutdown()

# connect(): the snapshot and the recent messages, and every later message in the queue (taken under one lock)
h = dashboard.Hub()
h.publish("message", {"text": "before"})
q, init_event = h.connect()
assert '"before"' in init_event and q.empty()
h.publish("message", {"text": "after"})
assert '"after"' in q.get_nowait() and h.connected(q)
h.disconnect(q)
assert not h.connected(q)

def settle(before):
    """Wait for every thread started since `before` (the hooks' timers, and anything they start) to finish: the checks
    below then see all that a connect() set off, without sleeping a fixed time."""
    while True:
        new = [t for t in threading.enumerate() if t not in before and t is not threading.current_thread()]
        if not new:
            return
        for t in new:
            t.join(10)
            assert not t.is_alive(), "a thread the connect started is still running: %r" % t
        before = before | set(new)


# The first-client hook runs once, shortly after the first client connects.
dashboard.FIRST_CLIENT_DELAY_S = 0.05
h = dashboard.Hub()
HubVoice({"enabled": True, "announce": True}, h)
HubVoice({"enabled": True, "announce": False}, h)       # announce off: says nothing
assert not h.clients, "nothing said before anyone listens"
before = set(threading.enumerate())
q, _ = h.connect()
q2, _ = h.connect()
settle(before)
said = [q.get_nowait() for _ in range(q.qsize())]
assert len(said) == 1 and said[0].startswith("event: speak"), said
assert q2.qsize() == 1, "the hook waited until both windows were listening"
before = set(threading.enumerate())
q3, _ = h.connect()
settle(before)
assert q3.empty(), "only once"
muted_voice = HubVoice({"enabled": True, "announce": True}, dashboard.Hub())
muted_voice.muted = True
before = set(threading.enumerate())
q4, _ = muted_voice.hub.connect()
settle(before)
assert q4.empty(), "muted by the time the app connects: silent"
# the desktop window opens only web links outside (a link to a file or another app's scheme is dropped)
win_ts = open(os.path.join(ROOT, "desktop", "src", "main", "app", "main-window.ts"), encoding="utf-8").read()
opens = [ln for ln in win_ts.splitlines() if "shell.openExternal(" in ln]
assert len(opens) == 2 and all(r"/^https?:\/\//.test(url)" in ln for ln in opens), opens
print("OK")
