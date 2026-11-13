"""Bounded speech policy, leading model hints and native delivery data (never XML).

Words are authoritative. Resolving a hint can only attach controls, never remove or manufacture text.
"""
from dataclasses import dataclass
import json
import re
from typing import Literal, NotRequired, TypedDict


FEATURES = frozenset(("rate", "pitch", "boundaries", "pauses"))
MAX_NATIVE_SPANS = 128
PROFILES = {"light": (64, 1, 512), "standard": (128, 3, 1024), "expanded": (256, 6, 2048)}
_HEADER = re.compile(r"^\s*(SPEECH|TONE)\s*:", re.I)
_SENTENCE = re.compile(r"[.!?](?=\s|$)")


class NativeSpan(TypedDict):
    start: int
    end: int
    rate: NotRequired[Literal["slow", "medium", "fast"]]
    pitch: NotRequired[Literal["low", "medium", "high"]]
    after_ms: NotRequired[int]
    boundary: NotRequired[Literal["s", "p"]]


class NativeMarkup(TypedDict):
    text: str
    spans: list[NativeSpan]


def canonical(text):
    return " ".join(str(text or "").split())


@dataclass(frozen=True)
class Policy:
    mode: str = "off"
    features: frozenset[str] = frozenset()
    allowance: int = 0
    max_hints: int = 3
    max_header_bytes: int = 1024

    def instruction(self):
        if not self.allowance:
            return ""
        effects = []
        if "rate" in self.features:
            effects.append('"rate":"slow|medium|fast"')
        if "pitch" in self.features:
            effects.append('"pitch":"low|medium|high"')
        if "pauses" in self.features:
            effects.append('"after_ms":0..1500')
        return ("\nOptional native delivery hints: before ordinary prose (and alongside optional TONE:), emit at most one "
                "SPEECH: line containing a JSON array of [exact quotation, positive one-based occurrence, effects object] "
                "records. At most %d records, %d UTF-8 bytes including SPEECH:, quotations at most 32 Unicode scalars. "
                "Allowed effects: %s; total pauses at most 3000 ms. Quote only the answer body, not directives or an "
                "independently spoken briefing objective. Keep targets within one sentence. Prefer fewer/no hints when "
                "room is tight. The next line is ordinary answer text, never XML or JSON; keep the existing word limit."
                % (self.max_hints, self.max_header_bytes, ", ".join(effects)))


def policy(voice_cfg=None, destination=None):
    cfg, dest = voice_cfg or {}, destination or {}
    mode = cfg.get("markup_mode", "ai")
    if mode not in ("ai", "local", "off"):
        mode = "off"
    features = frozenset(dest.get("features") or ()) & FEATURES if (
        dest.get("provider") == "windows" and dest.get("identity")) else frozenset()
    profile = cfg.get("markup_profile", "standard")
    if not isinstance(profile, str):
        profile = "standard"
    allowance, count, size = PROFILES.get(profile, PROFILES["standard"])
    eligible = mode == "ai" and bool(features & {"rate", "pitch", "pauses"})
    return Policy(mode, features, allowance if eligible else 0, count, size)


def split_headers(text, limits=None, *, complete=True):
    """Strip complete/partial control lines, retaining every separately framed prose line.

    Streaming callers withhold an undecidable leading header prefix. Late/duplicate/broken headers never
    contribute hints. A final unterminated metadata line is not a separately framed answer.
    """
    limits = limits or Policy()
    body, tone, hints = [], "", []
    seen, invalid, started = 0, False, False
    for line in (text or "").splitlines(keepends=True):
        ended = line.endswith(("\n", "\r"))
        stripped = line.strip()
        match = _HEADER.match(line)
        partial = bool(stripped) and any(name.startswith(stripped.upper()) for name in ("SPEECH:", "TONE:"))
        if match or (partial and not ended and not complete):
            if not match:
                continue
            name = match.group(1).upper()
            value = line[match.end():].strip()
            if name == "TONE":
                if not started and ended:
                    tone = value
                continue
            seen += 1
            size = len(line.rstrip("\r\n").encode("utf-8", errors="surrogatepass"))
            if started or seen != 1 or not ended or size > limits.max_header_bytes:
                invalid = True
                continue
            try:
                records = json.loads(value)
            except (ValueError, RecursionError):
                invalid = True
                continue
            if not isinstance(records, list) or len(records) > limits.max_hints:
                invalid = True
            else:
                hints = records
            continue
        inline = re.search(r"(?<=\S)[ \t]+SPEECH\s*:", line, re.I)
        if inline:
            # Rejected inline metadata owns the remainder of its line, never the independently framed
            # words on the next line. Use the same stripped text during streaming and finalization.
            seen += 1
            invalid = True
            ending = "\n" if ended else ""
            line = line[:inline.start()].rstrip() + ending
        if not complete and not ended and not started and not stripped:
            continue
        if stripped:
            started = True
        body.append(line)
    words = "".join(body)
    usable = hints if seen == 1 and not invalid and limits.allowance else []
    return (words.strip() if complete else words.lstrip()), tone, usable


