"""The strip's wire format, written down once: every core key, page and ring event the addon sends.

This registry is the definition of interface #1 in docs/REFACTOR-CONTRACTS.md. docs/STRIP.md is generated from it:
    python companion/strip_spec.py --write      # regenerate docs/STRIP.md
    python companion/strip_spec.py --check      # exit 1 when docs/STRIP.md is out of date
tests/test_strip_spec.py runs the real addon and reads the engine's sources to check that everything sent or read
is registered here, and that docs/STRIP.md is current.

The keys are also a disk format (contract #5): the engine logs raw core and page dicts to data/live/*.jsonl and
knowledge/sources/live_logs.py reads them back. So a key keeps its meaning once sent; new keys are fine.

`since` is the addon version that first sent it: "≤0.7" means it was already in GlideLink 0.7.0 (the oldest copy
kept), "≤0.10" that it came between 0.7.0 and the first Glide release in this repository (0.10.0).
"""
import os
import sys
from collections import namedtuple

Key = namedtuple("Key", "name type meaning module since")
Page = namedtuple("Page", "id indexed meaning module since keys entry")
Event = namedtuple("Event", "kind arg meaning module since")
Flag = namedtuple("Flag", "name bit meaning")

FRAME = """\
The addon draws a grid of cells at the top-left corner of the game window (Strip.lua), in physical pixels:
`cols` x `2 * rows` cells of `cell` px (GlideLinkDB, defaults 96 x 8 and 3 px), then three gauges.

- **Markers.** Cell 0 is magenta (255, 0, 255). Cell 1 is yellow (255, 255, 0) for the binary strip (addon
  0.14.1 and later), cyan (0, 255, 255) for the old 6-bit strip. Each frame's marker cells also set the on/off
  threshold per colour channel, so the read follows HDR or other processing as it changes.
- **Cells.** Every 6-bit symbol is drawn as two cells, three bits each (red, green, blue; fully off or fully on),
  high bits first. (The old 6-bit strip drew one cell per symbol, two bits per channel at levels 0/85/170/255;
  the engine still reads it, Windows Auto HDR breaks it.)
- **Frame.** Bytes `[version][seq][len hi][len lo][payload ...][fletcher16 hi][fletcher16 lo]`, zero-padded to a
  multiple of 3; every 3 bytes are four symbols. `version` is PROTO = 1; `seq` counts frames mod 256; the
  Fletcher-16 sum covers the bytes before it. Capacity: `(cols * rows - 2) // 4 * 3` bytes, 6 of them framing
  (567 payload bytes at the defaults). Codec.lua encodes, companion/protocol.py decodes; they are twins.
- **Payload.** UTF-8 text `core|page`: the core (every frame), a `|`, then one page (or nothing). Both are
  `key=value` pairs joined by `;`. Values never hold `;`, `=`, `|` or line breaks (the addon replaces them).
  A page starts with `pg=<id>;` and is at most 124 bytes with it; a page that doesn't fit the room the core leaves
  waits for a later frame instead of being cut. Pages that changed go first, then the rest in turn.
- **Lists** spread over numbered pages (`Q0`, `Q1`, ...; at most 20). Entries are joined by `,`, fields inside an
  entry by `:`. The engine takes a list only once it has all its chunks and their joined text matches the list's
  hash (protocol.list_hash, State.lua ns.Hash: h = (h * 31 + byte) mod 65521 over the UTF-8 bytes). Quest, buff
  and gear lists carry their page count and hash in the core (`qn`/`qh`, ...); the other lists carry them on every
  page (`n`, `h`).
- **Ring events.** The core's `e1`..`e3` repeat the last three events, newest first, as `id:kind:arg` (arg cleaned,
  at most 40 bytes), so a missed frame loses nothing. Ids count up from 1 each UI load; the engine takes ids above
  the highest it has seen and treats a ring all below it as a restart (UI reload, relog).
- **Gauges.** Under the grid, three 200 x 3 px StatusBars: player health (green), player power (blue), target
  health (red). Forever hides these numbers from addons (secret values), but a StatusBar still shows them, so the
  engine measures the fill (core `gk` says which bars hold a real value).
- **Versions.** The engine wants the addon version it ships with (core `gv`, from the TOC); any other version asks
  the player to update the addon and restart WoW (companion/versions.py).
"""

