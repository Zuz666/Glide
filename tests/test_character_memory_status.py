#!/usr/bin/env python3
"""Applied Character and memory status, independent of settings drafts and provider probes."""
import copy
import http.server
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "companion"))
from coach import Coach, CoachDeps  # noqa: E402
from glidedb import GlideDB  # noqa: E402
from memory import Memory  # noqa: E402
import embeddings  # noqa: E402
import recall  # noqa: E402
from state import GameState  # noqa: E402
from dashboard import Hub  # noqa: E402


class LocalEmbeddings(http.server.BaseHTTPRequestHandler):
    """Real HTTP, deterministic successes/failures and an explicit in-flight preparation barrier."""
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.path, body))
        if self.path == "/v1/messages":
            reply = self.server.replies.pop(0)
            out = {"id": "local", "type": "message", "role": "assistant", "model": "claude-test",
                   "content": [{"type": "text", "text": reply}], "stop_reason": "end_turn",
                   "usage": {"input_tokens": 10, "output_tokens": 10}}
        else:
            if self.server.block:
                self.server.entered.set()
                if not self.server.release.wait(5):
                    self.send_error(500)
                    return
            if self.server.fail:
                self.send_error(503, "private-provider-error secret-credential")
                return
            out = {"data": [{"index": i, "embedding": ([1.0, 0.0] if "hogger" in text.lower() else [0.0, 1.0])
                            + [0.0] * (self.server.dimensions - 2)} for i, text in enumerate(body["input"])]}
        data = json.dumps(out).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextmanager
def local_coach(persona="Khadgar"):
    with tempfile.TemporaryDirectory() as root:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LocalEmbeddings)
        server.seen, server.replies, server.fail, server.block = [], [], False, False
        server.dimensions = 2
        server.entered, server.release = threading.Event(), threading.Event()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        memory = Memory(os.path.join(root, "memory.json"))
        cfg = {"persona": persona, "recall_meaning": "ep:local", "recall_embed_model": "local-embed",
               "endpoints": [{"id": "local", "base_url": base + "/v1"}],
               "api_url": base + "/v1/messages", "api_key_env": "GLIDE_CHARACTER_STATUS_TEST_KEY",
               "grounding_log": os.path.join(root, "grounding.jsonl")}
        coach = Coach(cfg, GlideDB(os.path.join(root, "glide.db")), lambda message: None,
                      offline_ok=True, deps=CoachDeps(memory=memory))
        try:
            yield coach, memory, server
        finally:
            server.release.set()
            server.shutdown()
            server.server_close()
            thread.join(5)


def observed(coach, state):
    status = coach.character_memory_status()
    assert set(status) == {"character", "learning", "recall"}, status
    assert set(status["character"]) == {"enabled", "name"}, status
    assert set(status["learning"]) == {"enabled", "available", "reason"}, status
    assert set(status["recall"]) == {"method", "state", "source", "model", "detail"}, status
    assert status["recall"]["state"] == state, status
    serialized = json.dumps(status)
    assert "secret-credential" not in serialized and "private-provider-error" not in serialized, status
    assert "http://" not in serialized and "https://" not in serialized, status
    return status


def stored_files(root):
    files = {}
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            with open(path, "rb") as source:
                files[name] = (os.stat(path).st_mtime_ns, source.read())
    return files


def eligible(memory):
    return tuple(item["text"] for item in recall.items(memory))


def warm_in_background(coach, texts):
    worker = threading.Thread(target=coach.embedder.warm, args=(texts,))
    worker.start()
    return worker

with tempfile.TemporaryDirectory() as root:
    memory = Memory(os.path.join(root, "memory.json"))
    memory.add("Prefers short answers.")
    memory.set_learning(False)
    coach = Coach({"persona": "Khadgar", "recall_meaning": "off"},
                  GlideDB(os.path.join(root, "glide.db")), lambda message: None,
                  offline_ok=True, deps=CoachDeps(memory=memory))
    status = coach.character_memory_status()
    assert status["character"]["enabled"] and status["character"]["name"] == "Khadgar", status
    assert not status["learning"]["enabled"], status
    assert status["recall"]["method"] == "words" and status["recall"]["state"] == "words", status
    assert "Prefers short answers." in memory.block(), "Learning off must not disable existing memory"
    coach.cfg["persona"] = ""
    status = coach.character_memory_status()
    assert not status["character"]["enabled"] and status["recall"]["state"] == "waiting_character", status


