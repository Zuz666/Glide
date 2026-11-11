"""What the coach uses from the app, handed over when it is built (Coach(..., deps=CoachDeps(...))), so nothing is
missing when its thread starts. Every field may stay None: tests and evals run the coach without an app."""
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class CoachDeps:
    catalog: Any = None                       # models.Catalog: model prices and tool support
    usage_fn: Optional[Callable] = None       # callback(dict) per API call: tokens read/written/uncached, cost
    on_toggle: Optional[Callable] = None      # callback(key, bool): persist a feature switched by voice
    reminders: Any = None                     # reminders.Reminders: "remind me to repair ..."
    memory: Any = None                        # memory.Memory: what the coach learned about the player
    on_watch: Optional[Callable] = None       # callback(name, on) for WATCH/UNWATCH directives
    gear_fn: Optional[Callable] = None        # -> (upgrades, rewards, worn items, bag items)
    system_fn: Optional[Callable] = None      # -> dict of system status (addon version...)
    speak_fn: Optional[Callable] = None       # callback(text, kind, urgent): say a sentence now (streamed replies)
    watched_fn: Optional[Callable] = None     # -> set of watched buff names
    on_thinking: Optional[Callable] = None    # callback(role | None): active LLM turn ("question"|"callout"|"review")
    language: str = "en"                      # canonical companion.voice.language, fixed for this coach session
