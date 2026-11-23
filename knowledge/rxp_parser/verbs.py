"""Per-verb argument parsers (functions.lua). Each returns an args dict; keys starting with '_' are lifted into the
action row: _q quest_id, _k entity_kind, _e entity_id, _n qty."""
import re

from knowledge.rxp_parser.maps import dungeon_tag
from knowledge.rxp_parser.text import NONSPACE, SPACE, WS, lua_float, lua_int, lua_num

_RE_COMMA = re.compile(SPACE + r"*," + SPACE + r"*")
_RE_SEMI = re.compile(SPACE + r"*;" + SPACE + r"*")


def _get(lst, i):
    return lst[i] if i < len(lst) else None


def _split_args(verb, raw):
    if verb in ("target", "mob", "unitscan"):                   # addon.separators: semicolonsep
        return [x for x in _RE_SEMI.sub(";", raw).split(";") if x]
    if verb in ("link", "clicknext"):                           # addon.separators.link: whole string
        return [raw.rstrip(WS)]
    return [x for x in _RE_COMMA.sub(",", raw).split(",") if x]


def _pos(zone, x, y, maps):
    """goto/waypoint/pin position. Never converts between the two forms (no bounds here).

    'map,x,y'           -> ui_map, x, y in map percent.
    'map/continent,A,B' -> ui_map, continent, wx = B, wy = A in world yards. RXP/HereBeDragons write the world
                           form in map-axis order (A follows the map's horizontal axis = world Y, B the vertical
                           axis = world X); kb.db's binding convention (store.py) is wx = world X (north),
                           wy = world Y (west), so A is wy and B is wx.
    """
    a = {"zone": zone}
    if zone is None:
        a["error"] = "no zone"
        return a
    m = re.match(r"(.*?)/([0-9]+)", zone)
    if m:
        a["ui_map"] = maps.ui_map(m.group(1))
        a["continent"] = int(m.group(2))
        a["wx"], a["wy"] = lua_float(y), lua_float(x)
        if a["wx"] is None or a["wy"] is None:
            a["error"] = "bad coordinates"
    else:
        a["ui_map"] = maps.ui_map(zone)
        a["x"], a["y"] = lua_float(x), lua_float(y)
        if a["x"] is None or a["y"] is None:
            a["error"] = "bad coordinates"
    if a["ui_map"] is None:
        a["error"] = a.get("error") or "unknown map"
    return a


def _p_goto(L, raw, text, maps):
    a = _pos(_get(L, 0), _get(L, 1), _get(L, 2), maps)
    radius, opt = lua_float(_get(L, 3)), _get(L, 4)
    if radius is not None:
        a["radius"] = radius
    if opt is not None:
        a["optional"] = opt
    # element behaviour in functions.lua goto: arrive = checkbox completes within radius; path = hidden pin,
    # arrow advances (radius,optional); lowprio = radius 0 + optional; noarrow = radius 0; dynamic = radius < 0
    if radius is None:
        a["mode"] = "wp"
    elif radius < 0:
        a["mode"] = "dynamic"
    elif opt is not None:
        a["mode"] = "lowprio" if radius == 0 else "path"
    else:
        a["mode"] = "noarrow" if radius == 0 else "arrive"
    return a


def _p_waypoint(L, raw, text, maps):
    a = _pos(_get(L, 0), _get(L, 1), _get(L, 2), maps)
    radius, low = lua_float(_get(L, 3)), _get(L, 4)
    if low is not None and lua_num(low) is None:                # callback form
        a["callback"], a["events"] = low, L[5:]
        a["args"] = radius
    else:
        if radius is not None:
            a["radius"] = abs(radius)
            if radius < 0:
                a["persistent"] = True
            if radius == 0:
                a["lowprio"] = True
        if low is not None:
            a["lowprio"] = True
    return a


def _p_questgoto(L, raw, text, maps):
    a = _pos(_get(L, 0), _get(L, 1), _get(L, 2), maps)
    a["radius"] = lua_float(_get(L, 3))
    a["_q"] = a["quest_id"] = lua_int(_get(L, 4))
    a["obj"] = lua_int(_get(L, 5))
    a["obj_max"] = lua_int(_get(L, 6))
    if a["radius"] is None or a["quest_id"] is None or a["obj"] is None:
        a["error"] = a.get("error") or "invalid radius/quest/objective"
    if _get(L, 7) is not None:
        a["optional"] = L[7]
    return a


def _p_pin(L, raw, text, maps):
    a = _pos(_get(L, 0), _get(L, 1), _get(L, 2), maps)
    if _get(L, 3) is not None:
        a["tooltip"] = L[3]
    return a


def _p_treasure(L, raw, text, maps):
    a = _pos(_get(L, 0), _get(L, 1), _get(L, 2), maps)
    a["_q"] = a["quest_id"] = lua_int(_get(L, 3))
    return a