CORE = (
    Key("gv", "str", "Addon version (the TOC's ## Version)", "Core.lua", "≤0.7"),
    Key("gk", "bitmask", "Gauges that hold a real value: 1 player health, 2 player power, 4 target health",
        "Strip.lua", "0.6"),
    Key("cf", "bitmask", "Coach switches (see Coach flags); 255 = all on", "Coach.lua", "≤0.7"),
    Key("t", "int", "Server time in seconds, mod 1000000 (the clock buff expiries use)", "State.lua", "≤0.7"),
    Key("l", "int", "Player level", "State.lua", "≤0.7"),
    Key("x", "int", "XP into the level", "State.lua", "≤0.7"),
    Key("X", "int", "XP the level needs", "State.lua", "≤0.7"),
    Key("r", "int", "Rested XP (0 = none)", "State.lua", "≤0.7"),
    Key("h", "int", "Player health % (-1 = hidden: read the gauge)", "State.lua", "≤0.7"),
    Key("m", "int", "Player power % (-1 = hidden: read the gauge)", "State.lua", "≤0.7"),
    Key("pt", "str", "Power type token (MANA, RAGE, ENERGY, ...)", "State.lua", "≤0.7"),
    Key("cl", "str", "Class token (WARRIOR, ...)", "State.lua", "≤0.7"),
    Key("c", "flag", "In combat (1/0)", "State.lua", "≤0.7"),
    Key("d", "int", "0 alive, 1 dead, 2 ghost", "State.lua", "≤0.7"),
    Key("mp", "int", "uiMapID of the player's map (empty when unknown)", "State.lua", "≤0.7"),
    Key("px", "int", "Player x on that map x 10000 (only with mp)", "State.lua", "≤0.7"),
    Key("py", "int", "Player y on that map x 10000 (only with mp)", "State.lua", "≤0.7"),
    Key("f", "int", "Facing in radians x 100", "State.lua", "≤0.7"),
    Key("sp", "int", "Speed in yards per second x 10", "State.lua", "≤0.7"),
    Key("g", "int", "Money in copper", "State.lua", "≤0.7"),
    Key("b", "int", "Free bag slots", "State.lua", "≤0.7"),
    Key("du", "int", "Lowest durability % of the equipped items", "State.lua", "≤0.7"),
    Key("hs", "int", "Seconds left on the hearthstone cooldown", "State.lua", "≤0.7"),
    Key("rs", "flag", "Resting (1/0)", "State.lua", "≤0.7"),
    Key("mo", "flag", "Mounted (1/0)", "State.lua", "≤0.7"),
    Key("tx", "flag", "On a taxi (1/0)", "State.lua", "≤0.7"),
    Key("kc", "int", "Kills that gave XP since the UI loaded", "State.lua", "≤0.7"),
    Key("kx", "int", "XP of the last such kill", "State.lua", "≤0.7"),
    Key("tg", "str", "Target (only with one): creature or vehicle npc id, P for a player, empty otherwise",
        "State.lua", "≤0.7"),
    Key("tl", "int", "Target level (only with a target)", "State.lua", "≤0.7"),
    Key("th", "int", "Target health % (-1 = hidden: read the gauge; only with a target)", "State.lua", "≤0.7"),
    Key("tr", "int", "Target reaction to the player, 1 hated .. 8 exalted (only with a target)", "State.lua", "≤0.7"),
    Key("td", "flag", "Target dead (1/0; only with a target)", "State.lua", "≤0.7"),
    Key("st", "int", "RestedXP's current step index (empty: no guide)", "State.lua", "≤0.7"),
    Key("si", "int", "Current step's original ordinal in its own source guide, before filtering/includes; "
        "empty when unverified. Not the visible st number.", "StepIdentity.lua", "0.17.6"),
    Key("sh", "str", "Current source-block fingerprint: s1:<32 lowercase MD5 hex digits>; empty when unverified. "
        "Coherent with si/st in this frame; verifies only that source step, not the whole guide.",
        "StepIdentity.lua", "0.17.6"),
    Key("qn", "int", "Quest log pages Q0.. (count)", "State.lua", "≤0.7"),
    Key("qh", "int", "Quest log list hash", "State.lua", "≤0.7"),
    Key("an", "int", "Buff pages A0.. (count)", "State.lua", "≤0.7"),
    Key("ah", "int", "Buff list hash", "State.lua", "≤0.7"),
    Key("ak", "flag", "Buff list stale: aura data hidden in combat or unreadable, the last good list stays (1/0)",
        "State.lua", "≤0.7"),
    Key("sw", "bitmask", "Weapon item subclasses trained, bit n = subclass n (empty = unknown)", "Gear.lua", "≤0.10"),
    Key("sa", "bitmask", "Armor item subclasses trained, bit n = subclass n (empty = unknown)", "Gear.lua", "≤0.10"),
    Key("wn", "int", "Worn gear pages W0.. (count; only once gear was read)", "Gear.lua", "≤0.7"),
    Key("wh", "int", "Worn gear list hash", "Gear.lua", "≤0.7"),
    Key("in", "int", "Bag gear pages I0.. (count)", "Gear.lua", "≤0.7"),
    Key("ih", "int", "Bag gear list hash", "Gear.lua", "≤0.7"),
    Key("rn", "int", "Quest reward choice pages R0.. (count; 0 = no choice on screen)", "Gear.lua", "≤0.7"),
    Key("rh", "int", "Quest reward choice list hash", "Gear.lua", "≤0.7"),
    Key("rq", "int", "Quest id of the reward choices on screen (0 = none)", "Gear.lua", "≤0.7"),
    Key("tp", "int", "Unspent talent points (empty = unknown)", "Extras.lua", "0.10"),
    Key("aw", "str", "map:x:y of the waypoint RestedXP's arrow points at (x, y in %, one decimal)", "Extras.lua",
        "0.13.1"),
    Key("wz", "int", "Height (world z) in half yards", "Extras.lua", "0.14"),
    Key("fy", "str", "Flight destination of the current step's .fly, while still to do", "Extras.lua", "0.14"),
    Key("fd", "str", "Where the flight the player is on goes (the taxi node clicked)", "Extras.lua", "0.14"),
    Key("tt", "flag", "Target tapped by someone else (1/0, empty = unknown; only with a target)", "Extras.lua",
        "0.10"),
    Key("tq", "flag", "Target is a mob for an unfinished quest objective (1/0, empty = unknown; only with a target)",
        "Extras.lua", "0.10"),
    Key("cx", "int", "Corpse x on the current map x 10000 (only while dead and the corpse is on this map)",
        "Extras.lua", "0.10"),
    Key("cy", "int", "Corpse y on the current map x 10000 (as cx)", "Extras.lua", "0.10"),
    Key("rz", "int", "Seconds of Resurrection Sickness left (only while it is on)", "Extras.lua", "0.10"),
    Key("er", "int", "Errors the addon caught since the UI loaded (only when there was one; the last is page E)",
        "Core.lua", "0.15.3"),
    Key("e1", "event", "Newest ring event id:kind:arg (see Ring events)", "State.lua", "≤0.7"),
    Key("e2", "event", "Second newest ring event", "State.lua", "≤0.7"),
    Key("e3", "event", "Third newest ring event", "State.lua", "≤0.7"),
)

