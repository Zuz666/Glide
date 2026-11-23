#!/usr/bin/env python3
"""The desktop IPC has one channel map (desktop/src/shared/ipc.ts): every channel in it is answered by main/app/ipc.ts
and let through by the preload, and nothing else is; every event the main process sends is in its EventMap and the
preload's event list."""
import glob
import os
import re

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SRC = os.path.join(ROOT, "desktop", "src")
read = lambda *p: open(os.path.join(SRC, *p), encoding="utf-8-sig").read()  # noqa: E731


def interface_keys(src, name):
    body = re.search(r"export interface %s \{\n(.*?)\n\}" % name, src, re.S)
    assert body, "no interface %s" % name
    return set(re.findall(r'^\s+"([\w:]+)":', body.group(1), re.M))


shared = read("shared", "ipc.ts")
invoke, events = interface_keys(shared, "InvokeMap"), interface_keys(shared, "EventMap")
assert len(invoke) >= 38 and len(events) >= 9, (len(invoke), len(events))

preload = read("main", "preload.ts")
allow = lambda name: set(re.findall(r'"([\w:]+)"', re.search(r"const %s = new Set<\w+>\(\[(.*?)\]\)" % name,  # noqa: E731
                                                                 preload, re.S).group(1)))
assert allow("INVOKE") == invoke, ("preload vs InvokeMap", allow("INVOKE") ^ invoke)
assert allow("EVENTS") == events, ("preload vs EventMap", allow("EVENTS") ^ events)

handled = set(re.findall(r'\bhandle\("([\w:]+)"', read("main", "app", "ipc.ts")))
assert handled == invoke, ("ipc.ts handlers vs InvokeMap", handled ^ invoke)

main_files = glob.glob(os.path.join(SRC, "main", "**", "*.ts"), recursive=True)
sent = set()
for f in main_files:
    sent |= set(re.findall(r'\b(?:broadcast|send)\("([\w:]+)"', open(f, encoding="utf-8-sig").read()))
assert sent and sent <= events, ("sent but not in EventMap", sent - events)

print("ok")
