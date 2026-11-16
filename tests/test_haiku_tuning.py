#!/usr/bin/env python3
"""Empty-answer budget retry and concurrent role-specific effort/accounting, without provider requests."""
import os
import sys
import threading

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
from llm_client import LLMClient  # noqa: E402
from providers import AnthropicProvider  # noqa: E402


class FakeProvider:
    """A provider that answers from a list (no network): records each body it was asked to send."""
    name, label = "anthropic", "Claude API"

    def __init__(self, replies, before=None):
        self.replies, self.sent, self.before = list(replies), [], before

    options = AnthropicProvider.options

    def request(self, body, attempt, on_text, profile=None):
        return dict(body, **attempt), False

    def fetch(self, req, stream, on_text, timeout):
        if self.before:
            self.before(req)
        self.sent.append(req)
        return self.replies.pop(0) if self.replies else {"stop_reason": "end_turn",
                                                          "content": [{"type": "text", "text": "ok"}],
                                                          "usage": {"input_tokens": 1, "output_tokens": 1}}

    def parse(self, data):
        return data

    def fatal_reason(self, code, detail):
        return None

    def caching(self, model):
        return "explicit"


# the retry: an empty, cut-off answer is asked for again with more room
notes = []
c = LLMClient({}, emit=notes.append)
fake = FakeProvider([{"stop_reason": "end_turn", "content": [{"type": "text", "text": "Next: Thendal Grove"}]}])
c.providers["anthropic"] = fake
empty = {"stop_reason": "max_tokens", "content": [{"type": "thinking", "thinking": ""}], "usage": {"output_tokens": 600}}
out = c._retry_empty({"model": "claude-opus-4-5", "max_tokens": 600, "messages": []}, {}, empty, "fast")
assert out["content"][0]["text"] == "Next: Thendal Grove" and fake.sent[0]["max_tokens"] == 2400, (out, fake.sent)
assert any("ran out of room" in n["text"] for n in notes), notes
ok = {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial but useful"}]}
assert c._retry_empty({"model": "x", "max_tokens": 600}, {}, ok, "fast") is ok, "a cut-off answer with text isn't retried"

# the same through a whole one-shot call: the empty answer, then the retry's text
fake = FakeProvider([empty, {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Go east."}],
                             "usage": {"input_tokens": 10, "output_tokens": 3}}])
c.providers["anthropic"] = fake
text, _meta = c.call_once("claude-opus-4-5", "where now?", 600)
assert text == "Go east." and [b["max_tokens"] for b in fake.sent] == [600, 2400], (text, fake.sent)
assert fake.sent[0]["output_config"] == {"effort": "low"} and "thinking" not in fake.sent[0]
print("haiku tuning: ok")

# ---- a reflection (deep) running while a callout (fast) is answered: each call books to its own path ----------
c = LLMClient({"fast_model": "claude-opus-4-5", "deep_model": "claude-opus-5-5"})
gate, started = threading.Event(), threading.Event()


def hold_deep(req):
    """The deep call waits inside its request until the fast call has been booked (the old race window)."""
    if req.get("model") == "claude-opus-5-5":
        started.set()
        assert gate.wait(5), "the fast call never finished"


usage = {"input_tokens": 100, "output_tokens": 10}
c.providers["anthropic"] = FakeProvider(
    # the fast call takes the first reply: the deep one is still held in its request
    [{"stop_reason": "end_turn", "content": [{"type": "text", "text": "Turn in."}], "usage": usage},
     {"stop_reason": "end_turn", "content": [{"type": "text", "text": "noted"}], "usage": dict(usage, output_tokens=50)}],
    before=hold_deep)
booked = []
c.usage_fn = booked.append
reflection = threading.Thread(target=lambda: c.call_once("claude-opus-5-5", "reflect", 1200, deep=True))
reflection.start()
assert started.wait(5)
assert c.call_once("claude-opus-4-5", "callout", 600, deep=False)[0] == "Turn in."
gate.set()
reflection.join(5)
assert c.path_stats["fast"]["calls"] == 1 and c.path_stats["fast"]["output"] == 10, c.path_stats
assert c.path_stats["deep"]["calls"] == 1 and c.path_stats["deep"]["output"] == 50, c.path_stats
assert sorted((u["model"], u["path"]) for u in booked) == [("claude-opus-4-5", "fast"), ("claude-opus-5-5", "deep")]
assert c.stats["calls"] == 2 and c.stats["out_tokens"] == 60, c.stats

# Each concurrent request retains its own native effort as well as its accounting path.
assert sorted(b["output_config"]["effort"] for b in c.providers["anthropic"].sent) == ["low", "medium"]
print("model paths: ok")