_LIST_N = Key("n", "int", "Pages in the list (count)", "Extras.lua", "0.10")
_LIST_H = Key("h", "int", "List hash (the same on every page of the list)", "Extras.lua", "0.10")
GEAR_ENTRY = ("key:itemID:equipLoc:classID:subclassID:ilvl:quality:reqLevel:usable:name:stats; stats are CODE value "
              "pairs joined by / (codes as companion/gear.py), e.g. ST5/SA3/AR120/DP7.4/WS2.6")

PAGES = (
    Page("B", False, "Who the player is", "State.lua", "≤0.7", (
        Key("cl", "str", "Class token", "State.lua", "≤0.7"),
        Key("ra", "str", "Race token", "State.lua", "≤0.7"),
        Key("fa", "str", "Faction (Horde, Alliance)", "State.lua", "≤0.7"),
        Key("nm", "str", "Character name", "State.lua", "≤0.7"),
        Key("bd", "str", "Hearthstone bind location (at most 30 bytes)", "State.lua", "≤0.7")), None),
    Page("E", False, "The addon's last error (only once core er counts one)", "Core.lua", "0.15.3", (
        Key("ex", "str", "The error's text (cleaned, at most 115 bytes)", "Core.lua", "0.15.3"),), None),
    Page("K", False, "Weapon and defense skill ranks (clients with skill lines)", "Gear.lua", "≤0.10", (
        Key("sk", "list", "Name rank/max entries joined by , (whole entries, what fits)", "Gear.lua", "≤0.10"),),
        None),
    Page("Z", False, "Zone and map size", "State.lua", "≤0.7", (
        Key("zn", "str", "Zone name", "State.lua", "≤0.7"),
        Key("sz", "str", "Subzone name", "State.lua", "≤0.7"),
        Key("mm", "int", "uiMapID the size is for", "State.lua", "≤0.7"),
        Key("mw", "int", "Map width in yards (empty = unknown)", "State.lua", "≤0.7"),
        Key("mh", "int", "Map height in yards (empty = unknown)", "State.lua", "≤0.7")), None),
    Page("T", False, "Target name", "State.lua", "≤0.7", (
        Key("tn", "str", "Target name (empty = no target)", "State.lua", "≤0.7"),
        Key("tc", "str", "Target classification (normal, elite, rare, ...)", "State.lua", "≤0.7")), None),
    Page("G", False, "RestedXP guide", "State.lua", "≤0.7", (
        Key("gn", "str", "Guide name (empty = no guide)", "State.lua", "≤0.7"),
        Key("gg", "str", "Guide group", "State.lua", "≤0.7"),
        Key("gx", "str", "Next guide (what room is left, at most 30 bytes)", "State.lua", "≤0.7")), None),
    Page("S", "0..2", "RestedXP steps: the current one (S0) and the next two", "State.lua", "≤0.7", (
        Key("i", "int", "Step index (empty = no step)", "State.lua", "≤0.7"),
        Key("el", "list", "Elements: go:map:x:y, <tag>:<questId>[!] (! = done) or a bare tag; at most 70 bytes",
            "State.lua", "≤0.7"),
        Key("tx", "str", "The step's text (what room is left, at most 60 bytes)", "State.lua", "≤0.7")), None),
    Page("Q", True, "Quest log; count and hash in core qn/qh", "State.lua", "≤0.7", (
        Key("q", "list", "Entries", "State.lua", "≤0.7"),),
        "questID:done(1/0):have/need/have/need... (one have/need pair per objective)"),
    Page("A", True, "Buffs on the player and weapon enchants; count and hash in core an/ah", "State.lua", "≤0.7", (
        Key("a", "list", "Entries", "State.lua", "≤0.7"),),
        "spellId:expiry:duration:flags:name; expiry in server time mod 1000000 rounded to 2 s (0 = none); flags m = "
        "cast by the player, k = the player can cast it (0.6.2); weapon enchants as mh/oh:expiry:0:mk:name"),
    Page("W", True, "Worn gear; count and hash in core wn/wh", "Gear.lua", "≤0.7", (
        Key("g", "list", "Entries (key = equipment slot)", "Gear.lua", "≤0.7"),), GEAR_ENTRY),
    Page("I", True, "Equippable gear in the bags; count and hash in core in/ih", "Gear.lua", "≤0.7", (
        Key("g", "list", "Entries (key = bag.slot)", "Gear.lua", "≤0.7"),), GEAR_ENTRY),
    Page("R", True, "Quest reward choices on screen; count and hash in core rn/rh", "Gear.lua", "≤0.7", (
        Key("g", "list", "Entries (key = choice index)", "Gear.lua", "≤0.7"),), GEAR_ENTRY),
    Page("P", True, "Trainable spells the character doesn't know yet (RestedXP's training list, class and race, at or "
         "below the player's level)", "Extras.lua", "0.10", (
        _LIST_N, _LIST_H,
        Key("pl", "int", "Level the list was built for", "Extras.lua", "0.10"),
        Key("k", "int", "Listed spells checked", "Extras.lua", "0.10"),
        Key("u", "int", "Listed spells that couldn't be checked (no spell data)", "Extras.lua", "0.10"),
        Key("p", "list", "Spell ids", "Extras.lua", "0.10")), "spellID"),
    Page("F", True, "Known flight paths (taxi map scans plus RestedXP's list)", "Extras.lua", "0.10", (
        _LIST_N, _LIST_H, Key("f", "list", "Taxi node ids", "Extras.lua", "0.10")), "nodeID"),
    Page("C", True, "Consumables in the bags", "Extras.lua", "0.10", (
        _LIST_N, _LIST_H, Key("c", "list", "Entries", "Extras.lua", "0.10")),
        "kind:itemID:count:reqLevel:name; kind f food, d drink, b food and drink, h bandage, p healing potion, "
        "m mana potion, x poison, w weapon stone or oil, a ammo, s soul shard, r reagent"),
    Page("N", True, "Talents: every talent of the class", "Extras.lua", "0.13", (
        _LIST_N, _LIST_H, Key("t", "list", "Entries", "Extras.lua", "0.13")),
        "<tab><tier><column>:rank:maxRank:name"),
    Page("V", True, "RestedXP steps the player skipped in the current guide (at most 80)", "Extras.lua", "0.13", (
        _LIST_N, _LIST_H, Key("v", "list", "Step indices", "Extras.lua", "0.13")), "stepIndex"),
    Page("D", True, "Vendor treasure near the player (Data/VendorTreasures.lua)", "Extras.lua", "0.13", (
        _LIST_N, _LIST_H,
        Key("v", "int", "Vendor index in Data/VendorTreasures.lua, from 1 (0 = none near)", "Extras.lua", "0.13"),
        Key("d", "list", "Entries", "Extras.lua", "0.13")),
        "v:itemID:equipLoc:classID:subclassID:ilvl:quality:reqLevel:usable:name:stats; gear as in W/I/R, potions "
        "and elixirs as v:itemID:POTION:::0:1:reqLevel:1:name:"),
    Page("X", False, "RestedXP settings that decide which steps it shows (only with RestedXP)", "Extras.lua", "0.13", (
        Key("ss", "flag", "Solo self-found (1/0)", "Extras.lua", "0.13"),
        Key("gq", "flag", "Group quests on (1/0)", "Extras.lua", "0.13")), None),
)
PAGE_ID_KEY = Key("pg", "str", "Page id: every page starts with it", "State.lua", "≤0.7")

