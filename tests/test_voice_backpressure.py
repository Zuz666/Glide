#!/usr/bin/env python3
"""A full command pipe must not prevent draining the worker's full response pipe.

A controlled peer uses real bounded OS pipes in both directions. Run the scenario in a subprocess so a
regression cannot strand blocked writer threads or pipe handles in the suite's test process.
"""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if len(sys.argv) == 1:
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run([sys.executable, __file__, tmp], capture_output=True, text=True,
                                encoding="utf-8", timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
    print("OK")
    sys.exit(0)

sys.path.insert(0, os.path.join(ROOT, "companion"))
import voice  # noqa: E402

large_write = threading.Event()
writer_done = threading.Event()
received = queue.Queue()
errors = []


class CommandPipe:
    def __init__(self, fd):
        self.stream = os.fdopen(fd, "w", encoding="utf-8", buffering=1)

    def write(self, text):
        if len(text) > 1024 * 1024:
            large_write.set()
        return self.stream.write(text)

    def flush(self):
        self.stream.flush()


class Peer:
    def __init__(self):
        command_read, command_write = os.pipe()
        response_read, response_write = os.pipe()
        self.stdin = CommandPipe(command_write)
        self.stdout = os.fdopen(response_read, "r", encoding="utf-8")
        self.commands = os.fdopen(command_read, "r", encoding="utf-8")
        self.responses = os.fdopen(response_write, "w", encoding="utf-8", buffering=1)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def poll(self):
        return None if self.thread.is_alive() else 0

    def run(self):
        try:
            with self.commands, self.responses:
                first = json.loads(self.commands.readline())
                assert large_write.wait(5), "the large command was never sent"
                self.responses.write(json.dumps({"event": "done", "id": first["id"], "voice": "First voice"}) + "\n")
                # More than the OS pipe capacity: a reader waiting on the command-write lock deadlocks here.
                terminal = json.dumps({"event": "error", "id": -1, "error": "x" * 4096}) + "\n"
                for _ in range(512):
                    self.responses.write(terminal)
                second = json.loads(self.commands.readline())
                self.responses.write(json.dumps({"event": "done", "id": second["id"], "voice": "Second voice"}) + "\n")
        except Exception as error:
            errors.append(str(error))


peer = Peer()
with patch.object(voice.sys, "platform", "win32"), patch.object(voice.subprocess, "Popen", return_value=peer):
    v = voice.Voice({"enabled": True, "announce": False}, log_dir=sys.argv[1], on_spoken=received.put)
    v.say("First turn")
    long_text = "Long turn: " + "z" * (2 * 1024 * 1024)

    def write_long():
        try:
            v.say(long_text)
        finally:
            writer_done.set()

    writer = threading.Thread(target=write_long, daemon=True)
    writer.start()
    if not writer_done.wait(5):
        print("FAIL: command write and response reader deadlocked under pipe backpressure", flush=True)
        os._exit(1)
    try:
        first, second = received.get(timeout=5), received.get(timeout=5)
        assert (first["voice"], first["text"]) == ("First voice", "First turn"), first
        assert second["voice"] == "Second voice" and second["text"] == long_text
        peer.thread.join(2)
        assert not peer.thread.is_alive() and not errors, errors
    except Exception as error:
        print("FAIL: " + str(error), flush=True)
        os._exit(1)
    peer.stdin.stream.close()
print("OK: both bounded pipes drained and both completions delivered", flush=True)