# Local configuration blockers, including ones for which the engine deliberately has no Embedder.
with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"OPENAI_API_KEY": "", "GEMINI_API_KEY": "",
                                                                  "GOOGLE_API_KEY": ""}):
    for cfg, phrase in (
        ({"recall_meaning": "openai"}, "OpenAI"),
        ({"recall_meaning": "gemini"}, "Gemini"),
        ({"recall_meaning": "ep:missing", "recall_embed_model": "model"}, "endpoint"),
        ({"recall_meaning": "ep:missing"}, "model"),
        ({"recall_meaning": "telepathy"}, "not supported"),
    ):
        cfg["persona"] = "Khadgar"
        memory = Memory(os.path.join(root, "memory.json"))
        coach = Coach(cfg, GlideDB(os.path.join(root, "glide.db")), lambda message: None,
                      offline_ok=True, deps=CoachDeps(memory=memory))
        status = observed(coach, "unavailable")
        assert status["recall"]["method"] == "meaning" and phrase in status["recall"]["detail"], status
        coach.cfg["persona"] = ""
        assert phrase in observed(coach, "waiting_character")["recall"]["detail"]

    coach = Coach({"persona": "Khadgar", "recall_meaning": "openai"}, GlideDB(os.path.join(root, "glide.db")),
                  lambda message: None, offline_ok=True)
    status = observed(coach, "unavailable")
    assert not status["learning"]["available"] and "store" in status["learning"]["reason"], status
    assert "store" in status["recall"]["detail"] and "OpenAI" in status["recall"]["detail"], status

# Empty preparations and repeated observation perform no provider requests or writes.
with local_coach() as (coach, memory, server):
    status = observed(coach, "empty")
    assert status["learning"]["enabled"] and status["learning"]["available"], status
    assert coach.embedder.warm([])
    observed(coach, "empty")
    memory.add("Prefers short answers.", "preference")  # not eligible for meaning recall
    observed(coach, "empty")
    memory.add("Hogger chased us across Elwynn.", "fact")
    status = observed(coach, "unchecked")
    assert status["recall"]["source"] == "ep:local" and status["recall"]["model"] == "local-embed", status
    files = stored_files(os.path.dirname(memory.path))
    for _ in range(25):
        observed(coach, "unchecked")
    assert not server.seen, server.seen
    assert stored_files(os.path.dirname(memory.path)) == files, "Observation must never persist metadata"

# The existing full/light stream carries the entire nested projection without per-icon requests.
with local_coach() as (coach, memory, server):
    hub = Hub()
    full, _ = hub.connect()
    light, _ = hub.connect(lite=True)
    status = coach.character_memory_status()
    hub.publish("state", {"coach": {"character_memory": status}})
    for stream in (full, light):
        payload = json.loads(stream.get_nowait().split("data: ", 1)[1])
        assert payload["coach"]["character_memory"] == status, payload
    _, initial = hub.connect(lite=True)
    assert json.loads(initial.split("data: ", 1)[1])["state"]["coach"]["character_memory"] == status
    assert not server.seen

# Real preparation, partial coverage, fresh memory changes and cache-only restart evidence.
with local_coach() as (coach, memory, server):
    memory.add("Hogger chased us.", "fact")
    server.block = True
    worker = warm_in_background(coach, eligible(memory))
    try:
        assert server.entered.wait(5), "Preparation did not reach local server"
        observed(coach, "preparing")
    finally:
        server.release.set()
        worker.join(5)
    assert not worker.is_alive(), "Preparation did not finish"
    status = observed(coach, "ready")
    assert "Preparation requests succeeded" in status["recall"]["detail"], status
    assert "query path has not yet been checked" in status["recall"]["detail"], status
    assert not coach.banter.recall("Nothing relevant"), "No matching memory is not a provider error"
    observed(coach, "ready")
    memory.diary_add("moment", "Reached Goldshire.")
    observed(coach, "partial")
    memory.chapter_add("We travelled to Westfall.", ["Westfall"])
    observed(coach, "partial")
    memory.zone_seen("Redridge")
    observed(coach, "partial")
    note = memory.add("A murloc stole our lunch.", "fact")[0]
    observed(coach, "partial")
    assert coach.embedder.warm(eligible(memory))
    observed(coach, "ready")
    memory.update(note["id"], text="We recovered our lunch.")
    observed(coach, "partial")
    memory.remove(note["id"])
    observed(coach, "ready")
    count = len(server.seen)
    restarted = Coach(copy.deepcopy(coach.cfg), GlideDB(os.path.join(os.path.dirname(memory.path), "glide.db")),
                      lambda message: None, offline_ok=True, deps=CoachDeps(memory=memory))
    status = observed(restarted, "unchecked")
    assert restarted.embedder.warm(eligible(memory))  # all cache hits
    observed(restarted, "unchecked")
    assert len(server.seen) == count, "A cache-only warm must not request embeddings"
    assert restarted.embedder.query("A new question")
    observed(restarted, "ready")
    memory.remove("all")
    observed(restarted, "empty")