EVENTS = (
    Event("acc", "questID", "Quest accepted", "State.lua", "≤0.7"),
    Event("tin", "questID,xp", "Quest turned in", "State.lua", "≤0.7"),
    Event("rem", "questID", "Quest left the log (turned in or abandoned)", "State.lua", "≤0.7"),
    Event("lvl", "level", "Level up", "State.lua", "≤0.7"),
    Event("die", "", "Died", "State.lua", "≤0.7"),
    Event("alive", "", "Alive again (released or resurrected)", "State.lua", "≤0.7"),
    Event("unghost", "", "Back from ghost form", "State.lua", "≤0.7"),
    Event("bind", "location", "Hearthstone bound", "State.lua", "≤0.7"),
    Event("zone", "zone name", "Entered a new zone", "State.lua", "≤0.7"),
    Event("hs", "", "Hearthstone cast", "State.lua", "≤0.7"),
    Event("cst", "spellID", "Own buff (re)cast: the engine refreshes the buff while the aura list is frozen",
          "State.lua", "≤0.7"),
    Event("taxi", "", "Took a flight", "State.lua", "≤0.7"),
    Event("learn", "spellID", "Learned a spell", "State.lua", "≤0.7"),
    Event("fpn", "nodeID", "A flight path seen on the taxi map for the first time. Retired: not sent since 0.15.3 "
          "(list F carries the known flight paths; old copies still send it and the engine ignores it)", "Extras.lua",
          "0.10"),
    Event("mark", "note", "The player flagged this moment (/glide mark or the key binding)", "Core.lua", "≤0.10"),
)

