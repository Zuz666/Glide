"""The model providers behind llm_client.LLMClient: how one request is built, sent and read back for each API.

AnthropicProvider talks to the Claude API (the Messages body as is, streamed when early speech wants it);
OpenAICompatProvider(flavor) to the OpenAI-compatible ones: "openrouter", "gemini" (Gemini direct) and "endpoint"
(a custom server), through openai_compat's translation. The client picks one per model (LLMClient.provider_for) and
keeps everything provider-neutral itself: the billing guard, retries, caching markers, accounting."""
import io
import json
import urllib.error
import urllib.request
from typing import Protocol

import openai_compat
import openai_responses
import reasoning
import settings
from log import log_once

API_LABEL = {"anthropic": "Claude API", "gemini": "Gemini API", "openrouter": "OpenRouter",
             "endpoint": "custom endpoint"}
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


STREAM_ERROR_CODE = {"overloaded_error": 529, "rate_limit_error": 429, "api_error": 500, "timeout_error": 504}


def read_stream(resp, on_text, url=""):
    """Read a streamed Messages API reply (server-sent events) into the same dict a plain request returns: content
    blocks rebuilt from their deltas (text, tool and server-tool inputs, thinking with its signature, citations),
    stop_reason and usage. Each text delta goes to on_text as it arrives. An error event mid-stream raises the
    HTTPError a plain request would have, so the caller's retry and back-off rules apply unchanged."""
    msg, blocks, partial = {}, {}, {}
    usage = {}
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except ValueError:
            continue
        kind = ev.get("type")
        if kind == "message_start":
            msg = dict(ev.get("message") or {})
            usage.update(msg.get("usage") or {})
        elif kind == "content_block_start":
            blocks[ev["index"]] = dict(ev.get("content_block") or {})
        elif kind == "content_block_delta":
            b, d = blocks.setdefault(ev["index"], {}), ev.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta":
                b["text"] = b.get("text", "") + d.get("text", "")
                if on_text and d.get("text"):
                    try:
                        on_text(d["text"])
                    except Exception as e:  # noqa: BLE001 - speech is an extra; the reply still completes
                        log_once("llm.on_text", e)
            elif dt == "input_json_delta":
                partial[ev["index"]] = partial.get(ev["index"], "") + d.get("partial_json", "")
            elif dt == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
            elif dt == "signature_delta":
                b["signature"] = d.get("signature", "")
            elif dt == "citations_delta" and d.get("citation"):
                b.setdefault("citations", []).append(d["citation"])
        elif kind == "content_block_stop":
            i = ev.get("index")
            if i in partial:
                try:
                    blocks[i]["input"] = json.loads(partial.pop(i) or "{}")
                except ValueError:
                    blocks[i]["input"] = {}
        elif kind == "message_delta":
            d = ev.get("delta") or {}
            for k in ("stop_reason", "stop_sequence"):
                if k in d:
                    msg[k] = d[k]
            usage.update({k: v for k, v in (ev.get("usage") or {}).items() if v is not None})
        elif kind == "error":
            err = ev.get("error") or {}
            raise urllib.error.HTTPError(url, STREAM_ERROR_CODE.get(str(err.get("type")), 500), err.get("message") or "stream error",
                                         None, io.BytesIO(json.dumps(ev).encode("utf-8")))  # type: ignore[arg-type]
    msg["content"] = [blocks[i] for i in sorted(blocks)]
    msg["usage"] = usage
    return msg


class Provider(Protocol):
    """What the client needs from a provider. name: models.provider()'s id ("anthropic", "gemini", "openrouter",
    "endpoint"); label: how messages name it ("Claude API", an endpoint's own label); quiet_fallback: a refused
    option set falls back to the next without telling the player (Gemini's effort probing)."""
    name: str
    label: str
    quiet_fallback: bool

    def option_sets(self, model, opts, deep, effort="auto") -> list: ...

    def request(self, body, attempt, on_text) -> tuple: ...

    def fetch(self, req, stream, on_text, timeout) -> dict: ...

    def parse(self, data) -> dict: ...

    def fatal_reason(self, code, detail) -> "str | None": ...

    def caching(self, model) -> str: ...

    def accepted(self, model, attempt, deep=False) -> None: ...


def _json_request(url, body, headers):
    return urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)


class _HTTP:
    def fetch(self, req, stream, on_text, timeout):
        """Send one request -> the reply as JSON (a streamed one read with read_stream). HTTP errors raise."""
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return read_stream(r, on_text, req.full_url) if stream else json.loads(r.read().decode("utf-8"))


