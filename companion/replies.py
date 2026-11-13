"""What the coach does with a model's reply text: the FACTS line split off (machine-only fact ids), WATCH/UNWATCH
directive lines found, SKIP recognised, the length capped at whole sentences, and a streamed reply spoken sentence by
sentence while it arrives (EarlySpeech)."""
import re

import speech_markup


_FACTS_LINE = re.compile(r"^\s*[*_`>-]*\s*facts?\s*[*_`]*\s*:\s*(.*)$", re.I)
_FACT_START = re.compile(r"^(?:q|c|i|p|h|s|g|n|poi|web):", re.I)


def parse_fact_ids(text):
    """'q:1, s:5-11 Dun Morogh/7, poi:flight:2' -> ids (fragments that don't start a fact join the previous one)."""
    t = (text or "").strip().strip("`*").strip()
    if not t or t.lower().rstrip(".") in ("none", "-", "n/a"):
        return []
    out = []
    for part in t.split(","):
        p = part.strip().strip("`*[]()").rstrip(".;").strip()
        if not p:
            continue
        if _FACT_START.match(p) or not out:
            out.append(p)
        else:
            out[-1] += "," + p
    return list(dict.fromkeys(out))


def split_facts(text):
    """Strip "FACTS: ..." lines (also one glued to the end of the last line). -> (text, fact ids, had_line)."""
    keep, ids, had = [], [], False
    for line in (text or "").splitlines():
        m = _FACTS_LINE.match(line)
        if m:
            had = True
            ids += parse_fact_ids(m.group(1))
            continue
        i = line.find("FACTS:")
        if i > 0:
            had = True
            ids += parse_fact_ids(line[i + 6:])
            line = line[:i].rstrip()
        keep.append(line)
    return "\n".join(keep).strip(), list(dict.fromkeys(ids)), had


def trim_words(text, limit):
    """Hard cap on length: keep whole sentences while they fit; always keep the first one, cut at the limit."""
    if len(text.split()) <= limit:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    out, n = [], 0
    for sen in sentences:
        w = len(sen.split())
        if out and n + w > limit:
            break
        out.append(sen)
        n += w
    res = " ".join(out)
    words = res.split()
    if len(words) > limit + 6:                     # one run-on first sentence: cut it
        res = " ".join(words[:limit]).rstrip(",;:") + "."
    return res


def is_skip(text):
    """The model's "nothing worth saying" reply (SKIP, give or take a full stop)."""
    return text.strip().strip(".!").upper() == "SKIP"


def split_watch(text):
    """Pull the WATCH: / UNWATCH: <buff> lines out of a reply -> (the rest of the text, [(buff name, on), ...])."""
    keep, found = [], []
    for line in text.splitlines():
        t = line.strip()
        up = t.upper()
        if up.startswith("WATCH:") or up.startswith("UNWATCH:"):
            name = t.split(":", 1)[1].strip().strip(".")
            if name:
                found.append((name, up.startswith("WATCH:")))
        else:
            keep.append(line)
    return "\n".join(keep).strip(), found


# the lines a reply ends with that are never spoken (the coach strips and applies them)
DIRECTIVES = ("SKIP", "FACTS", "WATCH:", "UNWATCH:", "PERSONA:", "VOICE:", "BANTER:", "CHAT:", "NAME:", "LORE:",
              "TONE:", "SPEECH:", "ROLE:", "SCENE:")


MIDLINE_DIRECTIVE = re.compile(r"(?<=\S)\s+(?:VOICE|NAME|LORE|TONE|SPEECH|ROLE|SCENE|PERSONA|BANTER|CHAT)\s*:\s")


class EarlySpeech:
    """Speaks a streamed reply sentence by sentence while it arrives, instead of after the whole reply (and its
    FACTS line) is done. Stops at SKIP / FACTS / WATCH lines and at the word limit the reply will be trimmed to, so it
    never says what the final text won't contain. `spoken` (text already said, from the start of the reply, after
    `prefix`) goes on the final message; the app then says only the rest. A reply in character starts with a
    "TONE: <delivery>" line: it is read into `tone` (the lines are spoken with it) and never said."""
    _END = re.compile(r"[.!?](?=\s)")

    def __init__(self, speak, limit_words, urgent=False, prefix="", policy=None):
        self.speak, self.limit, self.urgent = speak, limit_words, urgent
        self.buf, self.used, self.words, self.stopped = "", 0, 0, False
        self.spoken = speech_markup.canonical(prefix)
        self.tone = None
        self.policy = policy or speech_markup.Policy()

    def __call__(self, chunk):
        if self.stopped:
            return
        self.buf += chunk
        body, self.tone, hints = speech_markup.split_headers(self.buf, self.policy, complete=False)
        # Only prose contributes to canonical offsets and the commitment ledger. Metadata, including a
        # partial or late header, is never a speech unit.
        lines = body.split("\n")
        cut = next((i for i, line in enumerate(lines) if line.strip().upper().startswith(DIRECTIVES)), None)
        directive = cut is not None
        if directive:
            body = "\n".join(lines[:cut])
        mid = MIDLINE_DIRECTIVE.search(body)
        if mid:
            body, directive = body[:mid.start()], True
        canonical = speech_markup.canonical(body) + (" " if body.endswith((" ", "\n", "\r", "\t")) else "")
        while not self.stopped:
            rest = canonical[self.used:].lstrip()
            start = len(canonical) - len(rest)
            # A punctuation mark needs a following separator (or a directive) before committing. The
            # final unterminated sentence belongs to final delivery, as before.
            m = self._END.search(rest)
            if not m and directive:
                m = re.search(r"[.!?]$", rest)
            if not m:
                if directive:
                    self.stopped = True
                return
            sentence = rest[:m.end()]
            n = len(sentence.split())
            if not sentence or "FACTS:" in sentence.upper() or self.words + n > self.limit:
                self.stopped = True
                return
            end = start + len(sentence)
            markup = speech_markup.resolve(body, hints, self.policy, committed=self.used)
            markup = speech_markup.prepare_with_policy(body, self.policy, markup)
            markup = speech_markup.slice_markup(markup, start, end, sentence)
            accepted = self.speak(sentence, self.urgent and not self.words, markup=markup) if markup else \
                self.speak(sentence, self.urgent and not self.words)
            if accepted is False:
                self.stopped = True       # PTT refused: final delivery still owns these words.
                return
            self.used = end
            self.words += n
            self.spoken = (self.spoken + " " + sentence).strip()

    @property
    def said(self):
        """Whether any of the reply itself was spoken early."""
        return self.words > 0