COACH_FLAGS = (
    Flag("coach", 1, "Coach commentary: the master switch, off mutes everything below"),
    Flag("route", 2, "Route: next step, turn-ins, off route, stuck"),
    Flag("combat", 4, "Combat: low health, deaths, ability and pull advice"),
    Flag("buffs", 8, "Buff reminders"),
    Flag("gear", 16, "Gear upgrades and quest reward picks"),
    Flag("pace", 32, "Pace reviews, level-ups, new spells"),
    Flag("upkeep", 64, "Bags full, repairs"),
    Flag("voice", 128, "Spoken callouts (the text still shows)"),
)

ALL_FLAGS_ON = 255           # an addon that sends no cf (older than the switches): every switch on

CORE_BY_NAME = {k.name: k for k in CORE}
PAGE_BY_ID = {p.id: p for p in PAGES}
EVENT_BY_KIND = {e.kind: e for e in EVENTS}


def coach_flags(mask):
    """The core 'cf' bitmask (an int, or None when the addon sent none or junk) -> {switch name: on}."""
    m = ALL_FLAGS_ON if mask is None else mask
    return {f.name: bool(m & f.bit) for f in COACH_FLAGS}


def page_spec(pid):
    """The registered page for a page id as sent ("B", "S1", "Q12"), or None."""
    p = PAGE_BY_ID.get(pid)
    if p and not p.indexed:
        return p
    p = PAGE_BY_ID.get(pid[:1])
    if not p or not p.indexed or not pid[1:].isdigit():
        return None
    if p.indexed is True:
        return p if int(pid[1:]) < 20 else None
    lo, hi = (int(x) for x in p.indexed.split(".."))
    return p if lo <= int(pid[1:]) <= hi else None