def _p_rare(L, raw, text, maps):
    a = _pos(_get(L, 0), _get(L, 1), _get(L, 2), maps)
    nid = _get(L, 3)
    a["rare"] = nid
    m = re.search(r"[0-9]+", nid or "")
    if m:
        a["_k"], a["_e"] = "npc", int(m.group(0))
    return a


def _points(vals):
    nums = [lua_float(v) for v in vals]
    return [[nums[i], nums[i + 1]] for i in range(0, len(nums) - 1, 2)]


def _p_line(L, raw, text, maps):
    zone = _get(L, 0) or ""
    a = {"zone": zone}
    z = zone[1:] if zone[:1] == "+" else zone
    m = re.match(r"([0-9]+)/([0-9]+)", z)
    if m:                                  # world form: pairs are (wy, wx) in the text, stored as [wx, wy]
        a["ui_map"], a["continent"] = int(m.group(1)), int(m.group(2))
        a["wpoints"] = [[q[1], q[0]] for q in _points(L[1:])]
    else:
        a["ui_map"] = maps.ui_map(z)
        a["points"] = _points(L[1:])
    return a


def _p_loop(L, raw, text, maps):
    rng = _get(L, 0) or ""
    a = {}
    if rng[:1] in ("+", "*", "@"):
        a["style"], rng = rng[0], rng[1:]
    a["range"] = lua_float(rng)
    zone = _get(L, 1) or ""
    a["zone"] = zone
    m = re.match(r"([0-9]+)/([0-9]+)", zone[1:] if zone[:1] == "+" else zone)
    if m:                                  # world form, as .line: pairs are (wy, wx) in the text, stored as [wx, wy]
        a["ui_map"], a["continent"] = int(m.group(1)), int(m.group(2))
        a["wpoints"] = [[q[1], q[0]] for q in _points(L[2:])]
    else:
        a["ui_map"] = maps.ui_map(zone)
        a["points"] = _points(L[2:])
    return a


def _p_accept(L, raw, text, maps):
    qid = lua_int(_get(L, 0))
    flags = lua_int(_get(L, 1)) or 0
    a = {"_q": qid, "quest_id": qid, "flags": flags}
    if qid is None:
        a["error"] = "invalid quest id"
    if flags & 0x1:
        a["no_auto"] = True
    if flags & 0x2:
        a["required_turnin"] = lua_int(_get(L, 2))
    return a


def _p_turnin(L, raw, text, maps):
    qid = lua_int(_get(L, 0))
    a = {"flags": lua_int(_get(L, 2)) or 0}
    reward = lua_int(_get(L, 1)) or 0
    if qid is None:
        a["error"] = "invalid quest id"
    elif qid < 0:                                              # negative id: skip if not on the quest
        qid = -qid
        a["skip_if_missing"] = True
        if reward > 0:
            a["skip_if_incomplete"] = True
        reward = None
    a["_q"] = a["quest_id"] = qid
    if reward:
        a["reward"] = reward
    return a


def _p_complete(L, raw, text, maps):
    qid, obj = lua_int(_get(L, 0)), lua_int(_get(L, 1))
    obj_max = lua_int(_get(L, 2))
    if obj_max is not None and obj_max <= 0:
        obj_max = None
    flags = lua_int(_get(L, 3)) or 0
    a = {"obj": obj, "flags": flags}
    if qid is None or obj is None:
        a["error"] = "invalid objective or quest id"
    if qid is not None and qid < 0:
        qid = -qid
        a["skip_if_missing"] = True
    if flags % 2 == 1:
        a["text_only"] = True
    a["_q"] = a["quest_id"] = qid
    if obj_max is not None:
        a["obj_max"] = a["_n"] = obj_max
    return a


def _p_quest_list(L, raw, text, maps):
    ids = [lua_int(v) for v in L]
    a = {"quest_ids": ids}
    a["_q"] = next((i for i in ids if i is not None), None)
    if a["_q"] is None:
        a["error"] = "invalid quest id"
    return a


def _p_abandon(L, raw, text, maps):
    a = _p_quest_list(L, raw, text, maps)
    if text and text[:1] == "*":
        a["turnin_icon"] = True
    return a


def _p_is_quest_complete(L, raw, text, maps):
    qid = lua_int(_get(L, 0))
    a = {"_q": qid, "quest_id": qid}
    if _get(L, 1) == "account":
        a["account"] = True
    if qid is None:
        a["error"] = "invalid quest id"
    return a


def _p_is_turned_in(L, raw, text, maps):
    ids, extra = [], None
    for v in L:
        i = lua_int(v)
        if i is not None:
            ids.append(i)
        elif extra is None:
            extra = v
    a = {"quest_ids": ids, "_q": ids[0] if ids else None}
    if extra == "account":
        a["account"] = True
    if not ids:
        a["error"] = "invalid quest id"
    return a