def units(text):
    """Sentence scopes in scalar offsets, never crossing a reliably retained paragraph."""
    out, offset = [], 0
    for paragraph in re.split(r"\n\s*\n", str(text or "")):
        paragraph = canonical(paragraph)
        if not paragraph:
            continue
        start = 0
        for match in _SENTENCE.finditer(paragraph):
            end = match.end()
            if end > start:
                out.append((offset + start, offset + end))
            start = end
            while start < len(paragraph) and paragraph[start].isspace():
                start += 1
        if start < len(paragraph):
            out.append((offset + start, offset + len(paragraph)))
        offset += len(paragraph) + 1
    return out


def _effects(effects, features):
    out = {}
    if not isinstance(effects, dict):
        return out
    if "rate" in features and effects.get("rate") in ("slow", "medium", "fast"):
        out["rate"] = effects["rate"]
    if "pitch" in features and effects.get("pitch") in ("low", "medium", "high"):
        out["pitch"] = effects["pitch"]
    pause = effects.get("after_ms")
    if "pauses" in features and type(pause) is int and 0 <= pause <= 1500:
        out["after_ms"] = pause
    if "boundaries" in features and effects.get("boundary") in ("s", "p"):
        out["boundary"] = effects["boundary"]
    return out


def resolve(text, hints, limits, *, clipped=None, committed=0):
    """Bind exact occurrence against the uncut reply, then discard cross-unit/clipped/committed targets."""
    full = canonical(text)
    final = canonical(clipped) if clipped is not None else full
    if limits.mode != "ai" or not limits.allowance or len(hints) > limits.max_hints:
        return None
    scopes, candidates = units(text), []
    for record in hints:
        if not isinstance(record, list) or len(record) != 3:
            continue
        quote, occurrence, effects = record
        if (not isinstance(quote, str) or not quote or len(quote) > 32
                or any(0xD800 <= ord(c) <= 0xDFFF for c in quote)
                or type(occurrence) is not int or occurrence <= 0 or not isinstance(effects, dict)):
            continue
        # Iteration is bounded by text size, not by a model-supplied occurrence number.
        start, found = -1, 0
        while found < occurrence:
            start = full.find(quote, start + 1)
            if start < 0:
                break
            found += 1
        end = start + len(quote)
        if start < 0 or end > len(final) or full[:end] != final[:end]:
            continue
        if not any(a <= start < end <= b for a, b in scopes):
            continue
        controls = _effects(effects, limits.features - {"boundaries"})
        if controls:
            candidates.append({"start": start, "end": end, **controls})
    data = _data(final, candidates)
    if data and committed:
        # Account for already committed pauses before discarding their targets. Otherwise each streamed
        # sentence would receive a fresh per-reply pause allowance.
        data["spans"] = [span for span in data["spans"] if span["start"] >= committed]
        if not data["spans"]:
            return None
    return data


def _data(text, spans) -> NativeMarkup | None:
    # Conflicting proposals do not acquire precedence from model array ordering.
    clean = [s.copy() for i, s in enumerate(spans) if not any(
        i != j and s["start"] < other["end"] and other["start"] < s["end"]
        for j, other in enumerate(spans))]
    clean.sort(key=lambda span: span["start"])
    pause_total = 0
    for span in clean:
        pause = span.get("after_ms", 0)
        if pause_total + pause > 3000:
            span.pop("after_ms", None)
        else:
            pause_total += pause
    clean = [span for span in clean if len(span) > 2]
    if len(clean) > MAX_NATIVE_SPANS:
        controlled = [span for span in clean if any(key in span for key in ("rate", "pitch", "after_ms"))]
        structural = [span for span in clean if not any(key in span for key in ("rate", "pitch", "after_ms"))]
        clean = sorted((controlled + structural)[:MAX_NATIVE_SPANS], key=lambda span: span["start"])
    return {"text": text, "spans": clean} if clean else None