def page_keys(pid):
    """Key names a page may carry, "pg" included (None: unregistered page)."""
    p = page_spec(pid)
    return None if p is None else {PAGE_ID_KEY.name} | {k.name for k in p.keys}


# ---------------------------------------------------------------------------------------------------------------
# docs/STRIP.md
# ---------------------------------------------------------------------------------------------------------------
DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "STRIP.md")


def _cell(s):
    return str(s).replace("|", "\\|")


def _table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return out


def _page_name(p):
    if p.indexed is True:
        return "`%s0`..`%s19`" % (p.id, p.id)
    if p.indexed:
        lo, hi = p.indexed.split("..")
        return "`%s%s`..`%s%s`" % (p.id, lo, p.id, hi)
    return "`%s`" % p.id


def markdown():
    out = ["# The Glide strip (addon → engine)", "",
           "Generated from `companion/strip_spec.py` by `python companion/strip_spec.py --write`; edit the registry, "
           "not this file. This is the definition of interface #1 in `docs/REFACTOR-CONTRACTS.md`: keys, pages and "
           "events keep their meaning once sent (they are also stored in `data/live/*.jsonl`); new ones are fine.",
           "", "## Frame", "", FRAME.rstrip(), "",
           "## Core keys", "",
           "Sent in every frame, in this order. Types: int, str, flag (1/0), bitmask (an int), event (`id:kind:arg`); "
           "an empty value means unknown.", ""]
    out += _table(("Key", "Type", "Meaning", "Set by", "Since"),
                  [("`%s`" % k.name, k.type, k.meaning, k.module, k.since) for k in CORE])
    out += ["", "## Pages", "",
            "One page rides along per frame, `pg=<id>;` first. Numbered pages are list chunks (see Lists above).", ""]
    out += _table(("Page", "What", "Keys", "Set by", "Since"),
                  [(_page_name(p), p.meaning, " ".join("`%s`" % k.name for k in p.keys), p.module, p.since)
                   for p in PAGES])
    for p in PAGES:
        out += ["", "### %s" % _page_name(p), "", p.meaning + ".", ""]
        out += _table(("Key", "Type", "Meaning", "Since"),
                      [("`%s`" % k.name, k.type, k.meaning, k.since) for k in p.keys])
        if p.entry:
            fmt, _, note = p.entry.partition("; ")
            out += ["", "Entry: `%s`" % fmt + ("; " + note if note else "") + "."]
    out += ["", "## Ring events", "", "`e1`..`e3` in the core, `id:kind:arg`.", ""]
    out += _table(("Kind", "Arg", "Meaning", "Set by", "Since"),
                  [("`%s`" % e.kind, e.arg, e.meaning, e.module, e.since) for e in EVENTS])
    out += ["", "## Coach flags", "",
            "Core `cf` is the sum of the bits that are on (`/coach`, Coach.lua; companion/rules.py takes the bits "
            "from here). A missing `cf` means all on.", ""]
    out += _table(("Bit", "Name", "Meaning"), [(f.bit, "`%s`" % f.name, f.meaning) for f in COACH_FLAGS])
    return "\n".join(out) + "\n"


def main(argv):
    if "--write" in argv:
        with open(DOC, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(markdown())
        print("wrote", DOC)
        return 0
    if "--check" in argv:
        try:
            with open(DOC, encoding="utf-8") as fh:
                ok = fh.read() == markdown()
        except OSError:
            ok = False
        print("docs/STRIP.md is up to date" if ok else "docs/STRIP.md is out of date: python companion/strip_spec.py "
                                                         "--write")
        return 0 if ok else 1
    sys.stdout.buffer.write(markdown().encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
