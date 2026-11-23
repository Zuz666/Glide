"""Exact current-source-step matching for one immutable knowledge-pack snapshot.

Only the source ordinal and canonical block digest authorize authored text. Guide
metadata is diagnostic, and native visible order, includes and settings are not
reconstructed here. Blocks are hashed once; only matched blocks are parsed.
"""
import hashlib
import re

from knowledge.rxp_parser import cond_applies, parse_cond, parse_guide, plain

_PREFIX = "Glide:RXP:step:1\n"
_FINGERPRINT = re.compile(r"s1:[0-9a-f]{32}\Z")
_LINES = re.compile(r"\r\n|\r|\n")
_COND = re.compile(r"[ \t\v\f]*<<[ \t\v\f]*(.+)")
_ASSIGN = re.compile(r"^#[^ \t\v\f]+[ \t\v\f]+=")
_STATIC_OTHER = frozenset(("skip", "FOREVER", "CLASSIC", "TBC", "WOTLK", "CATA", "DF", "RETAIL"))


def _blocks(text):
    """Yield canonical blocks, counting every native `step` prefix as an opener."""
    block = []
    for raw in _LINES.split(text):
        code = raw.split("--", 1)[0].strip(" \t\v\f")
        if not code:
            continue
        code = re.sub(r"[ \t]+", " ", code)
        if code.startswith("step"):
            if block:
                yield "\n".join(block) + "\n"
            block = [code]
        elif block:
            block.append(code)
    if block:
        yield "\n".join(block) + "\n"


def _supported(cond, player):
    if not cond:
        return True
    for field in ("class", "not_class", "race", "not_race"):
        if cond.get(field) and not player["cls" if "class" in field else "race"]:
            return False
    # Flat conditions lose mixed-faction ORs; raw literals retain required data.
    literals = re.findall(r"!?[A-Za-z0-9]+", cond["raw"])
    if any(lit.lstrip("!") in ("Alliance", "Horde") for lit in literals) and not player["faction"]:
        return False
    return all(lit.lstrip("!") in _STATIC_OTHER for lit in cond.get("other", ()))


def _hint_forms(values):
    """Catalogue metadata and escaped/truncated G-page forms, never identity."""
    forms = set()
    for value in values:
        if isinstance(value, str) and value:
            forms.add(value)
            wire = re.sub(r"[;=|\n\r]", ",", value).encode("utf-8")[:40]
            forms.add(wire.decode("utf-8", "replace"))
    return forms


class _Unknown(Exception):
    pass


class StepIdentityIndex:
    """Resolve an original source ordinal plus s1 block fingerprint, never `step`."""

    def __init__(self, rows):
        self._blocks = {}
        self._fingerprints = set()
        self._records = []
        # Identical aliases share hashing and parsing without selecting a guide revision.
        canonical_blocks = {}
        for row in rows:
            text = row.get("text")
            available = isinstance(text, str) and bool(text.strip())
            record = {
                "names": _hint_forms((row.get("name"),)),
                "groups": _hint_forms((row.get("grp"),)),
                "available": available,
                "blocks": False,
            }
            if available:
                for ordinal, canonical in enumerate(_blocks(text), 1):
                    record["blocks"] = True
                    if canonical not in canonical_blocks:
                        fingerprint = "s1:" + hashlib.md5(
                            (_PREFIX + canonical).encode("utf-8"), usedforsecurity=False
                        ).hexdigest()
                        canonical_blocks[canonical] = {
                            "fingerprint": fingerprint, "canonical": canonical,
                            "parsed": None, "error": None,
                        }
                    block = canonical_blocks[canonical]
                    self._blocks[(ordinal, block["fingerprint"])] = block
                    self._fingerprints.add(block["fingerprint"])
            self._records.append(record)

    @staticmethod
    def _parse(block):
        if block["error"]:
            raise _Unknown(block["error"])
        if block["parsed"] is None:
            try:
                canonical = block["canonical"]
                lines = canonical.split("\n")[:-1]
                for line in lines:
                    if _ASSIGN.match(line) or line.startswith("<<"):
                        raise _Unknown("executable tag or standalone step condition")
                step = parse_guide(canonical)["steps"][0]
                if any(action["args"].get("unknown_verb")
                       or action["verb"] in ("singleboxtext", "multiboxtext") for action in step["actions"]):
                    raise _Unknown("unsupported action function")
                conditions = []
                for line in lines:
                    match = _COND.search(line)
                    conditions.append(parse_cond(match.group(1)) if match else None)
                block["parsed"] = (step, conditions)
            except (ValueError, TypeError, KeyError, IndexError, _Unknown) as exc:
                block["error"] = str(exc) or "unsupported local source block"
                raise _Unknown(block["error"]) from exc
        return block["parsed"]

    def resolve(self, summary, player):
        """Return a fresh verified `{step: {text, notes}}`, or fail closed.

        `step_source_index` must be a positive integer and `step_fingerprint` an
        s1 lowercase MD5. Guide/group hints affect diagnostics only. Player fields
        are class/race/faction, normalized by the existing parser's evaluator.
        """
        fingerprint = summary.get("step_fingerprint")
        ordinal = summary.get("step_source_index")
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            return {"status": "unverified", "reason": "missing or unsupported step fingerprint"}
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
            return {"status": "unverified", "reason": "missing or invalid source step number"}
        block = self._blocks.get((ordinal, fingerprint))
        if block is None:
            name, group = summary.get("guide"), summary.get("guide_group")
            hints = [record for record in self._records
                     if name and name in record["names"] and (not group or group in record["groups"])]
            if any(not record["available"] for record in hints):
                return {"status": "unverified", "reason": "local source unavailable"}
            mismatch = fingerprint in self._fingerprints or any(record["blocks"] for record in hints)
            return {"status": "mismatch" if mismatch else "missing",
                    "reason": "no matching local source step"}
        who = player or {}
        who = {"cls": who.get("class"), "race": who.get("race"), "faction": who.get("faction")}
        try:
            step, conditions = self._parse(block)
            if any(not _supported(cond, who) for cond in conditions):
                raise _Unknown("unsupported condition or required player data unavailable")
            if not cond_applies(conditions[0], **who):
                raise _Unknown("source step condition does not apply")
            main, notes = [], []
            for action in step["actions"]:
                if not cond_applies(action["cond"], **who):
                    continue
                args = action.get("args") or {}
                text = plain(args.get("text_raw", action.get("text"))) or ""
                if not text or action["verb"] == "macro" or (args.get("kind") == "+" and text.isdigit()):
                    continue
                (notes if args.get("kind") == "*" else main).append(text)
            return {"status": "verified", "step": {"text": "\n".join(main), "notes": notes}}
        except _Unknown as exc:
            return {"status": "unverified", "reason": str(exc)}
