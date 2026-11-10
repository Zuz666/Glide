#!/usr/bin/env python3
"""Manifest values must have a recognized kind, a matching default type, and valid numeric ranges."""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "companion"))
import settings  # noqa: E402
M = settings.ENTRIES

for key, e in M.items():
    assert e["kind"] in ("int", "number", "bool", "text", "list"), key
    if "min" in e or "max" in e:
        assert e["kind"] in ("int", "number") and e["min"] <= e["default"] <= e["max"], key
    assert isinstance(e["default"], {"int": int, "number": (int, float), "bool": bool, "text": str,
                                     "list": list}[e["kind"]]), key

print("ok")
