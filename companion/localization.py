"""Coach text localization through the configured, metered model client.

LocalizedText explicitly marks native model output and already prepared messages. It is still an ordinary
JSON string; display and speech consume the same value, without guessing from model names or translating twice.
"""
import queue
import threading

import msg_kinds
from log import log_once
from speech_language import language_code

# Canonical English resources; localized text is supplied by the configured LLM.
TEXT_RESOURCES = {"voice.ready": "Glide Voice is running!"}


class LocalizedText(str):
    language: str

    def __new__(cls, text, language="en"):
        value = super().__new__(cls, text)
        value.language = language
        return value


class Localizer:
    def __init__(self, llm, language=None):
        self.llm = llm
        self.language = language_code(language)
        self._active = threading.local()
        self._pending = queue.Queue()
        self._worker = None
        self._worker_lock = threading.Lock()

    def generated(self, text):
        """Native model output: never send it through a second translation request."""
        return LocalizedText(text, self.language)

    def localize(self, text):
        """English text -> {text, language}; unavailable/failed translation returns the English original."""
        if isinstance(text, LocalizedText):
            return {"text": text, "language": text.language}
        if not text or self.language == "en" or getattr(self._active, "value", False):
            return {"text": LocalizedText(text, "en"), "language": "en"}
        self._active.value = True
        try:
            translated = self.llm.translate(text, self.language)
        except Exception as e:  # noqa: BLE001 - a localization failure never drops the player's message
            log_once("coach.localize", e)
            translated = None
        finally:
            self._active.value = False
        language = self.language if translated else "en"
        return {"text": LocalizedText(translated or text, language), "language": language}

    def needs_localization(self, message):
        return bool(message.get("text") and not isinstance(message["text"], LocalizedText)
                    and message.get("kind") in msg_kinds.COACH_SAID | {"info", "error", "status", "model"})

    def message(self, message):
        """Prepare deterministic text before either display or speech; leave user input and metadata alone."""
        if self.needs_localization(message):
            original = message["text"]
            result = self.localize(original)
            message["text"] = result["text"]
            if original != result["text"]:
                # Translation gives reliable words, not subspan correspondence. Never bind stale source
                # offsets or guess from a coincidentally surviving quotation in the translated sentence.
                message.pop("markup", None)
                message.pop("speech_context", None)
                message.setdefault("original_text", original)  # diagnostics remain available verbatim
        return message

    def deliver(self, message, emit):
        """Queue localization so capture, hotkey and settings callers never wait for provider requests."""
        if (self.language == "en" or not self.needs_localization(message)
                or getattr(self._active, "value", False)):
            emit(self.message(message))
            return
        with self._worker_lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, daemon=True, name="coach-localization")
                self._worker.start()
        self._pending.put((message.copy(), emit))

    def _run(self):
        while True:
            message, emit = self._pending.get()
            try:
                emit(self.message(message))
            except Exception as e:  # noqa: BLE001 - keep later messages deliverable
                log_once("coach.localize.delivery", e)
            finally:
                self._pending.task_done()
