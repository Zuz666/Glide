-- GlideLink state: what the companion needs, as a compact "k=v;k=v" payload.
-- Core keys go in every frame; one "page" (slower-changing data) rides along per frame.
-- Events (accept/turn-in/level/death/step...) are repeated in the last-3 ring e1..e3 so the
-- companion catches them even if it misses a frame.
local _, ns = ...

local Safe = ns.Safe
local floor = math.floor
local PAGE_MAX = 118           -- page text after "pg=<id>;" (longer pages are cut)
local LIST_PAGES_MAX = 20      -- a list spreads over at most <letter>0..<letter>19

-- ---------------------------------------------------------------------------------------------
-- payload text, shared with Gear.lua and Extras.lua
-- ---------------------------------------------------------------------------------------------
-- -> clean(s, max): tostring(s) with each character of `chars` (a Lua pattern set) replaced by `with`, cut to max
-- bytes. What must go depends on where the text sits: a k=v value can't hold ";=|", a list field not ":," either.
function ns.Cleaner(chars, with)
    return function(s, max)
        s = tostring(s or ""):gsub(chars, with)
        if max and #s > max then s = s:sub(1, max) end
        return s
    end
end
local clean = ns.Cleaner("[;=|\n\r]", ",")         -- a k=v value or a ring event argument
ns.Clean = clean

-- the companion's change check for a list: a 16-bit hash of its text
function ns.Hash(text)
    local h = 0
    for i = 1, #text do h = (h * 31 + text:byte(i)) % 65521 end
    return h
end

-- list entries joined by "," into chunks of at most `limit` bytes (one page each); never empty: no entries, one
-- empty chunk. An entry longer than the limit gets a chunk of its own.
function ns.ChunkList(entries, limit)
    local chunks, cur = {}, ""
    for _, e in ipairs(entries) do
        if cur ~= "" and #cur + #e + 1 > limit then chunks[#chunks + 1] = cur; cur = "" end
        cur = (cur == "" and e) or (cur .. "," .. e)
    end
    if cur ~= "" or #chunks == 0 then chunks[#chunks + 1] = cur end
    return chunks
end

-- a list's pages <letter>0..<letter>n in `builders`, page i sending prefix .. chunk i; the old ones go first
function ns.ListPages(builders, letter, chunks, prefix)
    for i = 0, LIST_PAGES_MAX - 1 do builders[letter .. i] = nil end
    for i, c in ipairs(chunks or {}) do
        local text = prefix .. c
        builders[letter .. (i - 1)] = function() return text end
    end
end

-- ---------------------------------------------------------------------------------------------
-- events
-- ---------------------------------------------------------------------------------------------
local evId, ring = 0, {}
function ns.Event(kind, arg)
    evId = evId + 1
    table.insert(ring, 1, ("%d:%s:%s"):format(evId, kind, clean(arg, 40)))
    ring[4] = nil
end

local kills, lastKillXP = 0, 0
ns.On("QUEST_ACCEPTED", function(a, b) ns.Event("acc", tonumber(b or a)) end)
ns.On("QUEST_TURNED_IN", function(q, xp) ns.Event("tin", ("%s,%s"):format(q, Safe(xp) or 0)) end)
ns.On("QUEST_REMOVED", function(q) ns.Event("rem", q) end)
ns.On("PLAYER_LEVEL_UP", function(l) ns.Event("lvl", Safe(l)) end)
ns.On("PLAYER_DEAD", function() ns.Event("die", "") end)
ns.On("PLAYER_ALIVE", function() ns.Event("alive", "") end)
ns.On("PLAYER_UNGHOST", function() ns.Event("unghost", "") end)
ns.On("HEARTHSTONE_BOUND", function() ns.Event("bind", Safe(GetBindLocation())) end)
ns.On("ZONE_CHANGED_NEW_AREA", function() ns.Event("zone", Safe(GetZoneText())) end)
ns.On("CHAT_MSG_COMBAT_XP_GAIN", function(text)
    text = Safe(text)
    local xp = text and tonumber(text:match("you gain (%d+) experience"))
    if xp then kills = kills + 1; lastKillXP = xp end
end)
ns.On("UNIT_SPELLCAST_SUCCEEDED", function(unit, _, spell)
    if unit ~= "player" then return end
    spell = Safe(spell)
    if spell == ns.HEARTHSTONE_SPELL then ns.Event("hs", "") end
    -- own buff (re)cast: lets the companion refresh a buff while the aura list is frozen in combat
    if spell and ns.knownBuffIds and ns.knownBuffIds[spell] then ns.Event("cst", spell) end
end)
ns.On("PLAYER_CONTROL_LOST", function() if UnitOnTaxi("player") then ns.Event("taxi", "") end end)
ns.On("LEARNED_SPELL_IN_SKILL_LINE", function(spell) ns.Event("learn", Safe(spell)) end)

