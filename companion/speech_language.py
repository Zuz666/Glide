"""Language selection shared by coaching, speech output, and recognition."""
import re


_LANGUAGE_TAG = re.compile(r"[a-z]{2,3}(?:[-_][a-z0-9]{1,8})*", re.ASCII)


def language_code(value):
    """Return a lowercase base language code, or English for an absent/invalid tag.

    Provider-specific aliases belong to their adapters, not the saved setting.
    Unknown but well-formed codes are preserved so providers can reject unsupported
    languages explicitly instead of silently changing the user's selection.
    """
    if not isinstance(value, str):
        return "en"
    tag = value.strip().lower()
    if not _LANGUAGE_TAG.fullmatch(tag):
        return "en"
    return re.split("[-_]", tag, maxsplit=1)[0]


def prose_spelling(language):
    """Shared spelling policy for translated messages and native model answers."""
    if language_code(language) == "ru":
        return (
            "Russian prose is also spoken by monolingual TTS voices: write its words in Cyrillic, "
            "including quoted quest, character and product names. Use established Russian names where known; "
            "otherwise render foreign names and English words phonetically in Cyrillic, without appending "
            "Latin originals. Spell the product name Glide Voice as Глайд войс. "
            "This spelling rule applies only to human-readable prose, not verbatim user input, identifiers, "
            "URLs, file paths, code, raw diagnostics or machine directive payloads.")
    return "Preserve proper names and model names verbatim."