def _p_questcount(L, raw, text, maps):
    m = re.search(r"([<>]?)" + SPACE + r"*([0-9]+)(\**)", _get(L, 0) or "")
    a = {"quest_ids": [lua_int(v) for v in L[1:]]}
    if m:
        a["op"], a["count"] = m.group(1), int(m.group(2))
        if m.group(3) == "*":
            a["ignore_turned_in"] = True
    else:
        a["error"] = "invalid count"
    a["_q"] = next((i for i in a["quest_ids"] if i is not None), None)
    return a


_COLLECT_FLAGS = ((0x1, "text_only"), (0x2, "subtract"), (0x4, "check_objectives"), (0x8, "include_bank"),
                  (0x10, "ignore_turnin"), (0x20, "profession"), (0x40, "ignore_if_none"))


def _p_collect(L, raw, text, maps):
    ids_raw = _get(L, 0) or ""
    m = re.match(r"[0-9]+", ids_raw)
    item = int(m.group(0)) if m else None
    qty = lua_int(_get(L, 1))
    qid = lua_int(_get(L, 2))
    obj_flags = lua_int(_get(L, 3)) or 0
    flags = lua_int(_get(L, 4)) or 0
    a = {"item_id": item, "qty": qty if qty is not None else 1}
    ids = [int(x) for x in re.findall(r"[0-9]+", ids_raw)]
    if len(ids) > 1:
        a["item_ids"] = ids
    if qid is not None:
        a["quest_id"] = qid
    if obj_flags:
        a["obj_flags"] = obj_flags
        a["objectives"] = [i + 1 for i in range(8) if obj_flags >> i & 1]
    if flags < 0:                                              # negative: multiplier, same as flags 3
        a["multiplier"] = -flags
        flags = 3
    if flags:
        a["flags"] = flags
        for bit, name in _COLLECT_FLAGS:
            if flags & bit:
                a[name] = True
    if len(L) > 5:
        a["extra"] = L[5:]
    if item is None:
        a["error"] = "invalid item id"
    a["_k"], a["_e"], a["_n"], a["_q"] = "item", item, a["qty"], qid
    return a


def _p_collectmultiple(L, raw, text, maps):
    item = lua_int(_get(L, 0))
    return {"item_id": item, "_k": "item", "_e": item}


def _p_item_first(L, raw, text, maps):
    item = lua_int(_get(L, 0))
    a = {"item_id": item, "_k": "item", "_e": item}
    if item is None:
        a["error"] = "invalid item id"
    return a


def _p_buy(L, raw, text, maps):
    item = lua_int(_get(L, 0))
    qty = lua_int(_get(L, 1))
    a = {"item_id": item, "qty": qty if qty is not None else 1}
    qid = lua_int(_get(L, 2))
    if qid is not None:
        a["quest_id"] = qid
    if lua_int(_get(L, 3)) is not None:
        a["obj"] = lua_int(L[3])
    a["_k"], a["_e"], a["_n"], a["_q"] = "item", item, a["qty"], qid
    return a


def _xp_like(s):
    s = (s or "").replace(" ", "")
    m = re.search(r"(<?)" + SPACE + r"*([0-9]+)([+.\-]?[0-9]*)", s)
    if not m:
        return None
    xp = lua_num(m.group(3))
    return m.group(1), int(m.group(2)), xp if xp is not None else 0


def _p_xp(L, raw, text, maps):
    r = _xp_like(_get(L, 0))
    if r is None:
        return {"error": "invalid syntax"}
    op, level, xp = r
    a = {"level": level, "xp": xp}
    if xp < 0:
        a["xp_kind"] = "before"            # xp away from `level` (i.e. into level-1)
    elif xp >= 1:
        a["xp_kind"] = "into"              # xp into `level`
    elif xp > 0:
        a["xp_kind"] = "frac"              # fraction of `level`
    skip = lua_int(_get(L, 1))
    reverse = op == "<" or (skip is not None and skip < 0)
    if op == "<":
        a["op"] = "<"
    if reverse:
        a["reverse"] = True
    if skip is not None:
        a["skipstep"] = skip
        a["skip_when"] = "below" if reverse else "at_or_above"
    if _get(L, 2) is not None:
        a["label"] = L[2]
    return a


def _p_maxlevel(L, raw, text, maps):
    r = _xp_like((_get(L, 0) or "").lstrip("<"))
    if r is None:
        return {"error": "invalid syntax"}
    a = {"level": r[1], "xp": r[2], "skip_when": "above"}
    if _get(L, 1) is not None:
        a["label"] = L[1]
    return a


def _p_skill(L, raw, text, maps):
    name = _get(L, 0)
    m = re.search(r"(<?)" + SPACE + r"*([0-9]+)", (_get(L, 1) or "").replace(" ", ""))
    a = {"skill": name}
    if not (m and name):
        a["error"] = "invalid skill or level"
        return a
    a["level"] = int(m.group(2))
    reverse = m.group(1) == "<"
    if reverse:
        a["reverse"] = True
    skip = lua_int(_get(L, 2))
    if skip is not None:
        a["skipstep"] = skip
        a["skip_when"] = "below" if reverse else "at_or_above"
    if _get(L, 3) is not None:
        a["use_max"] = L[3]
    return a