-- ---------------------------------------------------------------------------------------------
-- core
-- ---------------------------------------------------------------------------------------------
local function pct(cur, max)
    cur, max = Safe(cur), Safe(max)
    if not cur or not max or max == 0 then return -1 end
    return floor(cur / max * 100 + 0.5)
end

local function freeBagSlots()
    local n = 0
    for bag = 0, 4 do
        local free = C_Container.GetContainerNumFreeSlots(bag)
        n = n + (Safe(free) or 0)
    end
    return n
end

local function minDurability()
    local lo = 100
    for slot = 1, 18 do
        local cur, max = GetInventoryItemDurability(slot)
        if cur and max and max > 0 then lo = math.min(lo, floor(cur / max * 100)) end
    end
    return lo
end

local function hearthCD()
    local start, dur
    if C_Container and C_Container.GetItemCooldown then
        start, dur = C_Container.GetItemCooldown(ns.HEARTHSTONE_ITEM)
    end
    if not start or start == 0 then return 0 end
    return math.max(0, floor(start + dur - GetTime()))
end

function ns.Core()
    local t = {}
    local function put(k, v) t[#t + 1] = k .. "=" .. tostring(v == nil and "" or v) end
    put("gv", ns.VERSION)
    put("gk", ns.gaugeMask or 0)
    put("cf", ns.CoachMask and ns.CoachMask() or 255)
    put("t", GetServerTime() % 1000000)
    put("l", Safe(UnitLevel("player")))
    put("x", Safe(UnitXP("player")))
    put("X", Safe(UnitXPMax("player")))
    put("r", Safe(GetXPExhaustion()) or 0)
    put("h", pct(UnitHealth("player"), UnitHealthMax("player")))
    put("m", pct(UnitPower("player"), UnitPowerMax("player")))
    local okp, _, ptoken = pcall(UnitPowerType, "player")
    put("pt", okp and Safe(ptoken) or "")
    local okc, _, classToken = pcall(UnitClass, "player")
    put("cl", okc and Safe(classToken) or "")
    put("c", UnitAffectingCombat("player") and 1 or 0)
    put("d", UnitIsGhost("player") and 2 or (UnitIsDeadOrGhost("player") and 1 or 0))
    local map, x, y = ns.PlayerMapPosition()
    put("mp", map)
    if map then
        put("px", x and floor(x * 10000) or "")
        put("py", y and floor(y * 10000) or "")
    end
    local f = Safe(GetPlayerFacing and GetPlayerFacing())
    put("f", f and floor(f * 100) or "")
    put("sp", floor((Safe(GetUnitSpeed("player")) or 0) * 10))
    put("g", GetMoney())
    put("b", freeBagSlots())
    put("du", minDurability())
    put("hs", hearthCD())
    put("rs", IsResting() and 1 or 0)
    put("mo", IsMounted() and 1 or 0)
    put("tx", UnitOnTaxi("player") and 1 or 0)
    put("kc", kills)
    put("kx", lastKillXP)
    if UnitExists("target") then
        local guid = Safe(UnitGUID("target"))
        local kind, id
        if type(guid) == "string" then
            local parts = { strsplit("-", guid) }
            kind, id = parts[1], parts[6]
        end
        put("tg", (kind == "Creature" or kind == "Vehicle") and id or (kind == "Player" and "P" or ""))
        put("tl", Safe(UnitLevel("target")))
        put("th", pct(UnitHealth("target"), UnitHealthMax("target")))
        put("tr", Safe(UnitReaction("target", "player")) or "")
        put("td", UnitIsDead("target") and 1 or 0)
    end
    local step = ns.RXPStep()
    put("st", step or "")
    put("qn", ns.questPages or 0)
    put("qh", ns.questHash or 0)
    put("an", ns.buffPages or 0)
    put("ah", ns.buffHash or 0)
    put("ak", ns.buffKnown == false and 1 or 0)
    put("sw", ns.skillWeapons or ""); put("sa", ns.skillArmor or "")
    local g = ns.gear
    if g then
        put("wn", #g.W); put("wh", g.Wh or 0)
        put("in", #g.I); put("ih", g.Ih or 0)
        put("rn", #g.R); put("rh", g.Rh or 0); put("rq", g.rq or 0)
    end
    if ns.CoreExtras then ns.Try("core extras", ns.CoreExtras, put) end
    if ns.errors > 0 then put("er", ns.errors) end
    for i = 1, 3 do if ring[i] then put("e" .. i, ring[i]) end end
    return table.concat(t, ";")
end

-- ---------------------------------------------------------------------------------------------
-- pages
-- ---------------------------------------------------------------------------------------------
local function stepWords(text)
    -- Strip colors before escaping delimiters or cutting bytes; escaped pipes remain literal text.
    text = tostring(text or ""):gsub("||", "\001")
    text = text:gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|cRXP_[A-Z]+_", ""):gsub("|cn[%w_]+:", "")
    return (text:gsub("|r", ""):gsub("\001", "|"))
end

local function stepSummary(step, index)
    if not step then return nil end
    local els, text = {}, nil
    for _, el in ipairs(step.elements or {}) do
        local tag = el.tag or ""
        if tag == "goto" or el.arrow then
            if el.x and el.y then
                els[#els + 1] = ("go:%s:%.1f:%.1f"):format(el.zone or "", el.x, el.y)
            end
        elseif el.questId then
            els[#els + 1] = ("%s:%s%s"):format(tag, el.questId, el.completed and "!" or "")
        elseif tag ~= "" and #els < 8 then
            els[#els + 1] = tag
        end
        if not text and el.text and el.text ~= "" and el.questId then text = el.text end
    end
    if not text then
        for _, el in ipairs(step.elements or {}) do
            if el.text and el.text ~= "" and el.tag ~= "goto" then text = el.text; break end
        end
    end
    -- the text goes last and gets what room the page has left (a long element list leaves less than 60)
    local s = ("i=%d;el=%s;tx="):format(index, clean(table.concat(els, ","), 70))
    return s .. clean(stepWords(text), math.min(60, PAGE_MAX - #s))
end

local builders = {}
local function page(id, fn) builders[id] = fn end
ns.pageHooks = ns.pageHooks or {}     -- fn(builders): other modules (Extras.lua) add or clear their own pages

page("B", function()
    local _, class = UnitClass("player")
    local _, race = UnitRace("player")
    return ("cl=%s;ra=%s;fa=%s;nm=%s;bd=%s"):format(clean(Safe(class)), clean(Safe(race)),
        clean(Safe(UnitFactionGroup("player"))), clean(Safe(UnitName("player"))), clean(Safe(GetBindLocation()), 30))
end)
page("E", function()                          -- the last error, while there is one (core er counts them)
    if ns.errors > 0 then return "ex=" .. clean(ns.lastError, PAGE_MAX - 3) end
end)
page("K", function()                          -- weapon/defense skill ranks (when the client has skill lines)
    return "sk=" .. (ns.skillRanks or "")
end)
-- map size in yards (for distances): world positions of the map's two corners
local sizeCache = {}
local function mapSize(map)
    if not map then return nil end
    if sizeCache[map] == nil then
        sizeCache[map] = false
        local ok, w, h = pcall(function()
            local _, a = C_Map.GetWorldPosFromMapPos(map, CreateVector2D(0, 0))
            local _, b = C_Map.GetWorldPosFromMapPos(map, CreateVector2D(1, 1))
            if not a or not b then return nil end
            local ax, ay = a:GetXY()
            local bx, by = b:GetXY()
            return math.abs(ay - by), math.abs(ax - bx)   -- map x runs along world -Y, map y along -X
        end)
        if ok and w and h and w > 0 and h > 0 then sizeCache[map] = { w, h } end
    end
    return sizeCache[map] or nil
end

page("Z", function()
    local map = Safe(C_Map.GetBestMapForUnit("player"))
    local sz = mapSize(map)
    return ("zn=%s;sz=%s;mm=%s;mw=%s;mh=%s"):format(clean(Safe(GetZoneText()), 40), clean(Safe(GetSubZoneText()), 40),
        map or "", sz and floor(sz[1]) or "", sz and floor(sz[2]) or "")
end)
page("T", function()
    if not UnitExists("target") then return "tn=" end
    return "tn=" .. clean(Safe(UnitName("target")), 40) .. ";tc=" .. clean(Safe(UnitClassification("target")), 12)
end)
page("G", function()
    local _, guide = ns.RXPStep()
    if not guide then return "gn=" end
    local s = ("gn=%s;gg=%s;gx="):format(clean(guide.name, 40), clean(guide.group, 40))
    return s .. clean(guide.next, math.min(30, PAGE_MAX - #s))      -- the next guide gets the room that's left
end)
for k = 0, 2 do
    page("S" .. k, function()
        local idx, guide = ns.RXPStep()
        if not idx or not guide or not guide.steps then return "i=" end
        -- skip steps that don't apply to this character (RXP leaves them out of `steps` already)
        local step = guide.steps[idx + k]
        return stepSummary(step, idx + k) or "i="
    end)
end

-- quest log, chunked into pages Q0..Qn
local questChunks = {}
local function buildQuests()
    local entries = {}
    for qid in ns.QuestLogQuests() do
        local objs = {}
        for _, o in ipairs(C_QuestLog.GetQuestObjectives(qid) or {}) do
            objs[#objs + 1] = ("%s/%s"):format(Safe(o.numFulfilled) or 0, Safe(o.numRequired) or 0)
        end
        local done = C_QuestLog.IsComplete and C_QuestLog.IsComplete(qid) and 1 or 0
        entries[#entries + 1] = ("%d:%d:%s"):format(qid, done, table.concat(objs, "/"))
    end
    questChunks = ns.ChunkList(entries, PAGE_MAX - 4)
    ns.questHash, ns.questPages = ns.Hash(table.concat(questChunks, ",")), #questChunks
end

-- buffs on the player (helpful auras + weapon enchants), chunked into pages A0..An.
-- Entry: spellId:expiry:duration:flags:name. expiry is server time (mod 1e6, rounded to 2 s) so the
-- page only changes when a buff is (re)applied, not every second. flags: m = cast by me.
-- In combat Forever may hide aura data (secret values); then the last list is kept and "ak=1" says so.
local buffChunks = {}

-- Buffs. What Forever does (from live logs): in combat GetAuraDataByIndex returns nothing at all, and for
-- a moment after combat it still does. So the list is only published from out-of-combat reads that agree
-- twice in a row; otherwise the last good list stays and ak=1 tells the companion it is stale. Own buff
-- casts are sent as events (they do come through in combat) so the companion can refresh a buff itself.
ns.knownBuffIds = ns.knownBuffIds or {}           -- spellId -> true, buffs seen on a good read
local timeOffset                                   -- server time - GetTime(), fixed so expiries don't flicker
local function serverOffset(now, st)
    local o = st - now
    if not timeOffset or math.abs(o - timeOffset) > 2 then timeOffset = o end
    return timeOffset
end
local function canCast(id)
    if C_SpellBook and C_SpellBook.IsSpellKnown then
        local ok, k = pcall(C_SpellBook.IsSpellKnown, id)
        if ok and k ~= nil then return Safe(k) and true or false end
    end
    if IsPlayerSpell then
        local ok, k = pcall(IsPlayerSpell, id)
        if ok and k ~= nil then return Safe(k) and true or false end
    end
    return nil
end
local function auraEntry(a, off)
    local name, id = Safe(a.name), Safe(a.spellId)
    local exp, dur = Safe(a.expirationTime), Safe(a.duration)
    if not name or not id or (a.expirationTime ~= nil and exp == nil) then return nil end
    local expiry = 0
    if exp and exp > 0 then expiry = floor(exp + off + 0.5) % 1000000 end
    local mine = Safe(a.isFromPlayerOrPlayerPet) and "m" or ""
    local k = canCast(id)
    if k == nil then k = mine == "m" end          -- no spellbook API: assume own casts are castable
    if k then mine = mine .. "k" end
    return ("%d:%d:%d:%s:%s"):format(id, expiry, floor(dur or 0), mine, (clean(name, 22):gsub("[:,]", " "))), id
end

local lastRead
local function readAuras()
    local now, st = GetTime(), GetServerTime()
    local off = serverOffset(now, st)
    local entries, ids = {}, {}
    for i = 1, 40 do
        local ok, a = pcall(C_UnitAuras.GetAuraDataByIndex, "player", i, "HELPFUL")
        if not ok then return nil end
        if a == nil then break end
        local ok2, e, id = pcall(auraEntry, a, off)
        if not (ok2 and e) then return nil end
        entries[#entries + 1] = e
        ids[#ids + 1] = id
    end
    if GetWeaponEnchantInfo then
        local ok, mh, mhExp, mhCh, _, oh, ohExp, ohCh = pcall(GetWeaponEnchantInfo)
        if ok and Safe(mh) and Safe(mhExp) then
            entries[#entries + 1] = ("mh:%d:0:mk:Main-hand enchant%s"):format(floor(st + mhExp / 1000 + 0.5) % 1000000,
                (Safe(mhCh) or 0) > 0 and (" x" .. mhCh) or "")
        end
        if ok and Safe(oh) and Safe(ohExp) then
            entries[#entries + 1] = ("oh:%d:0:mk:Off-hand enchant%s"):format(floor(st + ohExp / 1000 + 0.5) % 1000000,
                (Safe(ohCh) or 0) > 0 and (" x" .. ohCh) or "")
        end
    end
    return entries, ids
end

local function buildBuffs()
    if not (C_UnitAuras and C_UnitAuras.GetAuraDataByIndex) then return end
    if UnitAffectingCombat("player") then
        ns.buffKnown, ns.buffSource, lastRead = false, "frozen (combat)", nil
        return
    end
    local entries, ids = readAuras()
    if not entries then ns.buffKnown, ns.buffSource, lastRead = false, "unreadable", nil; return end
    -- weapon enchant times tick; compare the aura part only when checking two reads agree
    local key = table.concat(entries, ","):gsub("[mo]h:%d+:", "")
    if key ~= lastRead then
        lastRead = key
        ns.buffKnown, ns.buffSource = false, "settling"
        return
    end
    ns.buffKnown, ns.buffSource = true, "list"
    for _, id in ipairs(ids) do ns.knownBuffIds[id] = true end
    buffChunks = ns.ChunkList(entries, PAGE_MAX - 6)
    ns.buffHash, ns.buffPages = ns.Hash(table.concat(buffChunks, ",")), #buffChunks
end

local cache, dirty, rr = {}, {}, 0
function ns.RefreshPages()
    ns.Try("quests", buildQuests)
    if ns.BuildSkills then ns.Try("skills", ns.BuildSkills) end
    local okB, errB = ns.Try("buffs", buildBuffs)
    if not okB then ns.buffKnown = false; ns.buffError = errB end
    local g = ns.gear or {}
    for _, kind in ipairs({ "W", "I", "R" }) do ns.ListPages(builders, kind, g[kind], "g=") end   -- Gear.lua
    ns.ListPages(builders, "Q", questChunks, "q=")
    ns.ListPages(builders, "A", buffChunks, "a=")
    for _, hook in ipairs(ns.pageHooks) do ns.Try("pages", hook, builders) end
    for id, fn in pairs(builders) do
        local ok, s = ns.Try("page " .. id, fn)
        if ok and s then
            s = "pg=" .. id .. ";" .. s
            if #s > PAGE_MAX + 6 then s = s:sub(1, PAGE_MAX + 6) end
            if cache[id] ~= s then cache[id] = s; dirty[id] = true end
        end
    end
    for id in pairs(cache) do if not builders[id] then cache[id] = nil; dirty[id] = nil end end
end

-- the pages are rebuilt once a second while the strip streams (Strip.lua only draws what NextPage hands it)
ns.On("PLAYER_LOGIN", function()
    C_Timer.NewTicker(1, function()
        if ns.StripOn and not ns.StripOn() then return end
        ns.RefreshPages()
    end)
end)

-- next page to send: changed pages first, then round-robin. `room` (optional) = bytes left after the
-- core: a page that doesn't fit whole is skipped (and stays queued) rather than cut by the encoder,
-- since a cut page would read as complete data. Long ring events (e.g. mark notes) shrink the room.
function ns.NextPage(room)
    room = room or math.huge
    local function fits(id) return cache[id] and #cache[id] <= room end
    for _, id in ipairs({ "R0", "R1", "S0", "A0", "I0", "G", "Z", "T", "A1", "I1", "S1", "S2", "B" }) do
        if dirty[id] and fits(id) then dirty[id] = nil; return cache[id] end
    end
    for id in pairs(dirty) do
        if fits(id) then dirty[id] = nil; return cache[id] end
    end
    local ids = {}
    for id in pairs(cache) do ids[#ids + 1] = id end
    table.sort(ids)
    for _ = 1, #ids do
        rr = rr % #ids + 1
        if fits(ids[rr]) then return cache[ids[rr]] end
    end
    return ""
end

-- /glide buffs: what the game hands an addon for each of your buffs (secret = hidden by the game)
ns.Command("buffs", "buffs - the buffs the game shows the addon, and the buff list the strip sends", function()
    local function show(v) if v == nil then return "nil" end if ns.IsSecret(v) then return "SECRET" end return tostring(v) end
    local n = 0
    for i = 1, 40 do
        local ok, a = pcall(C_UnitAuras.GetAuraDataByIndex, "player", i, "HELPFUL")
        if not ok then print("GetAuraDataByIndex error:", a); break end
        if not a then break end
        n = i
        print(("%d: %s id=%s left=%s mine=%s"):format(i, show(a.name), show(a.spellId),
            a.expirationTime and not ns.IsSecret(a.expirationTime) and math.floor(a.expirationTime - GetTime()) or show(a.expirationTime),
            show(a.isFromPlayerOrPlayerPet)))
    end
    ns.Print(("%d buffs read; pages=%s hash=%s known=%s source=%s"):format(n, tostring(ns.buffPages),
        tostring(ns.buffHash), tostring(ns.buffKnown), tostring(ns.buffSource)))
    if ns.buffError then print("buff error:", ns.buffError) end
end)