# Preparation and query failures recover independently; cache hits and empty warm-ups clear neither.
with local_coach() as (coach, memory, server), patch.object(embeddings, "BACKOFF_S", 0.25):
    memory.add("Hogger chased us.", "fact")
    memory.add("We reached Goldshire.", "fact")
    texts = eligible(memory)
    assert coach.embedder.warm(texts[:1])
    observed(coach, "partial")
    assert coach.embedder.query("Cached successful question")
    server.fail = True
    assert coach.embedder.query("Failing question") is None
    status = observed(coach, "error")
    assert "Query embedding failed" in status["recall"]["detail"] and "backoff" in status["recall"]["detail"], status
    count = len(server.seen)
    assert coach.embedder.query("Cached successful question")
    assert coach.embedder.query("Backed off question") is None
    assert len(server.seen) == count
    observed(coach, "error")
    server.fail = False
    assert coach.embedder.warm(texts)
    assert "Query embedding failed" in observed(coach, "error")["recall"]["detail"]
    time.sleep(0.3)
    assert coach.embedder.query("Recovered question")
    observed(coach, "ready")
    memory.add("We crossed the bridge.", "fact")
    texts = eligible(memory)
    server.fail = True
    assert not coach.embedder.warm(texts)
    assert "Memory preparation failed" in observed(coach, "error")["recall"]["detail"]
    assert coach.embedder.query("Independent failing query") is None
    detail = observed(coach, "error")["recall"]["detail"]
    assert "Memory preparation failed" in detail and "Query embedding failed" in detail, detail
    server.fail = False
    time.sleep(0.3)
    assert coach.embedder.query("Another successful question")
    assert "Memory preparation failed" in observed(coach, "error")["recall"]["detail"]
    assert coach.embedder.warm([])
    observed(coach, "error")
    server.block = True
    worker = warm_in_background(coach, texts)
    try:
        assert server.entered.wait(5)
        status = observed(coach, "error")
        assert "preparation is in progress" in status["recall"]["detail"], status
    finally:
        server.release.set()
        worker.join(5)
    assert not worker.is_alive()
    observed(coach, "ready")

# Evidence follows the actual embedder configuration, not settings drafts, URLs or changed credentials.
with local_coach() as (coach, memory, server):
    memory.add("Hogger chased us.", "fact")
    assert coach.embedder.warm(eligible(memory))
    observed(coach, "ready")
    draft = copy.deepcopy(coach.cfg)
    draft.update(persona="", recall_meaning="off")
    observed(coach, "ready")
    # The embedder is applied state even if a caller changes the selection without replacing it.
    coach.cfg.update(recall_meaning="off", recall_embed_model="another-model")
    assert observed(coach, "ready")["recall"]["model"] == "local-embed"
    coach.cfg["endpoints"][0]["base_url"] += "/other"
    observed(coach, "unchecked")
    assert coach.embedder.warm(eligible(memory))
    observed(coach, "unchecked")  # cache-only success cannot verify the changed endpoint
    assert coach.embedder.query("Current endpoint question")
    observed(coach, "ready")
    with patch.dict(os.environ, {"GLIDE_ENDPOINT_LOCAL_KEY": "secret-credential"}):
        observed(coach, "unchecked")
        assert coach.embedder.query("Current credential question")
        observed(coach, "ready")
    observed(coach, "unchecked")
    changed_model = Coach({**coach.cfg, "recall_meaning": "ep:local", "recall_embed_model": "new-model"},
                          GlideDB(os.path.join(os.path.dirname(memory.path), "glide.db")), lambda message: None,
                          offline_ok=True, deps=CoachDeps(memory=memory))
    observed(changed_model, "unchecked")
    count = len(server.seen)
    observed(changed_model, "unchecked")
    assert len(server.seen) == count