def _p_npc_id(L, raw, text, maps):
    npc = lua_int(_get(L, 0))
    a = {}
    if npc is not None:
        a["npc_id"] = npc
        a["_k"], a["_e"] = "npc", npc
    return a


def _p_money(L, raw, text, maps):
    s = _get(L, 0) or ""
    a = {}
    if s[:1] not in ("<", ">"):
        a["error"] = "invalid arguments"
        return a
    m = re.search(r"([0-9]+\.?[0-9]*)", s)
    if not m:
        a["error"] = "invalid arguments"
        return a
    a["op"] = s[0]
    a["gold"] = float(m.group(1))
    a["copper"] = int(round(float(m.group(1)) * 1e4))
    a["skip_when"] = "below" if s[0] == "<" else "at_or_above"   # step completes (skips) on this side
    if _get(L, 1) == "1":
        a["net_worth"] = True
    return a


def _p_train(L, raw, text, maps):
    sid = lua_int(_get(L, 0))
    flags = lua_int(_get(L, 1)) or 0
    a = {"spell_id": sid, "flags": flags}
    if sid is None:
        a["error"] = "invalid spell id"
    if flags & 0x1:
        a["skip_if_known"] = True          # text-only: step skipped once the spell is known
    if flags & 0x2:
        a["reverse"] = True
    a["_k"], a["_e"] = "spell", sid
    return a


def _p_spell_list(L, raw, text, maps):
    ids = [lua_int(v) for v in L]
    a = {"spell_ids": ids}
    first = next((i for i in ids if i is not None), None)
    a["_k"], a["_e"] = ("spell", first) if first is not None else (None, None)
    return a


def _p_cast(L, raw, text, maps):
    a = {"unit": "player"}
    ids = []
    for i, v in enumerate(L):
        n = lua_int(v)
        if i == 0 and n is None:
            a["unit"] = v
            continue
        ids.append(n)
    a["spell_ids"] = ids
    first = next((i for i in ids if i is not None), None)
    if first is not None:
        a["_k"], a["_e"] = "spell", first
    return a


def _p_area(L, raw, text, maps):
    area = lua_int(_get(L, 0))
    flags = lua_int(_get(L, 1)) or 0
    a = {"area_id": area, "flags": flags}
    if area is None:
        a["error"] = "invalid zone id"
    if flags & 0x1:
        a["reverse"] = True
    if flags & 0x2:
        a["no_map_only"] = True
    a["_k"], a["_e"] = ("area", area) if area is not None else (None, None)
    return a


def _p_zone(L, raw, text, maps):
    z = _get(L, 0)
    ui = maps.ui_map(z)
    a = {"zone": z, "ui_map": ui}
    if ui is None:
        a["error"] = "unknown map"
    else:
        a["_k"], a["_e"] = "map", ui
    return a


def _p_zoneskip(L, raw, text, maps):
    zones = [z for z in re.split(r"[+/]", _get(L, 0) or "") if z]
    flags = lua_int(_get(L, 1)) or 0
    ids = [maps.ui_map(z) for z in zones]
    a = {"zones": zones, "ui_maps": ids, "flags": flags}
    if flags & 0x1:
        a["reverse"] = True                # skip when NOT in the zone
    if ids and ids[0] is not None:
        a["_k"], a["_e"] = "map", ids[0]
    if not zones:
        a["error"] = "no map"
    return a


def _p_explore(L, raw, text, maps):
    area = lua_int(_get(L, 0))
    z = _get(L, 1)
    return {"area_id": area, "zone": z, "ui_map": maps.ui_map(z), "_k": "area" if area else None, "_e": area}


def _p_home(L, raw, text, maps):
    loc = _get(L, 0)
    a = {}
    area = lua_int(loc)
    if area is not None:
        a["area_id"] = area
        a["_k"], a["_e"] = "area", area
    elif loc is not None:
        a["location"] = loc
    return a


def _p_bindlocation(L, raw, text, maps):
    a = _p_home(L, raw, text, maps)
    flags = lua_int(_get(L, 1)) or 0
    a["flags"] = flags
    if flags % 2 == 1:
        a["reverse"] = True
    return a


def _p_fp(L, raw, text, maps):
    a = {"location": _get(L, 0)}
    f = lua_int(_get(L, 1))
    if f is not None:
        a["flags"] = f
        if f % 2 == 1:
            a["text_only"] = True
    return a


def _p_location(L, raw, text, maps):
    a = {}
    if L:
        a["location"] = L[0]
    if len(L) > 1:
        a["extra"] = L[1:]
    return a


def _p_link(L, raw, text, maps):
    url = _get(L, 0)
    return {"url": re.sub(r"\\?\\-", "-", url) if url else url}


