"""Recall by meaning (optional; llm.recall_meaning): an embedding model turns the memories and the moment into vectors,
so "that time we got wrecked by the big gnoll" finds the Hogger chapter with no shared word. Off by default (recall.py
matches words then). Where the vectors come from:

    "openai"        OpenAI's embeddings API (OPENAI_API_KEY, the key push-to-talk and the OpenAI voice use; model
                    text-embedding-3-small unless llm.recall_embed_model says another)
    "gemini"        Gemini's embeddings API (GEMINI_API_KEY; gemini-embedding-001; the key goes in a header)
    "ep:<id>"       a custom OpenAI-compatible endpoint (Settings > AI models & keys): a local Ollama or LM Studio
                    embeds for free (llm.recall_embed_model names its model, e.g. nomic-embed-text: required)

Two kinds of text: the memories ("documents": embedded in the background by warm(), Banter._warm, kept compactly in
coach_embeddings.json beside the memory and pruned to what memory holds now) and the moment's words ("queries": one
small request when a line in character needs it, kept in a short in-memory list, never on disk). Gemini and nomic models
are told which is which (task types, prefixes). Standard library only (urllib). Never raises: a slow, failing or odd
service gives None and recall goes by words alone; a failing query backs off a minute (a failing warm-up doesn't)."""
import base64
import hashlib
import json
import math
import os
import struct
import threading
import time
import urllib.request
from collections import OrderedDict

import jsonfile
import openai_compat
from log import log_once

QUERY_TIMEOUT_S = 4.0           # a line waits this long for its query vector at most
WARM_TIMEOUT_S = 45.0           # a background batch (a local CPU model loading takes a while)
BACKOFF_S = 60.0                # a failing query: not asked again before this
BATCH = 16                      # documents per warm-up request (saved after each)
QUERIES_KEPT = 200              # query vectors kept in memory (the newest)
DEFAULT_MODEL = {"openai": "text-embedding-3-small", "gemini": "gemini-embedding-001"}
OPENAI_URL = "https://api.openai.com/v1/embeddings"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/%s:batchEmbedContents"   # key: a header


