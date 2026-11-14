# The Glide strip (addon → engine)

Generated from `companion/strip_spec.py` by `python companion/strip_spec.py --write`; edit the registry, not this file. This is the definition of interface #1 in `docs/REFACTOR-CONTRACTS.md`: keys, pages and events keep their meaning once sent (they are also stored in `data/live/*.jsonl`); new ones are fine.

## Frame

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

## Core keys

Sent in every frame, in this order. Types: int, str, flag (1/0), bitmask (an int), event (`id:kind:arg`); an empty value means unknown.

| Key | Type | Meaning | Set by | Since |
|---|---|---|---|---|
| `gv` | str | Addon version (the TOC's ## Version) | Core.lua | ≤0.7 |
| `gk` | bitmask | Gauges that hold a real value: 1 player health, 2 player power, 4 target health | Strip.lua | 0.6 |
| `cf` | bitmask | Coach switches (see Coach flags); 255 = all on | Coach.lua | ≤0.7 |
| `t` | int | Server time in seconds, mod 1000000 (the clock buff expiries use) | State.lua | ≤0.7 |
| `l` | int | Player level | State.lua | ≤0.7 |
| `x` | int | XP into the level | State.lua | ≤0.7 |
| `X` | int | XP the level needs | State.lua | ≤0.7 |
| `r` | int | Rested XP (0 = none) | State.lua | ≤0.7 |
| `h` | int | Player health % (-1 = hidden: read the gauge) | State.lua | ≤0.7 |
| `m` | int | Player power % (-1 = hidden: read the gauge) | State.lua | ≤0.7 |
| `pt` | str | Power type token (MANA, RAGE, ENERGY, ...) | State.lua | ≤0.7 |
| `cl` | str | Class token (WARRIOR, ...) | State.lua | ≤0.7 |
| `c` | flag | In combat (1/0) | State.lua | ≤0.7 |
| `d` | int | 0 alive, 1 dead, 2 ghost | State.lua | ≤0.7 |
| `mp` | int | uiMapID of the player's map (empty when unknown) | State.lua | ≤0.7 |
| `px` | int | Player x on that map x 10000 (only with mp) | State.lua | ≤0.7 |
| `py` | int | Player y on that map x 10000 (only with mp) | State.lua | ≤0.7 |
| `f` | int | Facing in radians x 100 | State.lua | ≤0.7 |
| `sp` | int | Speed in yards per second x 10 | State.lua | ≤0.7 |
| `g` | int | Money in copper | State.lua | ≤0.7 |
| `b` | int | Free bag slots | State.lua | ≤0.7 |
| `du` | int | Lowest durability % of the equipped items | State.lua | ≤0.7 |
| `hs` | int | Seconds left on the hearthstone cooldown | State.lua | ≤0.7 |
| `rs` | flag | Resting (1/0) | State.lua | ≤0.7 |
| `mo` | flag | Mounted (1/0) | State.lua | ≤0.7 |
| `tx` | flag | On a taxi (1/0) | State.lua | ≤0.7 |
| `kc` | int | Kills that gave XP since the UI loaded | State.lua | ≤0.7 |
| `kx` | int | XP of the last such kill | State.lua | ≤0.7 |
| `tg` | str | Target (only with one): creature or vehicle npc id, P for a player, empty otherwise | State.lua | ≤0.7 |
| `tl` | int | Target level (only with a target) | State.lua | ≤0.7 |
| `th` | int | Target health % (-1 = hidden: read the gauge; only with a target) | State.lua | ≤0.7 |
| `tr` | int | Target reaction to the player, 1 hated .. 8 exalted (only with a target) | State.lua | ≤0.7 |
| `td` | flag | Target dead (1/0; only with a target) | State.lua | ≤0.7 |
| `st` | int | RestedXP's current step index (empty: no guide) | State.lua | ≤0.7 |
| `si` | int | Current step's original ordinal in its own source guide, before filtering/includes; empty when unverified. Not the visible st number. | StepIdentity.lua | 0.17.6 |
| `sh` | str | Current source-block fingerprint: s1:<32 lowercase MD5 hex digits>; empty when unverified. Coherent with si/st in this frame; verifies only that source step, not the whole guide. | StepIdentity.lua | 0.17.6 |
| `qn` | int | Quest log pages Q0.. (count) | State.lua | ≤0.7 |
| `qh` | int | Quest log list hash | State.lua | ≤0.7 |
| `an` | int | Buff pages A0.. (count) | State.lua | ≤0.7 |
| `ah` | int | Buff list hash | State.lua | ≤0.7 |
| `ak` | flag | Buff list stale: aura data hidden in combat or unreadable, the last good list stays (1/0) | State.lua | ≤0.7 |
| `sw` | bitmask | Weapon item subclasses trained, bit n = subclass n (empty = unknown) | Gear.lua | ≤0.10 |
| `sa` | bitmask | Armor item subclasses trained, bit n = subclass n (empty = unknown) | Gear.lua | ≤0.10 |
| `wn` | int | Worn gear pages W0.. (count; only once gear was read) | Gear.lua | ≤0.7 |
| `wh` | int | Worn gear list hash | Gear.lua | ≤0.7 |
| `in` | int | Bag gear pages I0.. (count) | Gear.lua | ≤0.7 |
| `ih` | int | Bag gear list hash | Gear.lua | ≤0.7 |
| `rn` | int | Quest reward choice pages R0.. (count; 0 = no choice on screen) | Gear.lua | ≤0.7 |
| `rh` | int | Quest reward choice list hash | Gear.lua | ≤0.7 |
| `rq` | int | Quest id of the reward choices on screen (0 = none) | Gear.lua | ≤0.7 |
| `tp` | int | Unspent talent points (empty = unknown) | Extras.lua | 0.10 |
| `aw` | str | map:x:y of the waypoint RestedXP's arrow points at (x, y in %, one decimal) | Extras.lua | 0.13.1 |
| `wz` | int | Height (world z) in half yards | Extras.lua | 0.14 |
| `fy` | str | Flight destination of the current step's .fly, while still to do | Extras.lua | 0.14 |
| `fd` | str | Where the flight the player is on goes (the taxi node clicked) | Extras.lua | 0.14 |
| `tt` | flag | Target tapped by someone else (1/0, empty = unknown; only with a target) | Extras.lua | 0.10 |
| `tq` | flag | Target is a mob for an unfinished quest objective (1/0, empty = unknown; only with a target) | Extras.lua | 0.10 |
| `cx` | int | Corpse x on the current map x 10000 (only while dead and the corpse is on this map) | Extras.lua | 0.10 |
| `cy` | int | Corpse y on the current map x 10000 (as cx) | Extras.lua | 0.10 |
| `rz` | int | Seconds of Resurrection Sickness left (only while it is on) | Extras.lua | 0.10 |
| `er` | int | Errors the addon caught since the UI loaded (only when there was one; the last is page E) | Core.lua | 0.15.3 |
| `e1` | event | Newest ring event id:kind:arg (see Ring events) | State.lua | ≤0.7 |
| `e2` | event | Second newest ring event | State.lua | ≤0.7 |
| `e3` | event | Third newest ring event | State.lua | ≤0.7 |

## Pages

One page rides along per frame, `pg=<id>;` first. Numbered pages are list chunks (see Lists above).

| Page | What | Keys | Set by | Since |
|---|---|---|---|---|
| `B` | Who the player is | `cl` `ra` `fa` `nm` `bd` | State.lua | ≤0.7 |
| `E` | The addon's last error (only once core er counts one) | `ex` | Core.lua | 0.15.3 |
| `K` | Weapon and defense skill ranks (clients with skill lines) | `sk` | Gear.lua | ≤0.10 |
| `Z` | Zone and map size | `zn` `sz` `mm` `mw` `mh` | State.lua | ≤0.7 |
| `T` | Target name | `tn` `tc` | State.lua | ≤0.7 |
| `G` | RestedXP guide | `gn` `gg` `gx` | State.lua | ≤0.7 |
| `S0`..`S2` | RestedXP steps: the current one (S0) and the next two | `i` `el` `tx` | State.lua | ≤0.7 |
| `Q0`..`Q19` | Quest log; count and hash in core qn/qh | `q` | State.lua | ≤0.7 |
| `A0`..`A19` | Buffs on the player and weapon enchants; count and hash in core an/ah | `a` | State.lua | ≤0.7 |
| `W0`..`W19` | Worn gear; count and hash in core wn/wh | `g` | Gear.lua | ≤0.7 |
| `I0`..`I19` | Equippable gear in the bags; count and hash in core in/ih | `g` | Gear.lua | ≤0.7 |
| `R0`..`R19` | Quest reward choices on screen; count and hash in core rn/rh | `g` | Gear.lua | ≤0.7 |
| `P0`..`P19` | Trainable spells the character doesn't know yet (RestedXP's training list, class and race, at or below the player's level) | `n` `h` `pl` `k` `u` `p` | Extras.lua | 0.10 |
| `F0`..`F19` | Known flight paths (taxi map scans plus RestedXP's list) | `n` `h` `f` | Extras.lua | 0.10 |
| `C0`..`C19` | Consumables in the bags | `n` `h` `c` | Extras.lua | 0.10 |
| `N0`..`N19` | Talents: every talent of the class | `n` `h` `t` | Extras.lua | 0.13 |
| `V0`..`V19` | RestedXP steps the player skipped in the current guide (at most 80) | `n` `h` `v` | Extras.lua | 0.13 |
| `D0`..`D19` | Vendor treasure near the player (Data/VendorTreasures.lua) | `n` `h` `v` `d` | Extras.lua | 0.13 |
| `X` | RestedXP settings that decide which steps it shows (only with RestedXP) | `ss` `gq` | Extras.lua | 0.13 |

### `B`

Who the player is.

| Key | Type | Meaning | Since |
|---|---|---|---|
| `cl` | str | Class token | ≤0.7 |
| `ra` | str | Race token | ≤0.7 |
| `fa` | str | Faction (Horde, Alliance) | ≤0.7 |
| `nm` | str | Character name | ≤0.7 |
| `bd` | str | Hearthstone bind location (at most 30 bytes) | ≤0.7 |

### `E`

The addon's last error (only once core er counts one).

| Key | Type | Meaning | Since |
|---|---|---|---|
| `ex` | str | The error's text (cleaned, at most 115 bytes) | 0.15.3 |

### `K`

Weapon and defense skill ranks (clients with skill lines).

| Key | Type | Meaning | Since |
|---|---|---|---|
| `sk` | list | Name rank/max entries joined by , (whole entries, what fits) | ≤0.10 |

### `Z`

Zone and map size.

| Key | Type | Meaning | Since |
|---|---|---|---|
| `zn` | str | Zone name | ≤0.7 |
| `sz` | str | Subzone name | ≤0.7 |
| `mm` | int | uiMapID the size is for | ≤0.7 |
| `mw` | int | Map width in yards (empty = unknown) | ≤0.7 |
| `mh` | int | Map height in yards (empty = unknown) | ≤0.7 |

### `T`

Target name.

| Key | Type | Meaning | Since |
|---|---|---|---|
| `tn` | str | Target name (empty = no target) | ≤0.7 |
| `tc` | str | Target classification (normal, elite, rare, ...) | ≤0.7 |

### `G`

RestedXP guide.

| Key | Type | Meaning | Since |
|---|---|---|---|
| `gn` | str | Guide name (empty = no guide) | ≤0.7 |
| `gg` | str | Guide group | ≤0.7 |
| `gx` | str | Next guide (what room is left, at most 30 bytes) | ≤0.7 |

### `S0`..`S2`

RestedXP steps: the current one (S0) and the next two.

| Key | Type | Meaning | Since |
|---|---|---|---|
| `i` | int | Step index (empty = no step) | ≤0.7 |
| `el` | list | Elements: go:map:x:y, <tag>:<questId>[!] (! = done) or a bare tag; at most 70 bytes | ≤0.7 |
| `tx` | str | The step's text (what room is left, at most 60 bytes) | ≤0.7 |

### `Q0`..`Q19`

Quest log; count and hash in core qn/qh.

| Key | Type | Meaning | Since |
|---|---|---|---|
| `q` | list | Entries | ≤0.7 |

Entry: `questID:done(1/0):have/need/have/need... (one have/need pair per objective)`.

### `A0`..`A19`

Buffs on the player and weapon enchants; count and hash in core an/ah.

| Key | Type | Meaning | Since |
|---|---|---|---|
| `a` | list | Entries | ≤0.7 |

Entry: `spellId:expiry:duration:flags:name`; expiry in server time mod 1000000 rounded to 2 s (0 = none); flags m = cast by the player, k = the player can cast it (0.6.2); weapon enchants as mh/oh:expiry:0:mk:name.

### `W0`..`W19`

Worn gear; count and hash in core wn/wh.

| Key | Type | Meaning | Since |
|---|---|---|---|
| `g` | list | Entries (key = equipment slot) | ≤0.7 |

Entry: `key:itemID:equipLoc:classID:subclassID:ilvl:quality:reqLevel:usable:name:stats`; stats are CODE value pairs joined by / (codes as companion/gear.py), e.g. ST5/SA3/AR120/DP7.4/WS2.6.

### `I0`..`I19`

Equippable gear in the bags; count and hash in core in/ih.

| Key | Type | Meaning | Since |
|---|---|---|---|
| `g` | list | Entries (key = bag.slot) | ≤0.7 |

Entry: `key:itemID:equipLoc:classID:subclassID:ilvl:quality:reqLevel:usable:name:stats`; stats are CODE value pairs joined by / (codes as companion/gear.py), e.g. ST5/SA3/AR120/DP7.4/WS2.6.

### `R0`..`R19`

Quest reward choices on screen; count and hash in core rn/rh.

| Key | Type | Meaning | Since |
|---|---|---|---|
| `g` | list | Entries (key = choice index) | ≤0.7 |

Entry: `key:itemID:equipLoc:classID:subclassID:ilvl:quality:reqLevel:usable:name:stats`; stats are CODE value pairs joined by / (codes as companion/gear.py), e.g. ST5/SA3/AR120/DP7.4/WS2.6.

### `P0`..`P19`

Trainable spells the character doesn't know yet (RestedXP's training list, class and race, at or below the player's level).

| Key | Type | Meaning | Since |
|---|---|---|---|
| `n` | int | Pages in the list (count) | 0.10 |
| `h` | int | List hash (the same on every page of the list) | 0.10 |
| `pl` | int | Level the list was built for | 0.10 |
| `k` | int | Listed spells checked | 0.10 |
| `u` | int | Listed spells that couldn't be checked (no spell data) | 0.10 |
| `p` | list | Spell ids | 0.10 |

Entry: `spellID`.

### `F0`..`F19`

Known flight paths (taxi map scans plus RestedXP's list).

| Key | Type | Meaning | Since |
|---|---|---|---|
| `n` | int | Pages in the list (count) | 0.10 |
| `h` | int | List hash (the same on every page of the list) | 0.10 |
| `f` | list | Taxi node ids | 0.10 |

Entry: `nodeID`.

### `C0`..`C19`

Consumables in the bags.

| Key | Type | Meaning | Since |
|---|---|---|---|
| `n` | int | Pages in the list (count) | 0.10 |
| `h` | int | List hash (the same on every page of the list) | 0.10 |
| `c` | list | Entries | 0.10 |

Entry: `kind:itemID:count:reqLevel:name`; kind f food, d drink, b food and drink, h bandage, p healing potion, m mana potion, x poison, w weapon stone or oil, a ammo, s soul shard, r reagent.

### `N0`..`N19`

Talents: every talent of the class.

| Key | Type | Meaning | Since |
|---|---|---|---|
| `n` | int | Pages in the list (count) | 0.10 |
| `h` | int | List hash (the same on every page of the list) | 0.10 |
| `t` | list | Entries | 0.13 |

Entry: `<tab><tier><column>:rank:maxRank:name`.

### `V0`..`V19`

RestedXP steps the player skipped in the current guide (at most 80).

| Key | Type | Meaning | Since |
|---|---|---|---|
| `n` | int | Pages in the list (count) | 0.10 |
| `h` | int | List hash (the same on every page of the list) | 0.10 |
| `v` | list | Step indices | 0.13 |

Entry: `stepIndex`.

### `D0`..`D19`

Vendor treasure near the player (Data/VendorTreasures.lua).

| Key | Type | Meaning | Since |
|---|---|---|---|
| `n` | int | Pages in the list (count) | 0.10 |
| `h` | int | List hash (the same on every page of the list) | 0.10 |
| `v` | int | Vendor index in Data/VendorTreasures.lua, from 1 (0 = none near) | 0.13 |
| `d` | list | Entries | 0.13 |

Entry: `v:itemID:equipLoc:classID:subclassID:ilvl:quality:reqLevel:usable:name:stats`; gear as in W/I/R, potions and elixirs as v:itemID:POTION:::0:1:reqLevel:1:name:.

### `X`

RestedXP settings that decide which steps it shows (only with RestedXP).

| Key | Type | Meaning | Since |
|---|---|---|---|
| `ss` | flag | Solo self-found (1/0) | 0.13 |
| `gq` | flag | Group quests on (1/0) | 0.13 |

## Ring events

`e1`..`e3` in the core, `id:kind:arg`.

| Kind | Arg | Meaning | Set by | Since |
|---|---|---|---|---|
| `acc` | questID | Quest accepted | State.lua | ≤0.7 |
| `tin` | questID,xp | Quest turned in | State.lua | ≤0.7 |
| `rem` | questID | Quest left the log (turned in or abandoned) | State.lua | ≤0.7 |
| `lvl` | level | Level up | State.lua | ≤0.7 |
| `die` |  | Died | State.lua | ≤0.7 |
| `alive` |  | Alive again (released or resurrected) | State.lua | ≤0.7 |
| `unghost` |  | Back from ghost form | State.lua | ≤0.7 |
| `bind` | location | Hearthstone bound | State.lua | ≤0.7 |
| `zone` | zone name | Entered a new zone | State.lua | ≤0.7 |
| `hs` |  | Hearthstone cast | State.lua | ≤0.7 |
| `cst` | spellID | Own buff (re)cast: the engine refreshes the buff while the aura list is frozen | State.lua | ≤0.7 |
| `taxi` |  | Took a flight | State.lua | ≤0.7 |
| `learn` | spellID | Learned a spell | State.lua | ≤0.7 |
| `fpn` | nodeID | A flight path seen on the taxi map for the first time. Retired: not sent since 0.15.3 (list F carries the known flight paths; old copies still send it and the engine ignores it) | Extras.lua | 0.10 |
| `mark` | note | The player flagged this moment (/glide mark or the key binding) | Core.lua | ≤0.10 |

## Coach flags

Core `cf` is the sum of the bits that are on (`/coach`, Coach.lua; companion/rules.py takes the bits from here). A missing `cf` means all on.

| Bit | Name | Meaning |
|---|---|---|
| 1 | `coach` | Coach commentary: the master switch, off mutes everything below |
| 2 | `route` | Route: next step, turn-ins, off route, stuck |
| 4 | `combat` | Combat: low health, deaths, ability and pull advice |
| 8 | `buffs` | Buff reminders |
| 16 | `gear` | Gear upgrades and quest reward picks |
| 32 | `pace` | Pace reviews, level-ups, new spells |
| 64 | `upkeep` | Bags full, repairs |
| 128 | `voice` | Spoken callouts (the text still shows) |
