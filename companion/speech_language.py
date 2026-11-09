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