def _p_next(L, raw, text, maps):
    return {"next": _get(L, 0)}


def _units(L):
    out, parent = [], False
    for i, v in enumerate(L):
        if i == 0 and v[:1] == "+":
            v, parent = v[1:], True
        m = re.match(r"(.+)::([0-9]+)$", v)
        if m:
            out.append({"name": m.group(1), "id": int(m.group(2))})
        else:
            n = lua_int(v)
            out.append({"name": None, "id": n} if n is not None else {"name": v, "id": None})
    return out, parent


def _p_units(L, raw, text, maps):
    units, parent = _units(L)
    a = {"name": units[0]["name"] if units else None, "units": units}
    if parent:
        a["parent"] = True
    nid = next((u["id"] for u in units if u["id"] is not None), None)
    a["_k"] = "npc" if units else None
    a["_e"] = nid
    return a


def _p_items(L, raw, text, maps):
    items = [lua_int(v) if lua_int(v) is not None else v for v in L]
    a = {"items": items}
    first = next((i for i in items if isinstance(i, int)), None)
    if first is not None:
        a["_k"], a["_e"] = "item", first
    return a


def _p_use(L, raw, text, maps):
    items = []
    for v in L:
        m = re.search(r"([0-9]+):?(" + NONSPACE + r"*)", v)             # v:match("(%d+):?(%S*)")
        if m:
            it = {"id": int(m.group(1))}
            if m.group(2):
                it["arg"] = m.group(2)
            items.append(it)
        else:
            items.append({"id": None, "raw": v})
    a = {"items": items}
    first = next((i["id"] for i in items if i["id"] is not None), None)
    if first is None:
        a["error"] = "invalid item id"
    return a, first


def _p_use_item(L, raw, text, maps):
    a, first = _p_use(L, raw, text, maps)
    if first is not None:
        a["_k"], a["_e"] = "item", first
    return a


def _p_use_spell(L, raw, text, maps):
    a, first = _p_use(L, raw, text, maps)
    if first is not None:
        a["_k"], a["_e"] = "spell", first
    return a


def _p_macro(L, raw, text, maps):
    icon = lua_int(_get(L, 1))
    return {"name": _get(L, 0), "icon": icon if icon is not None else 134400, "body": text}


def _p_itemcount(L, raw, text, maps):
    ids = [int(x) for x in re.findall(r"[0-9]+", _get(L, 0) or "")]
    m = re.search(r"([<>]?)(=?)" + SPACE + r"*([0-9]+)", (_get(L, 1) or "").replace(" ", ""))
    a = {"item_ids": ids}
    if not (m and ids):
        a["error"] = "invalid item id/count"
        return a
    op, eq, total = m.group(1), m.group(2) == "=", int(m.group(3))
    # step is skipped unless count <cmp> total holds
    a["cmp"] = {("<", True): "<=", (">", True): ">=", ("", True): "==",
                ("<", False): "<", (">", False): ">", ("", False): ">="}[(op, eq)]
    a["total"] = total
    if _get(L, 2) is not None:
        a["bank"] = L[2]
    a["_k"], a["_e"], a["_n"] = "item", ids[0], total
    return a


def _p_itemstat(L, raw, text, maps):
    slot, stat, val = lua_int(_get(L, 0)), _get(L, 1), _get(L, 2)
    a = {"slot": slot, "stat": stat}
    if val is not None:
        m = re.match(r"([<>]?)(.+)", val.replace(" ", ""))
        if m:
            a["op"] = m.group(1)
            v = lua_num(m.group(2))
            a["value"] = v if v is not None else m.group(2)
    if slot is None or stat is None or "value" not in a:
        a["error"] = "invalid arguments"
    return a


def _p_cooldown(L, raw, text, maps):
    typ, cid = _get(L, 0), lua_int(_get(L, 1))
    a = {"type": typ, "id": cid}
    m = re.search(r"([<>]?)" + SPACE + r"*([0-9]+[!-/:-@\[-`{-~]?[0-9]*)" + SPACE + r"*([ms]?)", _get(L, 2) or "")
    cd = 0.0
    if m:
        a["op"] = m.group(1) or ">"
        cd = lua_float(m.group(2)) or 0.0
        if m.group(3) == "m":
            cd *= 60
    a["seconds"] = max(cd, 2.0)
    if _get(L, 3) is not None:
        a["update_once"] = L[3]
    if cid is None:
        a["error"] = "invalid arguments"
    if cid is not None and typ in ("item", "spell"):
        a["_k"], a["_e"] = typ, cid
    return a


def _p_timer(L, raw, text, maps):
    a = {"seconds": lua_float(_get(L, 0)), "timer_text": _get(L, 1)}
    if _get(L, 2) is not None:
        a["callback"], a["events"] = L[2], L[3:]
    return a


def _p_duration(L, raw, text, maps):
    return {"seconds": lua_float(_get(L, 0)) or 0.0}