def slice_markup(markup: NativeMarkup | None, start: int, end: int, text: str | None = None) -> NativeMarkup | None:
    """Keep only wholly contained spans; never move a clipped target onto other words."""
    if not markup:
        return None
    source = markup["text"]
    selected = source[start:end] if text is None else text
    if source[start:end] != selected:
        return None
    spans: list[NativeSpan] = []
    for span in markup["spans"]:
        if start <= span["start"] < span["end"] <= end:
            shifted = span.copy()
            shifted["start"] -= start
            shifted["end"] -= start
            spans.append(shifted)
    result: NativeMarkup = {"text": selected, "spans": spans}
    return result if spans else None


def prepare(text, destination, markup=None, *, mode="ai", context=None):
    """Prepare native controls at enqueue using current destination permission and reliable typed context.

    Existing data refers to exactly these canonical words. Unknown routes and ordinary arbitrary prose are
    neutral; sentence/paragraph structure may add boundaries without inferring emotion.
    """
    return prepare_with_policy(text, policy({"markup_mode": mode}, destination), markup, context=context)


def prepare_with_policy(text, limits, markup=None, *, context=None):
    """The same preparation for an authored producer holding a request snapshot."""
    source = str(text or "")
    text = canonical(source)
    if limits.mode == "off" or not limits.features or not text:
        return None
    spans, boundaries = [], {}
    if isinstance(markup, dict) and markup.get("text") == text:
        for span in markup.get("spans") or ():
            if not isinstance(span, dict):
                continue
            start, end = span.get("start"), span.get("end")
            if type(start) is int and type(end) is int and 0 <= start < end <= len(text):
                effects = _effects(span, limits.features)
                boundary = effects.pop("boundary", None)
                if boundary:
                    if boundaries.get(end) != "p":
                        boundaries[end] = boundary
                if effects:
                    spans.append({"start": start, "end": end, **effects})
    scopes = units(source)
    # These contexts are authored by deterministic producers, never derived from urgent/tone/prose.
    if context and context.get("kind") in ("low_health_in_combat", "instruction"):
        target = context.get("text")
        start = text.find(target) if isinstance(target, str) and target else -1
        end = start + len(target) if start >= 0 else -1
        if start >= 0 and any(a <= start < end <= b for a, b in scopes):
            effects = _effects({"rate": "fast" if context["kind"] == "low_health_in_combat" else "slow"}, limits.features)
            if effects:
                remaining = [(start, end)]
                for span in spans:
                    remaining = [(a, b) for left, right in remaining
                                 for a, b in ((left, min(right, span["start"])),
                                              (max(left, span["end"]), right)) if a < b]
                spans.extend({"start": a, "end": b, **effects} for a, b in remaining)
    if "boundaries" in limits.features:
        paragraph_ends, length = set(), 0
        for paragraph in re.split(r"\n\s*\n", source):
            paragraph = canonical(paragraph)
            if paragraph:
                length += len(paragraph)
                paragraph_ends.add(length)
                length += 1
        for _start, end in scopes:
            boundary = "p" if end in paragraph_ends and re.search(r"\n\s*\n", source) else "s"
            if boundaries.get(end) != "p":
                boundaries[end] = boundary
    # Boundaries mark an endpoint, not an occupied prosody range. Merge them after local/AI controls,
    # splitting an affected range only when necessary; a following pause stays at its original endpoint.
    for end, boundary in sorted(boundaries.items()):
        containing = next((span for span in spans if span["start"] < end <= span["end"]), None)
        if containing:
            if end < containing["end"]:
                suffix = {**containing, "start": end}
                spans.append(suffix)
                containing["end"] = end
                containing.pop("after_ms", None)
            containing["boundary"] = boundary
        else:
            start = next((a for a, b in scopes if a < end <= b), 0)
            start = max([start, *(span["end"] for span in spans if start < span["end"] < end)])
            if start < end:
                spans.append({"start": start, "end": end, "boundary": boundary})
    return _data(text, spans)