def cosine(a, b):
    """Cosine similarity of two vectors (0 for an empty or zero one, or different lengths)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def pack(v):
    """A vector as base64 float32 (about a quarter of JSON numbers' size)."""
    return base64.b64encode(struct.pack("<%df" % len(v), *v)).decode("ascii")


def unpack(s):
    """The vector of pack(s), or None for something else."""
    try:
        raw = base64.b64decode(s, validate=True)
        if not raw or len(raw) % 4:
            return None
        v = list(struct.unpack("<%df" % (len(raw) // 4), raw))
        return v if all(math.isfinite(x) for x in v) else None
    except (ValueError, TypeError, struct.error):
        return None


def norm(text):
    return " ".join(str(text).split())


class Embedder:
    """source: "openai" | "gemini" | "ep:<endpoint id>"; model: "" for the source's default; cfg: the "llm" config
    (its custom endpoints); cache_path: coach_embeddings.json (None: kept in this process only)."""

    def __init__(self, source, model, cfg, cache_path=None):
        self.source = (source or "").strip().lower()   # "ep:Ollama" and "ep:ollama" are one endpoint, one cache
        self.model = model or DEFAULT_MODEL.get(self.source, "")
        self.cfg = cfg
        self.cache_path = cache_path
        self.lock = threading.Lock()
        self.docs = {}                    # key -> vector of a memory text (kept on disk)
        self.queries = OrderedDict()      # key -> vector of a query (the newest QUERIES_KEPT, in memory only)
        self.down_until = 0.0             # a failing query path waits until then
        self.warm_ok = None               # the last warm-up: True (all embedded), False (failed), None (none yet)
        # Observation is separate from warm_ok/backoff (which retain their existing scheduling semantics).
        self._warm_result = None          # (configuration fingerprint, success, dimensions); real requests only
        self._query_result = None
        self._warming = {}
        self._docs_revision = 0
        self._coverage_texts = None
        self._coverage_revision = -1
        self._coverage_count = 0
        self._coverage_dimension = None
        self._coverage_incompatible = 0
        self._load()

    @classmethod
    def from_settings(cls, opt, cfg, memory_dir=None):
        """The embedder llm.recall_meaning asks for, or None (off, or the source can't be used)."""
        source = (opt.get("recall_meaning") or "off").strip().lower()
        if source in ("", "off"):
            return None
        model = (opt.get("recall_embed_model") or "").strip()
        if source not in DEFAULT_MODEL and not source.startswith("ep:"):
            log_once("embeddings.source", ValueError("unknown llm.recall_meaning %r" % source))
            return None
        if source.startswith("ep:") and not model:
            log_once("embeddings.model", ValueError("llm.recall_meaning %s needs llm.recall_embed_model" % source))
            return None
        path = os.path.join(memory_dir, "coach_embeddings.json") if memory_dir else None
        return cls(source, model, cfg, path)

    def _configuration(self):
        """Local service identity and a safe blocker; never expose credentials or endpoint URLs."""
        reason = ""
        if self.source == "openai":
            url, key_env = OPENAI_URL, "OPENAI_API_KEY"
            key = os.environ.get(key_env, "").strip()
            if not key:
                reason = "The OpenAI embedding API key is missing."
        elif self.source == "gemini":
            url, key_env = GEMINI_URL, "GEMINI_API_KEY"
            key = (os.environ.get(key_env, "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
            if not key:
                reason = "The Gemini embedding API key is missing."
        else:
            ep = openai_compat.endpoints(self.cfg).get(self.source[3:])
            url, key_env = (ep["base_url"], ep["key_env"]) if ep else ("", "")
            key = os.environ.get(key_env, "").strip()
            if ep is None:
                reason = "The custom embedding endpoint is missing or has an invalid URL."
            elif not self.model:
                reason = "An embedding model is required for the custom endpoint."
        identity = hashlib.sha256(json.dumps([self.source, self.model, url, key_env, key]).encode("utf-8")).digest()
        return identity, reason

    def status(self, texts):
        """Observe eligible-memory coverage and independent request outcomes without doing any I/O.

        `texts` is an immutable snapshot from Memory.recall_texts; coverage is counted only when it or docs change.
        Cache coverage is not proof of current service access. Success is historical evidence, not a health probe.
        """
        identity, blocker = self._configuration()
        with self.lock:
            warm_result = self._warm_result if self._warm_result and self._warm_result[0] == identity else None
            query_result = self._query_result if self._query_result and self._query_result[0] == identity else None
            warm = warm_result[1] if warm_result else None
            query = query_result[1] if query_result else None
            dimension = query_result[2] if query_result and query else warm_result[2] if warm_result and warm else None
            if texts is not self._coverage_texts or self._coverage_revision != self._docs_revision \
                    or dimension != self._coverage_dimension:
                self._coverage_texts = texts
                self._coverage_revision = self._docs_revision
                self._coverage_dimension = dimension
                covered = incompatible = 0
                for text in texts:
                    vector = self.docs.get(self._key(text, "doc"))
                    if vector is not None:
                        if dimension is not None and len(vector) != dimension:
                            incompatible += 1
                        else:
                            covered += 1
                self._coverage_count, self._coverage_incompatible = covered, incompatible
            covered, incompatible, total = self._coverage_count, self._coverage_incompatible, len(texts)
            preparing = bool(self._warming.get(identity))
            backoff = time.time() < self.down_until
        failures = []
        if warm is False:
            failures.append("Memory preparation failed")
        if query is False:
            failures.append("Query embedding failed" + ("; query retry backoff is active" if backoff else ""))
        elif backoff:
            failures.append("Query requests are paused by retry backoff from an earlier operation")
        if failures:
            state, detail = "error", ". ".join(failures) + ". Word-based fallback remains available."
            if preparing:
                detail += " Memory preparation is in progress; previous failures remain until their own path recovers."
            if blocker:
                detail += " " + blocker
        elif blocker:
            state, detail = "unavailable", blocker + " Word-based fallback remains available."
        elif preparing:
            state, detail = "preparing", "Memory preparation is in progress. Word-based matching remains available."
        elif not total:
            state, detail = "empty", "No eligible memories to prepare. Empty preparation does not verify service access."
        elif not covered and incompatible:
            state, detail = "unavailable", "Cached memories are incompatible with observed embedding vectors. Word-based fallback remains available."
        elif 0 < covered < total:
            state, detail = "partial", "Only some eligible memories have cached embeddings. Word-based matching remains available."
        elif covered == total and (warm is True or query is True):
            state, detail = "ready", "Eligible memories have cached embeddings; meaning-based recall has observed service evidence."
        elif warm is True or query is True:
            state, detail = "unchecked", "Eligible memories have not been prepared for meaning-based recall."
        else:
            state, detail = "unchecked", "Waiting for the first successful embedding request with these settings."
        cache = "Memory cache ready" if total and covered == total else "Prepared memories"
        detail = "%s: %d/%d. %s" % (cache, covered, total, detail)
        if incompatible:
            detail += " %d cached texts have a different vector dimension from the current service evidence." % incompatible
        if warm is True:
            detail += " Preparation requests succeeded for this configuration."
        if query is True:
            detail += " A query request succeeded for this configuration; no matching memory is a normal result."
        if query is None and warm is True:
            detail += " The query path has not yet been checked."
        if state == "ready":
            detail += " Service availability can change."
        source = self.source if self.source in DEFAULT_MODEL or (
            self.source.startswith("ep:") and openai_compat.ENDPOINT_ID_RE.fullmatch(self.source[3:])) else "unknown"
        return {"state": state, "source": source, "model": self.model, "detail": detail}

    # ---- the documents' cache (on disk) --------------------------------------------------------------------------
    def _key(self, text, role):
        return hashlib.sha1(("%s|%s|%s|%s" % (self.source, self.model, role, text)).encode("utf-8")).hexdigest()

    def _load(self):
        if not self.cache_path or not os.path.exists(self.cache_path):
            return
        try:
            with open(self.cache_path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(d, dict) or d.get("source") != self.source or d.get("model") != self.model:
            return                        # another source or model: its vectors don't compare with this one's
        vecs = d.get("vectors")
        if isinstance(vecs, dict):
            for k, s in vecs.items():
                v = unpack(s) if isinstance(s, str) else None
                if v is not None:
                    self.docs[k] = v

    def _save(self, docs):
        """Write `docs` (a copy, taken under the lock) outside the lock."""
        if not self.cache_path:
            return
        try:
            jsonfile.write_atomic(self.cache_path, {"source": self.source, "model": self.model,
                                                    "vectors": {k: pack(v) for k, v in docs.items()}})
        except OSError as e:
            log_once("embeddings.save", e)

    def clear(self):
        """Forget every vector (the player's "forget everything"), on disk too."""
        with self.lock:
            self.docs.clear()
            self.queries.clear()
            self._docs_revision += 1
        if self.cache_path:
            try:
                os.remove(self.cache_path)
            except OSError:
                pass

    # ---- what a line uses ---------------------------------------------------------------------------------------
    def cached(self, texts):
        """{text: vector} for the memory texts already embedded (nothing is asked for: lines never wait on the
        memory; warm() embeds it in the background)."""
        with self.lock:
            out = {}
            for t in texts:
                t = norm(t)
                v = self.docs.get(self._key(t, "doc"))
                if v is not None:
                    out[t] = v
            return out

    def query(self, text):
        """The moment's vector (one small request on a miss, QUERY_TIMEOUT_S at most), or None (down, failing)."""
        t = norm(text)
        if not t:
            return None
        k = self._key(t, "query")
        with self.lock:
            v = self.queries.get(k)
            if v is not None:
                self.queries.move_to_end(k)
                return v
            if time.time() < self.down_until:
                return None
        identity, _ = self._configuration()
        got = self._request([t], "query", QUERY_TIMEOUT_S)
        if not got:
            with self.lock:
                self.down_until = time.time() + BACKOFF_S
                if self._configuration()[0] == identity:
                    self._query_result = (identity, False, None)
            return None
        with self.lock:
            if self._configuration()[0] == identity:
                self._query_result = (identity, True, len(got[0]))
            self.queries[k] = got[0]
            while len(self.queries) > QUERIES_KEPT:
                self.queries.popitem(last=False)
        return got[0]

    def warm(self, texts):
        """Embed the memory texts not embedded yet (background: Banter._warm), in batches of BATCH, saving after each;
        drop the vectors of texts memory no longer holds. -> True when all are embedded (warm_ok)."""
        texts = list(dict.fromkeys(norm(t) for t in texts if norm(t)))
        keep = {self._key(t, "doc") for t in texts}
        with self.lock:
            pruned = [k for k in self.docs if k not in keep]
            for k in pruned:               # forgotten or changed memories
                del self.docs[k]
            if pruned:
                self._docs_revision += 1
            todo = [t for t in texts if self._key(t, "doc") not in self.docs]
            snapshot = dict(self.docs)
        identity, _ = self._configuration()
        if todo:
            with self.lock:
                self._warming[identity] = self._warming.get(identity, 0) + 1
        ok = True
        requested_identity = None
        dimension = None
        try:
            for i in range(0, len(todo), BATCH):
                chunk = todo[i:i + BATCH]
                requested_identity, _ = self._configuration()
                got = self._request(chunk, "doc", WARM_TIMEOUT_S)
                if not got:
                    ok = False
                    break
                dimension = len(got[0])
                with self.lock:
                    for t, v in zip(chunk, got):
                        self.docs[self._key(t, "doc")] = v
                    self._docs_revision += 1
                    snapshot = dict(self.docs)
                self._save(snapshot)
            if pruned and not todo:
                self._save(snapshot)
            with self.lock:
                self.warm_ok = ok
                if requested_identity is not None and self._configuration()[0] == requested_identity:
                    self._warm_result = (requested_identity, ok, dimension)
            return ok
        finally:
            if todo:
                with self.lock:
                    remaining = self._warming[identity] - 1
                    if remaining:
                        self._warming[identity] = remaining
                    else:
                        del self._warming[identity]

    # ---- the services ---------------------------------------------------------------------------------------------
    def _texts_for(self, texts, role):
        """nomic models want a task prefix; the others take the text as it is."""
        if "nomic" in self.model.lower():
            return [("search_query: " if role == "query" else "search_document: ") + t for t in texts]
        return texts

    def _request(self, texts, role, timeout):
        """One request -> [vector] in order (each a list of finite numbers, all one length), or None."""
        try:
            if self.source == "openai":
                key = os.environ.get("OPENAI_API_KEY", "").strip()
                if not key:
                    raise ValueError("no OPENAI_API_KEY")
                d = self._post(OPENAI_URL, {"model": self.model, "input": texts},
                               {"Authorization": "Bearer " + key}, timeout)
                vecs = [x["embedding"] for x in sorted(d["data"], key=lambda x: x.get("index", 0))]
            elif self.source == "gemini":
                key = (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
                if not key:
                    raise ValueError("no GEMINI_API_KEY")
                task = "RETRIEVAL_QUERY" if role == "query" else "RETRIEVAL_DOCUMENT"
                body = {"requests": [{"model": "models/" + self.model, "taskType": task,
                                      "content": {"parts": [{"text": t}]}} for t in texts]}
                # the key in a header, never in the URL (an error's text or a log line would carry it)
                vecs = [x["values"] for x in self._post(GEMINI_URL % self.model, body, {"x-goog-api-key": key},
                                                        timeout)["embeddings"]]
            else:
                ep = openai_compat.endpoints(self.cfg).get(self.source[3:])
                if ep is None:
                    raise ValueError("no custom endpoint %r" % self.source[3:])
                key = os.environ.get(ep["key_env"], "").strip()
                d = self._post(ep["base_url"].rstrip("/") + "/embeddings",
                               {"model": self.model, "input": self._texts_for(texts, role)},
                               {"Authorization": "Bearer " + key} if key else {}, timeout)
                vecs = [x["embedding"] for x in sorted(d["data"], key=lambda x: x.get("index", 0))]
            out = [[float(x) for x in v] for v in vecs]
            if len(out) != len(texts) or not out[0] or len({len(v) for v in out}) != 1 \
                    or not all(math.isfinite(x) for v in out for x in v):
                raise ValueError("an odd answer: %d vectors for %d texts" % (len(out), len(texts)))
            return out
        except Exception as e:  # noqa: BLE001 - recall by meaning is an extra: recall by words goes on
            log_once("embeddings.%s" % self.source.split(":")[0], e)
            return None

    @staticmethod
    def _post(url, body, headers, timeout):
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers=dict({"Content-Type": "application/json"}, **headers))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