def _p_dungeon(L, raw, text, maps):
    inst = _get(L, 0) or ""
    a = {"dungeon": inst.lstrip("!")}
    if inst[:1] == "!":
        a["skip"] = True
    return a


def _p_dungeon_tag(L, raw, text, maps):
    """`.dungeon [!]X`: X as RestedXP's dungeon tag (GetDungeonName: 'Stockades' -> 'STOCKS'); "unknown" when RXP
    knows no such dungeon (it reports a parse error and the step gets no dungeon flag)."""
    a = _p_dungeon(L, raw, text, maps)
    tag = dungeon_tag(a["dungeon"])
    if tag is None:
        a["unknown"] = True
    else:
        a["dungeon"] = tag
    return a


def _p_enabledungeon(L, raw, text, maps):
    """`.enabledungeon [!]X`: the guide group turns dungeon X on (off with !) whatever the player selected."""
    a = _p_dungeon_tag(L, raw, text, maps)
    a["enabled"] = not a.pop("skip", False)
    return a


def _p_group(L, raw, text, maps):
    n = lua_int(_get(L, 0))
    return {"size": n} if n is not None else {}


def _p_aura(L, raw, text, maps):
    sid = lua_int(_get(L, 0))
    a = {"spell_id": sid, "unit": _get(L, 2) or "player"}
    if sid is None:
        a["error"] = "invalid aura id"
        return a
    reverse = False
    dur = _get(L, 1)
    if dur:
        m = re.search(r"(<?)" + SPACE + r"*([0-9]+)([+\-]?)", dur)
        if m:
            reverse = m.group(1) == "<" or m.group(3) == "-"
            if m.group(3) in ("+", "-"):
                a["stacks"] = int(m.group(2))
            else:
                a["seconds"] = float(m.group(2))
    if sid < 0:
        sid, reverse = -sid, not reverse
    a["spell_id"] = sid
    if reverse:
        a["reverse"] = True
    a["_k"], a["_e"] = "spell", sid
    return a


def _p_equip(L, raw, text, maps):
    slot, item = lua_int(_get(L, 0)), lua_int(_get(L, 1))
    a = {"slot": slot}
    if slot is None:
        a["error"] = "invalid slot id"
        return a
    if slot < 0:
        a["slot"], a["reverse"] = -slot, True
    if item is not None:
        a["item_id"] = item
        a["_k"], a["_e"] = "item", item
    return a


def _p_engrave(L, raw, text, maps):
    a = _p_equip(L, raw, text, maps)
    if "item_id" in a:
        a["spell_id"] = a.pop("item_id")
        a["_k"] = "spell"
    return a


def _p_skipgossip(L, raw, text, maps):
    vals = [lua_int(v) for v in L]
    a = {"args": vals}
    if len(vals) == 1:
        a["npc_or_option"] = vals[0]
    elif len(vals) > 1:
        a["npc_id"], a["options"] = vals[0], vals[1:]
        if vals[0]:
            a["_k"], a["_e"] = "npc", vals[0]
    return a


def _p_skipgossipid(L, raw, text, maps):
    vals = list(L)
    a = {}
    if vals and vals[0][:1] == "+":
        vals[0] = vals[0][1:]
        a["parent"] = True
    ids = [lua_int(v) for v in vals]
    a["gossip_ids"] = ids
    if ids and ids[0] is not None:
        a["_k"], a["_e"] = "gossip", ids[0]
    if not ids:
        a["error"] = "no gossip id"
    return a


def _p_gossipoption(L, raw, text, maps):
    gid = lua_int(_get(L, 0))
    a = {"gossip_id": gid, "_k": "gossip" if gid is not None else None, "_e": gid}
    if gid is None:
        a["error"] = "invalid gossipoption id"
    return a


def _p_gossip(L, raw, text, maps):
    npc = lua_int(_get(L, 0))
    return {"npc_id": npc, "length": lua_int(_get(L, 1)) or 0, "flags": lua_int(_get(L, 2)) or 0,
            "_k": "npc" if npc else None, "_e": npc}


def _p_addquestitem(L, raw, text, maps):
    item, qid = lua_int(_get(L, 0)), lua_int(_get(L, 1))
    return {"item_id": item, "quest_id": qid, "_k": "item", "_e": item, "_q": qid}


def _p_questitemcount(L, raw, text, maps):
    item, qty = lua_int(_get(L, 0)), lua_int(_get(L, 1))
    qs = [lua_int(v) or 0 for v in L[2:]]
    return {"item_id": item, "qty": qty, "quest_ids": qs, "_k": "item", "_e": item, "_n": qty,
            "_q": next((q for q in qs if q), None)}


def _p_retrieveitem(L, raw, text, maps):
    item, qty = lua_int(_get(L, 0)), lua_int(_get(L, 1))
    return {"item_id": item, "qty": qty, "_k": "item", "_e": item, "_n": qty}