class AnthropicProvider(_HTTP):
    """The Claude API: Messages requests as they are, the tuning options (thinking/effort) merged in, streamed
    when a caller wants the text as it arrives."""
    name, label, quiet_fallback = "anthropic", API_LABEL["anthropic"], False

    def __init__(self, cfg, key):
        self.cfg, self.key = cfg, key

    def option_sets(self, model, opts, deep, effort="auto"):
        """Explicit effort overrides automatic tuning, never the reply's structured-output schema."""
        if effort != "auto":
            selected = {k: v for k, v in opts.items() if k != "thinking"}
            selected["output_config"] = dict(opts.get("output_config") or {}, effort=effort)
            return [selected]
        return [opts, {}] if opts else [{}]

    def request(self, body, attempt, on_text):
        """-> (urllib Request, whether the reply is streamed)."""
        messages = body.get("messages") or []
        private = (openai_compat.META_BLOCK, openai_responses.META_BLOCK)
        if any(isinstance(m.get("content"), list) and any(b.get("type") in private for b in m["content"]
                                                        if isinstance(b, dict)) for m in messages):
            body = dict(body, messages=[dict(m, content=[b for b in m["content"]
                                       if not isinstance(b, dict) or b.get("type") not in private])
                                       if isinstance(m.get("content"), list) else m for m in messages])
        stream = bool(on_text) and settings.Settings("llm", self.cfg).get("stream_speech")
        return _json_request(self.cfg.get("api_url", ANTHROPIC_URL),
                             dict({**body, **attempt}, **({"stream": True} if stream else {})),
                             {"x-api-key": self.key, "anthropic-version": "2023-06-01",
                              "content-type": "application/json"}), stream

    def parse(self, data):
        return data

    @staticmethod
    def fatal_reason(code, detail):
        """Errors that won't fix themselves by retrying -> short reason, else None."""
        d = detail.lower()
        if "credit balance" in d or "billing" in d or "purchase credits" in d:
            return "out of API credits"
        if code == 401 or "invalid x-api-key" in d or "authentication" in d:
            return "API key rejected"
        if code == 403 or "permission" in d or ("disabled" in d and any(w in d for w in ("organization", "account", "api key"))):
            return "API access denied"
        return None

    def caching(self, model):
        return "explicit"

    def accepted(self, model, attempt, deep=False):
        pass


class OpenAICompatProvider(_HTTP):
    """An OpenAI-style chat/completions API (openai_compat translates both ways). flavor "openrouter", "gemini" or
    "endpoint" (then url and label are the endpoint's own). Gemini remembers the reasoning effort each model
    accepted, so later calls send it straight away."""

    def __init__(self, flavor, cfg, key, url=None, label=None):
        self.name = self.flavor = flavor
        self.cfg, self.key, self.url = cfg, key, url
        self.label = label or API_LABEL[flavor]
        self.quiet_fallback = flavor == "gemini"
        self.effort_ok = {}                   # (Gemini model, path) -> accepted automatic tuning

    def option_sets(self, model, opts, deep, effort="auto"):
        """An explicit level has no fallback; Auto keeps Gemini's existing low-latency tuning."""
        if effort != "auto":
            return [{"reasoning": {"effort": effort}}] if self.flavor == "openrouter" else [{"reasoning_effort": effort}]
        if self.flavor != "gemini" or not settings.Settings("llm", self.cfg).get("tune_thinking"):
            return [{}]
        if (model, deep) in self.effort_ok:
            return [self.effort_ok[model, deep]]
        first = openai_compat.gemini_effort(model, deep)
        attempts = [first]
        if first.get("reasoning_effort") in ("minimal", "none"):
            attempts.append({"reasoning_effort": "low"})
        attempts.append({})
        return [a for i, a in enumerate(attempts) if a not in attempts[:i]]

    def request(self, body, attempt, on_text):
        """-> (urllib Request, False): these replies are never streamed."""
        if self.flavor == "gemini":
            return _json_request(self.cfg.get("gemini_url", openai_compat.GEMINI_URL),
                                 {**openai_compat.to_openai(body, "gemini"), **attempt},
                                 {"authorization": "Bearer " + self.key, "content-type": "application/json"}), False
        if self.flavor == "openrouter":
            return _json_request(self.cfg.get("openrouter_url", openai_compat.URL), {**openai_compat.to_openai(body), **attempt},
                                 {"authorization": "Bearer " + self.key, "content-type": "application/json",
                                  "x-title": "Glide coach"}), False
        headers = {"content-type": "application/json"}
        if self.key:
            headers["authorization"] = "Bearer " + self.key
        profile = reasoning.describe(body["model"], self.cfg)
        if profile["transport"] == "responses":
            assert self.url is not None
            url = self.url.removesuffix("/chat/completions") + "/responses"
            return _json_request(url, openai_responses.to_responses(body, attempt.get("reasoning_effort")), headers), False
        limit_field = profile.get("limit_field") or "max_tokens"
        converted = openai_compat.to_openai(body, "endpoint", limit_field=limit_field)
        return _json_request(self.url, {**converted, **attempt}, headers), False

    def parse(self, data):
        if self.flavor == "endpoint" and data.get("object") == "response":
            return openai_responses.from_responses(data)
        return openai_compat.from_openai(data)

    def fatal_reason(self, code, detail):
        if self.flavor == "gemini":
            return openai_compat.gemini_fatal(code, detail)
        return openai_compat.fatal(code, detail)

    def caching(self, model):
        """OpenRouter honours cache_control markers for Anthropic and Google models; the rest cache implicitly."""
        if self.flavor == "openrouter" and (model or "").startswith(openai_compat.CACHE_VENDORS):
            return "explicit"
        return "implicit"

    def accepted(self, model, attempt, deep=False):
        if self.flavor == "gemini":
            self.effort_ok[model, deep] = attempt