# A different service can reuse an endpoint id/model but return vectors incompatible with the disk cache.
with local_coach() as (coach, memory, server):
    memory.add("Hogger chased us.", "fact")
    assert coach.embedder.warm(eligible(memory))
    cfg = copy.deepcopy(coach.cfg)
    cfg["endpoints"][0]["base_url"] += "/replacement"
    server.dimensions = 3
    restarted = Coach(cfg, GlideDB(os.path.join(os.path.dirname(memory.path), "glide.db")),
                      lambda message: None, offline_ok=True, deps=CoachDeps(memory=memory))
    observed(restarted, "unchecked")
    assert restarted.embedder.query("Question using replacement service")
    status = observed(restarted, "unavailable")
    assert "dimension" in status["recall"]["detail"], status
    count = len(server.seen)
    observed(restarted, "unavailable")
    assert len(server.seen) == count, "Incompatible cache status must not rebuild vectors or probe the service"
    memory.add("We reached Goldshire.", "fact")
    assert restarted.embedder.warm(eligible(memory))
    status = observed(restarted, "partial")
    assert "dimension" in status["recall"]["detail"], status
    memory.remove("Hogger")
    observed(restarted, "ready")

# Runtime learning switches and conversation-produced Character changes use the real Coach path.
with local_coach(persona="") as (coach, memory, server), \
        patch.dict(os.environ, {"GLIDE_CHARACTER_STATUS_TEST_KEY": "local-only"}):
    cfg = copy.deepcopy(coach.cfg)
    cfg.update(fast_model="claude-test", deep_model="claude-test", query_model="fast", web_search=False,
               localize_info=False, two_voice=False, stream_speech=False)
    coach = Coach(cfg, coach.k, lambda message: None, offline_ok=True, deps=CoachDeps(memory=memory))
    status = observed(coach, "waiting_character")
    assert status["learning"]["available"], status  # learning does not require a Character
    memory.set_learning(False)
    assert not observed(coach, "waiting_character")["learning"]["available"]
    memory.set_learning(True)
    assert observed(coach, "waiting_character")["learning"]["available"]
    server.replies.append("Greetings.\nPERSONA: A wise mage from Dalaran.\nNAME: Khadgar\nFACTS: none")
    state = GameState()
    state.update({"t": "1000", "l": "7", "x": "2000", "X": "4500", "cl": "WARRIOR", "mp": "1426", "px": "4650", "py": "5300"},
                 {"pg": "B", "cl": "WARRIOR", "ra": "Dwarf", "fa": "Alliance", "nm": "T"})
    result = coach.answer_once("Play Khadgar.", state)
    assert result["answer"], result
    status = observed(coach, "empty")
    assert status["character"] == {"enabled": True, "name": "Khadgar"}, status
    count = len(server.seen)
    observed(coach, "empty")
    assert len(server.seen) == count
    server.replies.append("Back to normal.\nPERSONA: off\nFACTS: none")
    assert coach.answer_once("Drop the act.")["answer"]
    assert observed(coach, "waiting_character")["character"] == {"enabled": False, "name": ""}

# Offline and paused AI do not erase a configured Character or conceal an embedding failure.
with local_coach() as (coach, memory, server):
    memory.add("Hogger chased us.", "fact")
    server.fail = True
    assert not coach.embedder.warm(eligible(memory))
    coach.llm.suspended = "private-provider-error secret-credential"
    status = observed(coach, "error")
    assert status["character"]["enabled"] and not status["learning"]["available"], status
    assert "suspended" in status["learning"]["reason"] and "suspended" in status["recall"]["detail"], status
    coach.llm.suspended = None
    coach.llm.backoff_until = time.time() + 60
    assert not observed(coach, "error")["learning"]["available"]
    coach.llm.enabled = False
    status = observed(coach, "error")
    assert "offline" in status["learning"]["reason"] and status["character"]["enabled"], status
print("OK")