def _p_reputation(L, raw, text, maps):
    fac = lua_int(_get(L, 0))
    a = {"faction_id": fac}
    s = (_get(L, 1) or "").replace(" ", "")
    m = re.search(r"(<?)" + SPACE + r"*([0-9A-Za-z]+)([+.\-]?[0-9]*)", s)
    op = ""
    if m:
        op = m.group(1)
        a["standing"] = m.group(2)
        rep = m.group(3)
    else:
        rep = ""
    val = _get(L, 2)
    if val is not None:
        m2 = re.search(r"(<?)" + SPACE + r"*(-?[0-9]+\.?[0-9]*)", val)
        if m2:
            op, rep = m2.group(1), m2.group(2)
    a["value"] = lua_num(val) if lua_num(val) is not None else (lua_num(rep) or 0)
    if op == "<":
        a["reverse"] = True
    if _get(L, 3) is not None:
        a["skipstep"] = L[3]
    if fac is None or "standing" not in a:
        a["error"] = "invalid faction/standing"
    a["_k"], a["_e"] = ("faction", fac) if fac is not None else (None, None)
    return a


def _p_collectcurrency(L, raw, text, maps):
    cid = lua_int(_get(L, 0))
    a = {"currency_id": cid}
    m = re.search(r"([<>]?)=?([0-9]+)", (_get(L, 1) or "").replace(" ", ""))
    a["qty"] = int(m.group(2)) if m else 1
    if m and m.group(1) == "<":
        a["reverse"] = True
    if _get(L, 2) is not None:
        a["silent"] = L[2]
    a["_k"], a["_e"], a["_n"] = "currency", cid, a["qty"]
    return a


def _p_emote(L, raw, text, maps):
    a = {"emote": (_get(L, 0) or "").upper()}
    npc = lua_int(_get(L, 1))
    if npc is not None:
        a["npc_id"], a["_k"], a["_e"] = npc, "npc", npc
    if _get(L, 2) is not None:
        a["callback"], a["events"] = L[2], L[3:]
    return a


def _p_openmap(L, raw, text, maps):
    z = _get(L, 0)
    a = {"zone": z, "ui_map": maps.ui_map(z)}
    if _get(L, 1) is not None:
        a["callback"], a["events"] = L[1], L[2:]
    return a


def _p_bronzetube(L, raw, text, maps):
    return {"reverse": True} if _get(L, 0) is not None else {}


def _p_convertquest(L, raw, text, maps):
    src, dst = lua_int(_get(L, 0)), lua_int(_get(L, 1))
    return {"src": src, "dst": dst, "_q": src}


def _p_mirrorquest(L, raw, text, maps):
    src = lua_int(_get(L, 0))
    return {"src": src, "dst": [lua_int(v) for v in L[1:] if lua_int(v) is not None], "_q": src}


def _p_petfamily(L, raw, text, maps):
    return {"families": list(L)}


def _p_skiponquest(L, raw, text, maps):
    qid = lua_int(_get(L, 0))
    a = {"quest_id": qid, "_q": qid}
    if _get(L, 1) is not None:
        a["label"] = L[1]
    return a


def _p_scenario(L, raw, text, maps):
    st = _get(L, 0) or ""
    a = {"stage": lua_int(st.lstrip("+")), "criteria": lua_int(_get(L, 1)), "obj_max": lua_int(_get(L, 2))}
    if st[:1] == "+":
        a["ignore_progress"] = True
    return a


