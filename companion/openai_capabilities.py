"""First-party capability, suitability, transport, and pricing policy for direct OpenAI endpoints.

Scoped strictly to endpoints whose normalized URL is https://api.openai.com/v1 (Official OpenAI Origin).
Third-party compatible endpoints and OpenRouter gateway metadata remain isolated.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import os
import re
from typing import Literal
import urllib.parse

import openai_compat

OriginScope = Literal["official_openai", "compatible"]
Suitability = Literal["coach", "excluded", "unclassified"]
Transport = Literal["chat", "responses"]
LimitField = Literal["max_tokens", "max_completion_tokens", "max_output_tokens"]

COMPAT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_STANDARD = ("low", "medium", "high")
_EXTENDED = (*_STANDARD, "xhigh")
_EXTENDED_MAX = (*_EXTENDED, "max")
_GPT6 = ("none", *_EXTENDED_MAX)
_SNAPSHOT_RE = re.compile(r"^(.+)-\d{4}-\d{2}-\d{2}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_EXCLUSIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openai_exclusions.json")
with open(_EXCLUSIONS_PATH, encoding="utf-8") as _efh:
    _EXCLUSION_RULES: tuple[dict, ...] = tuple(json.load(_efh)["rules"])


def _price(pin: float, pout: float, cache_read: float | None = None,
           cache_write: float | None = None) -> dict[str, float | None]:
    return {"in": pin, "out": pout, "cache_read": cache_read, "cache_write": cache_write}


# Verified against first-party model cards on developers.openai.com/api/docs/models/*.md (2026-11-11).
# Keys are undated base IDs and documented aliases only; single -YYYY-MM-DD snapshots resolve to these bases.
_OFFICIAL_COACH_MODELS: dict[str, tuple[tuple[str, ...], Transport, int | None, dict[str, float | None] | None]] = {
    "gpt-6-astra": (_EXTENDED_MAX, "responses", 1_050_000, _price(10.0, 50.0, 1.0, 12.5)),
    # Direct API observation rejects max and none on 6.1 Sol despite the public card listing max.
    "gpt-6.1-sol": (_EXTENDED, "responses", 1_050_000, _price(2.0, 10.0, 0.1, 2.5)),
    "gpt-6.1-terra": (_GPT6, "responses", 1_050_000, _price(1.0, 5.0, 0.05, 1.25)),
    "gpt-6-sol": (_GPT6, "responses", 1_050_000, _price(2.0, 10.0, 0.2, 2.5)),
    "gpt-6-luna": (_GPT6, "responses", 1_050_000, _price(0.1, 0.5, 0.01, 0.125)),
    "gpt-5.6-sol": (_GPT6, "responses", 1_050_000, _price(4.0, 20.0, 0.4, 5.0)),
    "gpt-5.6": (_GPT6, "responses", 1_050_000, _price(4.0, 20.0, 0.4, 5.0)),
    "gpt-5.6-terra": (_GPT6, "responses", 1_050_000, _price(2.0, 12.0, 0.2, 2.5)),
    "gpt-5.6-luna": (_GPT6, "responses", 1_050_000, _price(0.2, 1.2, 0.02, 0.25)),
    "gpt-5.5": (("none", *_EXTENDED), "responses", 1_050_000, _price(5.0, 30.0, 0.5)),
    "gpt-5.5-pro": (("medium", "high", "xhigh"), "responses", 1_050_000, _price(30.0, 180.0)),
    "gpt-5.4": (("none", *_EXTENDED), "responses", 1_050_000, _price(2.5, 15.0, 0.25)),
    "gpt-5.4-mini": (("none", *_EXTENDED), "responses", 400_000, _price(0.75, 4.5, 0.075)),
    "gpt-5.4-nano": (("none", *_EXTENDED), "responses", 400_000, _price(0.2, 1.25, 0.02)),
    "gpt-5.4-pro": (("medium", "high", "xhigh"), "responses", 1_050_000, _price(30.0, 180.0)),
    "gpt-5.3-codex": (_EXTENDED, "responses", 400_000, _price(1.75, 14.0, 0.175)),
    "gpt-5.2": (("none", *_EXTENDED), "responses", 400_000, _price(1.75, 14.0, 0.175)),
    "gpt-5.2-pro": (("medium", "high", "xhigh"), "responses", 400_000, _price(21.0, 168.0)),
    "gpt-5.1": (("none", *_STANDARD), "responses", 400_000, _price(1.25, 10.0, 0.125)),
    "gpt-5": (("minimal", *_STANDARD), "responses", 400_000, _price(1.25, 10.0, 0.125)),
    "gpt-5-mini": (("minimal", *_STANDARD), "responses", 400_000, _price(0.25, 2.0, 0.025)),
    "gpt-5-nano": (("minimal", *_STANDARD), "responses", 400_000, _price(0.05, 0.4, 0.005)),
    "gpt-5-pro": (("high",), "responses", 400_000, _price(15.0, 120.0)),
    "gpt-5-codex": ((), "responses", None, None),
    "codex-mini-latest": ((), "responses", None, None),
    "o3": (_STANDARD, "responses", 200_000, _price(2.0, 8.0, 0.5)),
    "o3-mini": (_STANDARD, "responses", 200_000, _price(1.1, 4.4, 0.55)),
    "o4-mini": (_STANDARD, "responses", 200_000, _price(1.1, 4.4, 0.275)),
    "o1-pro": ((), "responses", 200_000, None),
    "o3-pro": ((), "responses", 200_000, _price(20.0, 80.0)),
    "chat-latest": ((), "chat", 400_000, _price(5.0, 30.0, 0.5)),
    "gpt-5-chat-latest": ((), "chat", None, None),
    "gpt-5.1-chat-latest": ((), "chat", None, None),
    "gpt-5.2-chat-latest": ((), "chat", None, None),
    "gpt-4.1": ((), "chat", 1_047_576, _price(2.0, 8.0, 0.5)),
    "gpt-4.1-mini": ((), "chat", 1_047_576, _price(0.4, 1.6, 0.1)),
    "gpt-4.1-nano": ((), "chat", 1_047_576, _price(0.1, 0.4, 0.025)),
    "gpt-4o": ((), "chat", 128_000, _price(2.5, 10.0, 1.25)),
    "gpt-4o-mini": ((), "chat", 128_000, _price(0.15, 0.6, 0.075)),
}


@dataclass(frozen=True)
class ModelContract:
    scope: OriginScope
    model_id: str
    suitability: Suitability
    transport: Transport
    limit_field: LimitField
    known: bool
    efforts: tuple[str, ...]
    note: str
    reason: str | None = None
    context: int | None = None
    price: dict[str, float | None] | None = None


def is_official_openai(endpoint) -> bool:
    """An endpoint's normalized URL, never its user-chosen id, identifies OpenAI's first-party API."""
    raw = endpoint.get("base_url") if isinstance(endpoint, dict) else endpoint
    if not isinstance(raw, str):
        return False
    try:
        url = urllib.parse.urlsplit(raw.strip())
        path = url.path.rstrip("/")
        return (url.scheme.lower() == "https" and (url.hostname or "").lower() == "api.openai.com"
                and url.port in (None, 443) and path == "/v1"
                and not url.username and not url.password and not url.query and not url.fragment)
    except ValueError:
        return False


def _parse_iso_date(raw: str | None) -> date | None:
    if not isinstance(raw, str) or not _ISO_DATE_RE.match(raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def catalog_raw_entry(catalog, model: str | None) -> dict | None:
    """Return unprojected catalog metadata (including shutdown_date on excluded entries) when available."""
    if catalog is None or not model:
        return None
    getter = getattr(catalog, "raw_get", None) or getattr(catalog, "get", None)
    entry = getter(model) if callable(getter) else None
    return entry if isinstance(entry, dict) else None


def excluded_reason(name: str, shutdown_date: str | None = None, today: str | None = None) -> str | None:
    """Return a concise non-coach exclusion reason for an official OpenAI model id, or None."""
    parsed_shutdown = _parse_iso_date(shutdown_date)
    if parsed_shutdown is not None:
        now_date = _parse_iso_date(today) or datetime.now(timezone.utc).date()
        if parsed_shutdown <= now_date:
            return "shut down on %s" % parsed_shutdown.isoformat()
    m = (name or "").strip().lower()
    if not m:
        return "empty model id"
    for rule in _EXCLUSION_RULES:
        if (any(m.startswith(p) for p in rule["prefixes"])
                or any(m.endswith(s) for s in rule["suffixes"])
                or any(c in m for c in rule["contains"])):
            return rule["reason"]
    return None


def describe_endpoint_model(model: str, cfg: dict | None, entry: dict | None = None,
                            today: str | None = None) -> ModelContract:
    """Resolve an ep:<id>:<model> identifier against its configured endpoint origin."""
    eid, name = openai_compat.split_endpoint(model)
    endpoint = openai_compat.endpoints(cfg).get(eid)
    if not is_official_openai(endpoint):
        return ModelContract(
            scope="compatible",
            model_id=name,
            suitability="unclassified",
            transport="chat",
            limit_field="max_tokens",
            known=False,
            efforts=COMPAT_LEVELS,
            note="Supported levels are not advertised. Select only levels documented by this provider; "
                 "auto is safest.",
        )
    shutdown = entry.get("shutdown_date") if isinstance(entry, dict) and entry.get("id", model) == model else None
    reason = excluded_reason(name, shutdown, today=today)
    if reason is not None:
        return ModelContract(
            scope="official_openai",
            model_id=name,
            suitability="excluded",
            transport="chat",
            limit_field="max_completion_tokens",
            known=True,
            efforts=(),
            note="Excluded official OpenAI model (%s)." % reason,
            reason=reason,
        )
    base_name = name
    profile = _OFFICIAL_COACH_MODELS.get(name)
    if profile is None:
        match = _SNAPSHOT_RE.match(name)
        if match is not None:
            base_name = match.group(1)
            profile = _OFFICIAL_COACH_MODELS.get(base_name)
    if profile is not None:
        levels, transport, context, price = profile
        limit_field: LimitField = "max_output_tokens" if transport == "responses" else "max_completion_tokens"
        if not levels and transport == "responses":
            note = "OpenAI Responses model; explicit effort levels are not advertised here, so use auto."
        elif not levels:
            note = "This model does not support reasoning effort controls."
        elif base_name == "gpt-6.1-sol":
            note = "Observed OpenAI Sol levels exclude none and max; tool calls use the Responses API."
        elif transport == "responses":
            note = "Documented OpenAI model effort levels; routed via the stateless Responses API."
        else:
            note = "Documented OpenAI model effort levels; auto leaves the provider default unchanged."
        return ModelContract(
            scope="official_openai",
            model_id=name,
            suitability="coach",
            transport=transport,
            limit_field=limit_field,
            known=True,
            efforts=levels,
            note=note,
            context=context,
            price=dict(price) if price is not None else None,
        )
    return ModelContract(
        scope="official_openai",
        model_id=name,
        suitability="unclassified",
        transport="chat",
        limit_field="max_completion_tokens",
        known=False,
        efforts=COMPAT_LEVELS,
        note="Supported levels are not advertised for this OpenAI model. Select only documented levels; "
             "auto is safest.",
    )