_VERB_PARSERS = {
    "goto": _p_goto, "groundgoto": _p_goto, "flygoto": _p_goto,
    "waypoint": _p_waypoint, "questwaypoint": _p_questgoto, "questgoto": _p_questgoto,
    "pin": _p_pin, "ingamewaypoint": _p_pin, "treasure": _p_treasure, "rare": _p_rare,
    "line": _p_line, "loop": _p_loop,
    "accept": _p_accept, "turnin": _p_turnin, "complete": _p_complete,
    "daily": _p_quest_list, "dailyturnin": _p_quest_list, "acceptmultiple": _p_quest_list,
    "turninmultiple": _p_quest_list, "abandon": _p_abandon,
    "isQuestComplete": _p_is_quest_complete, "isQuestNotComplete": _p_is_quest_complete,
    "isOnQuest": _p_quest_list, "isNotOnQuest": _p_quest_list,
    "isQuestTurnedIn": _p_is_turned_in, "isQuestAvailable": _p_is_turned_in,
    "questcount": _p_questcount, "skipOnQuest": _p_skiponquest,
    "collect": _p_collect, "collectmultiple": _p_collectmultiple, "destroy": _p_item_first,
    "openitem": _p_item_first, "buy": _p_buy, "buyAll": _p_buy, "buyUntilBroke": _p_buy,
    "addquestitem": _p_addquestitem, "questitemcount": _p_questitemcount, "retrieveitem": _p_retrieveitem,
    "bankdeposit": _p_items, "bankwithdraw": _p_items,
    "xp": _p_xp, "maxlevel": _p_maxlevel, "skill": _p_skill, "maxskill": _p_skill,
    "vendor": _p_npc_id, "trainer": _p_npc_id, "stable": _p_npc_id, "tame": _p_npc_id,
    "vehicle": _p_npc_id, "exitvehicle": _p_npc_id,
    "money": _p_money, "train": _p_train, "istrained": _p_spell_list, "spellmissing": _p_spell_list,
    "subzone": _p_area, "subzoneskip": _p_area, "zone": _p_zone, "zoneskip": _p_zoneskip,
    "explore": _p_explore,
    "hs": _p_location, "hsbatching": _p_location, "home": _p_home, "bindlocation": _p_bindlocation,
    "fp": _p_fp, "fly": _p_location, "link": _p_link, "clicknext": _p_next, "next": _p_next,
    "target": _p_units, "mob": _p_units, "unitscan": _p_units,
    "cast": _p_cast, "use": _p_use_item, "usespell": _p_use_spell, "macro": _p_macro,
    "itemcount": _p_itemcount, "itemStat": _p_itemstat, "cooldown": _p_cooldown, "timer": _p_timer,
    "logout": _p_duration, "countdown": _p_duration,
    "dungeon": _p_dungeon_tag, "enabledungeon": _p_enabledungeon, "profession": _p_dungeon, "group": _p_group,
    "aura": _p_aura, "equip": _p_equip, "engrave": _p_engrave,
    "skipgossip": _p_skipgossip, "skipgossipid": _p_skipgossipid, "gossipoption": _p_gossipoption,
    "gossip": _p_gossip, "reputation": _p_reputation, "collectcurrency": _p_collectcurrency,
    "emote": _p_emote, "openmap": _p_openmap, "bronzetube": _p_bronzetube,
    "convertquest": _p_convertquest, "mirrorquest": _p_mirrorquest, "petfamily": _p_petfamily,
    "scenario": _p_scenario,
}

# every verb defined by the reference addon (functions.lua + DB/questDB.lua); others are RXP parse errors
KNOWN_VERBS = frozenset("""abandon accept acceptmap acceptmultiple achievement achievementComplete
achievementIncomplete achievementskip addquestitem addtoquestdb areapoiexists areapoiguide aura bankdeposit
bankwithdraw beta bindlocation blastedLands bombdispenser bronzetube buy buyAll buyUntilBroke cast celestial
choose chromietime clicknext collect collectcurrency collectmount collectmultiple collectpet collecttoy
complete convertquest cooldown countdown daily dailyhub dailyreset dailyturnin deathskip destroy
disablecheckbox disablequestautomation dmf dualspec dungeon emote engrave enterScenario equip exitvehicle
explore fly flyable flygoto fp getTBCchapters gossip gossipoption goto groundgoto group hastyhearth
hideifcomplete holiday home hs hsbatching ingamewaypoint ironchain isInScenario isNotOnQuest isOnQuest
isQuestAvailable isQuestComplete isQuestNotComplete isQuestOffered isQuestTurnedIn isWorldQuestAvailable
isWorldQuestUnavailable istrained itemStat itemcount klaxxi label landfall line link logout loop macro
maxlevel maxskill mirrorquest mob money mountcount multibox multiboxtext neutralzonefinished next niffelen
nodmf noflyable noop noskyriding openitem openmap petfamily pin profession pve pvp questcount questgoto
questitemcount questwaypoint rare reputation requires rescue retrieveitem scenario scrap setquestdb
setturninhs setturninroute show25quests showtotalxp showwhiledead singlebox singleboxtext skill skipOnQuest skipgossip
skipgossipid skipto skyriding solo spec spellmissing stable subzone subzoneskip tame target tbcWBF timer
totalbagslots tradeskill train trainer treasure turnin turninconfig turninmultiple unitscan use usespell
vale vehicle vendor waypoint wpbuff wpradius wptimer xp xpcheck xpto60 xpto60alliance xpto60hc xpto60horde
zone zoneskip""".split()) | {"enabledungeon"}   # newer than reference/rxp: the installed Forever addon has it

# verbs whose parse in functions.lua also sets a step attribute (addon.step.X = ...)
STEP_SIDE_EFFECTS = {"deathskip": ("softcore", True), "group": ("group", True), "solo": ("solo", True)}

PARSED_VERBS = tuple(sorted(_VERB_PARSERS))


def parse_args(verb, raw, text, maps):
    """One action's args: the verb's parser over the split arguments (a verb without one keeps them as "list"); "raw"
    is always kept."""
    L = _split_args(verb, raw)
    fn = _VERB_PARSERS.get(verb)
    if fn is not None:
        try:
            a = fn(L, raw, text, maps)
        except Exception as e:          # defensive: a malformed line never kills the guide
            a = {"error": "%s: %s" % (type(e).__name__, e)}
    else:
        a = {}
        if L:
            a["list"] = L
    out = {"raw": raw}
    out.update(a)
    return out
