#!/usr/bin/env python3
"""Content identities at the Lua addon seam; independent hashlib wire-format oracle.

The deterministic native fixture models ParseGuide's NONEMPTY physical line stepId,
conditional group preprocessing, class/action filtering, and ProcessGuideTable's
scalar-guide/all-step-field copying. It never needs an installed addon or game.
"""
import hashlib
from pathlib import Path
import sys
import wowmock

try:
    from lupa import lua51
except ImportError:
    print("SKIP: needs lupa")
    sys.exit(77)

ROOT = Path(__file__).resolve().parents[1] / "addon" / "Glide"


def md5(value):
    return hashlib.md5(value, usedforsecurity=False).hexdigest()


def fingerprint(block):
    """The oracle takes an explicit canonical source block, never a whole guide."""
    return "s1:" + md5(("Glide:RXP:step:1\n" + block).encode())


NATIVE = r'''
RXP = {guides = {}, embeddedGuides = {}, db = {profile = {guides = {}}}, class = "MAGE", hardcore = false, calls = 0}
function RXP.applies(condition)
    for term in condition:gmatch("[^/]+") do
        local valid = true
        for token in term:gmatch("!?[%w]+") do
            local negate = token:sub(1,1) == "!"
            if negate then token = token:sub(2) end
            local matched = token:upper() == RXP.class or token == "Alliance" or token == "Human"
            if matched == negate then valid = false end
        end
        if valid then return true end
    end
    return false
end
function RXP.BuildGuideKey(g) return g.group.."|"..(g.subgroup or "").."|"..g.name end
-- Pinned GuideLoader.lua:114-117,1322-1387; only exercised native override branches.
RXP.game = "CLASSIC"
RXP.classicPrefix = "C-"
RXP.locale = {Get = function(value) return value end}
RXP.groupAlias = {}
function RXP.affix(smin, smax)
    if smax:len() == 1 then smax = "0"..smax end
    return "0"..smin.."-"..smax
end
function RXP.GroupOverride(g)
    local group, subgroup, swap = g.group, g.subgroup
    if RXP.game == "CLASSIC" or g.classic then
        local faction = not g.groupid and group:match("RestedXP ([AH][lo][lr][id][ea]%w*)")
        if faction == "Alliance" or faction == "Horde" or
            g.groupid == "RXP-SRGCE-A1" or g.groupid == "RXP-SRGCE-H1" then
            subgroup, swap = subgroup or group, true
        end
    elseif RXP.game == "TBC" then
        if not g.classic then
            local range = subgroup and subgroup:match("^RestedXP Survival.-( %d+%-%d+)$")
            if range then subgroup, swap = "TBC Survival Guide"..range, true end
            local suffix = group:match("^RestedXP Survival Guide(.*)")
            if suffix then group, swap = "RXP TBC Survival Guide"..suffix, true end
        end
    end
    g.subgroup = subgroup
    if swap then RXP.groupAlias[g.group], g.group = group, group end
    return group
end
function RXP.ParseGuide(first, text, defaultFor, embedded, group)
    RXP.calls = RXP.calls + 1
    if not text then
        text = first
        if embedded or not first:match("^%-%-(%d+)") then
            text = text:gsub("%-%-[^\r\n]*[\r\n]+", "\n")
        end
        local currentGroup = group
        if not group then
            text = text:gsub("(#group[ \t]*)([^\r\n]-)[ \t]*<<[ \t]*([^\r\n]-)[ \t]*[\r\n]+",
                function(prefix, value, condition)
                    if RXP.applies(condition) then currentGroup = value; return prefix..value.."\n" end
                    return "\n"
                end)
        end
        first = currentGroup or text:match("#group%s+([^\r\n]+)")
    else
        text = text:gsub("%-%-[^\r\n]*[\r\n]", "\n")
    end
    local g = {group = first, guideId = 100000, steps = {}}
    local number, step, skip = 0
    for line in text:gmatch("[^\n\r]+") do
        number = number + 1
        line = line:gsub("^%s+", ""):gsub("%s+$", "")
        local condition = line:match("<<%s*(.+)")
        local applicable = not condition or RXP.applies(condition)
        if line:sub(1,4) == "step" then
            skip = not applicable
            if not skip then
                step = {stepId = g.guideId + number, elements = {}}
                g.steps[#g.steps + 1] = step
            end
        elseif applicable and not skip then
            local code = line:gsub("%s*<<.*$", "")
            local tag, value = code:match("^#(%S+)%s*(.*)")
            if not step then
                if tag and not g[tag] then g[tag] = value end
            elseif tag then
                step[tag] = value
            elseif code:match("^[.*+]" ) then
                step.elements[#step.elements + 1] = {text = code}
            end
        end
    end
    local parsedGroup = RXP.GroupOverride(g)
    if RXP.game == "TBC" and g.classic and not g.wotlk and not parsedGroup:find("TBC") and parsedGroup:sub(1,1) ~= "+" then
        local range = g.subgroup and g.subgroup:match("( %d+%-%d+)$")
        if range then g.subgroup = "Classic"..range end
        if parsedGroup:sub(1,8) == "RestedXP" then g.group = RXP.classicPrefix..parsedGroup end
    end
    g.name = g.name:gsub("^(%d)-(%d%d?)", RXP.affix)
    g.key = RXP.BuildGuideKey(g)
    return g, nil, "metadata", nil, "fifth"
end
local function copyTable(source)
    local copied = {}
    for key, value in pairs(source) do
        copied[key] = type(value) == "table" and copyTable(value) or value
    end
    return copied
end
function RXP.ProcessGuideTable(guide)
    local current = {steps = {}}
    for key, value in pairs(guide) do
        if type(value) ~= "table" then
            current[key] = value
        elseif key ~= "steps" and key ~= "tips" and key ~= "questCacheIndex" then
            current[key] = copyTable(value)
        end
    end
    local function add(source, isInclude)
        for _, step in ipairs(source.steps) do
            if not step.hardcore or RXP.hardcore then
                if not (step.include and #step.elements == 0 and not step.label and not step.requires) then
                    local copied = step
                    if isInclude then
                        copied = {}
                        for key, value in pairs(step) do copied[key] = value end
                    end
                    if not step.tip then current.steps[#current.steps + 1] = copied end
                end
                if step.include then
                    local included = RXP.guides[step.include]
                    if included then add(included, true) end
                end
            end
        end
    end
    add(guide, false)
    return current, nil, "process metadata"
end
-- Pinned GuideWindow.lua:3133-3153, including AceSerializer's supported-value boundary.
RXP.comms = {}
function RXP.comms:Serialize(payload)
    local function supported(value)
        local kind = type(value)
        assert(kind == "table" or kind == "string" or kind == "number" or kind == "boolean" or kind == "nil",
            "AceSerializer cannot serialize "..kind)
        if kind == "table" then
            for key, item in pairs(value) do supported(key); supported(item) end
        end
    end
    supported(payload)
    self.lastSerialized = payload
    return "serialized active steps"
end
RXP.v2 = {}
function RXP.v2:GetActiveStepText(step)
    local text = {}
    for _, element in ipairs(step.elements or {}) do text[#text + 1] = element.text end
    return table.concat(text, "\n")
end
function RXP.v2:EncodePlayerActiveSteps(payload)
    local trimmedPayload = {}
    for _, step in ipairs(payload) do
        if not step.hidewindow and not step.hidetip then
            local trimmedStep = {}
            for key, value in pairs(step) do
                if type(value) ~= "table" and key ~= "hiddentext" then trimmedStep[key] = value end
            end
            trimmedStep.text = self:GetActiveStepText(step)
            trimmedStep.active = true
            trimmedPayload[#trimmedPayload + 1] = trimmedStep
        end
    end
    -- Deflate/chat encoding operate only on the resulting string, not step values.
    return RXP.comms:Serialize(trimmedPayload)
end
function register(text, legacy)
    local g
    if legacy then
        g = RXP.ParseGuide(legacy, text, nil, true)
        RXP.embeddedGuides[#RXP.embeddedGuides + 1] = {groupOrContent = legacy, text = text}
    else
        local group
        for line in text:gmatch("[^\r\n]+") do
            if line:match("^%s*step") then break end
            line = line:gsub("%-%-.*$", "")
            local condition = line:match("<<%s*(.+)")
            if not condition or RXP.applies(condition) then
                line = line:gsub("%s*<<.*$", "")
                group = group or line:match("^%s*#group%s+(.-)%s*$")
            end
        end
        g = RXP.ParseGuide(text, nil, nil, true, group)
        RXP.embeddedGuides[#RXP.embeddedGuides + 1] = {groupOrContent = text}
    end
    RXP.guides[g.key] = g
    return g
end
'''


def runtime(before=None, *, late=False):
    lua = lua51.LuaRuntime(unpack_returned_tuples=True)
    ns = lua.table()
    lua.globals().ns = ns
    wowmock.install_bit(lua)
    lua.execute("callbacks = {}; ns.rec = {On = function(event, callback) callbacks[event] = callback end}")
    lua.execute(NATIVE)
    loader = lua.eval("function(src, ns) local f = assert(loadstring(src)); return f('Glide', ns) end")
    loader((ROOT / "MD5.lua").read_text(encoding="utf-8"), ns)
    if before:
        before(lua)
    if late:
        lua.execute("savedRXP = RXP; RXP = nil")
    loader((ROOT / "StepIdentity.lua").read_text(encoding="utf-8"), ns)
    if late:
        lua.execute("RXP = savedRXP; callbacks.ADDON_LOADED()")
    return lua, ns


def test_md5():
    lua, ns = runtime()
    vectors = ["", "a", "abc", "message digest", "é 中文", "\x00\x01abc"]
    vectors += ["x" * length for length in (55, 56, 63, 64, 65, 127, 128, 129, 4096)]
    for text in vectors:
        expected = md5(text.encode())
        assert ns.MD5(text) == expected
        append = ns.MD5(None)
        for pos in range(0, len(text), 7):
            append(text[pos:pos + 7])
        assert append() == expected
        assert append() == expected, "finalization is stable"
        lua.globals().append = append
        assert not lua.eval("pcall(function() append('late') end)")[0]
    # Two incremental instances can be interleaved despite the reused block scratch array.
    first, second = ns.MD5(None), ns.MD5(None)
    first("a" * 80)
    second("b" * 130)
    first("c" * 70)
    assert first() == md5(b"a" * 80 + b"c" * 70)
    assert second() == md5(b"b" * 130)


SIMPLE_BLOCK = "step\n.goto Zone,1,2 >> Walk all the way\n* Separate note\n"
SIMPLE = "#group Route\n#name Same\n#version 1\n" + SIMPLE_BLOCK


def test_startup_cache_and_no_rehash():
    def before(lua):
        lua.globals().raw = SIMPLE
        lua.execute("native = register(raw); process = RXP.ProcessGuideTable")
        lua.execute("hashes = 0; md5 = ns.MD5; ns.MD5 = function(bytes) hashes = hashes + 1; return md5(bytes) end")

    lua, ns = runtime(before)
    lua.execute("assert(process == RXP.ProcessGuideTable); RXP.embeddedGuides = {}; active = RXP.ProcessGuideTable(native)")
    step = lua.globals().active.steps[1]
    assert lua.globals().hashes == 0, "capturing source must not hash every guide or block"
    expected = (1, fingerprint(SIMPLE_BLOCK))
    assert ns.StepIdentity(step) == expected
    assert lua.globals().hashes == 1
    assert lua.globals().RXP.calls == 1, "identity never reparses a registered guide"
    lua.execute("ns.MD5 = function() error('unexpected per-frame hashing') end")
    for _ in range(20):
        assert ns.StepIdentity(step) == expected
    assert lua.globals().hashes == 1
    assert ns.StepIdentity(None) is None
    assert ns.StepIdentity(lua.table_from({"stepId": step.stepId})) is None


def test_unrelated_edits_and_original_numbering():
    block = "step\n.goto Zone,2,2 >> Current\n"
    raw = "#group Route\n#name Same\nstep\n.goto Zone,1,1 >> Before\n" + block + "step\n.goto Zone,3,3 >> After\n"
    lua, ns = runtime()
    parsed = lua.globals().RXP.ParseGuide(raw)
    assert parsed[1:] == (None, "metadata", None, "fifth"), "all nil-separated native returns survive"
    native = parsed[0]
    processed = lua.globals().RXP.ProcessGuideTable(native)
    assert processed[1:] == (None, "process metadata")
    lua.globals().native, lua.globals().active = native, processed[0]
    lua.execute("assert(active.steps[2] == native.steps[2], 'ordinary native navigation retains its current pointer')")
    expected = (2, fingerprint(block))
    assert ns.StepIdentity(processed[0].steps[2]) == expected
    edited = raw.replace("#name Same", "#name Unrelated").replace(">> Before", ">> Before changed").replace(
        ">> After", ">> After changed",
    )
    replacement = lua.globals().RXP.ParseGuide(edited)[0]
    assert ns.StepIdentity(replacement.steps[2]) == expected
    changed = lua.globals().RXP.ParseGuide(raw.replace(">> Current", ">> Revised"))[0]
    assert ns.StepIdentity(changed.steps[2]) == (2, fingerprint("step\n.goto Zone,2,2 >> Revised\n"))
    assert ns.StepIdentity(native.steps[2]) == expected, "another parse cannot rewrite the current pointer's provenance"
    inserted = lua.globals().RXP.ParseGuide(raw.replace(block, "step\n.goto Zone,0,0 >> Inserted\n" + block))[0]
    assert ns.StepIdentity(inserted.steps[3]) == (3, fingerprint(block))


def test_canonical_and_native_line_mapping():
    dirty = "--123/account\r\n\t#group Route\r\n#name Same\r \t\rstep -- explanation\r\t.goto Zone,1,2   >> Walk all the way\t\r* Separate note\r"
    lua, ns = runtime()
    native = lua.globals().RXP.ParseGuide(dirty)[0]
    assert native.steps[1].stepId == 100005, "cache marker and whitespace-only lines count natively"
    assert ns.StepIdentity(native.steps[1]) == (1, fingerprint(SIMPLE_BLOCK))
    legacy = "#name Legacy\nstep\n.goto Zone,1,2 >> Élan 中文\n"
    native = lua.globals().RXP.ParseGuide("Old Route", legacy)[0]
    assert native.steps[1].stepId == 100002, "legacy group is not a source line"
    assert ns.StepIdentity(native.steps[1]) == (1, fingerprint("step\n.goto Zone,1,2 >> Élan 中文\n"))
    whitespace = "#group Route\n#name Whitespace\n\vstep\f\n\t.goto\tZone,1,2  >>\tÉlan 中文\f\n"
    native = lua.globals().RXP.ParseGuide(whitespace)[0]
    assert ns.StepIdentity(native.steps[1]) == (1, fingerprint("step\n.goto Zone,1,2 >> Élan 中文\n"))


def test_class_action_and_settings_filtering():
    block = "step\n.goto Zone,2,2 >> Mage << Mage\n.goto Zone,3,3 >> Warrior << Warrior\n"
    raw = "#group Route\n#name Branches\nstep << Warrior\n.goto Zone,1,1 >> Warrior\n" + block + "step\n#hardcore\n.goto Zone,4,4 >> Hard\n"
    lua, ns = runtime()
    native = lua.globals().RXP.ParseGuide(raw)[0]
    assert len(native.steps) == 2 and native.steps[1].stepId == 100005
    active = lua.globals().RXP.ProcessGuideTable(native)[0]
    assert len(active.steps) == 1
    expected = (2, fingerprint(block))
    assert ns.StepIdentity(active.steps[1]) == expected, "source ordinal is not visible step one"
    lua.globals().RXP.hardcore = True
    hard = lua.globals().RXP.ProcessGuideTable(native)[0]
    assert len(hard.steps) == 2
    assert ns.StepIdentity(hard.steps[1]) == expected
    assert ns.StepIdentity(hard.steps[2]) == (3, fingerprint("step\n#hardcore\n.goto Zone,4,4 >> Hard\n"))
    lua.globals().RXP["class"] = "WARRIOR"
    warrior = lua.globals().RXP.ParseGuide(raw)[0]
    warrior_active = lua.globals().RXP.ProcessGuideTable(warrior)[0]
    assert len(warrior_active.steps) == 3
    assert ns.StepIdentity(warrior_active.steps[2]) == expected, "hash the raw block, not selected class lines"
    lua.globals().mage, lua.globals().warrior = native, warrior
    lua.execute("""
        assert(mage.steps[1].elements[1].text:find("Mage", 1, true))
        assert(warrior.steps[2].elements[1].text:find("Warrior", 1, true))
    """)


def test_included_sources_and_duplicate_steps():
    root = "#group Route\n#name Root\nstep\n.goto Zone,1,1 >> Root\nstep\n#include Route||Included\nstep\n#include Route||Included\n"
    included = "#group Route\n#name Included\nstep << Warrior\n.goto Zone,0,0 >> Hidden\nstep\n.goto Zone,2,2 >> Included\n"
    block = "step\n.goto Zone,2,2 >> Included\n"
    lua, ns = runtime()
    native = lua.globals().RXP.ParseGuide(root)[0]
    lua.globals().RXP.guides["Route||Included"] = lua.globals().RXP.ParseGuide(included)[0]
    active = lua.globals().RXP.ProcessGuideTable(native)[0]
    lua.globals().active = active
    lua.execute("""
        assert(#active.steps == 3)
        assert(active.steps[2] ~= RXP.guides["Route||Included"].steps[1], "native include really copies the pointer")
        assert(active.steps[2] ~= active.steps[3], "each include has its own native pointer")
        assert(active.steps[2].__glideProof == RXP.guides["Route||Included"].steps[1].__glideProof)
    """)
    expected = (2, fingerprint(block))
    assert ns.StepIdentity(active.steps[2]) == expected
    assert ns.StepIdentity(active.steps[3]) == expected
    changed = included.replace(">> Included", ">> Revised")
    lua.globals().RXP.guides["Route||Included"] = lua.globals().RXP.ParseGuide(changed)[0]
    replacement = lua.globals().RXP.ProcessGuideTable(native)[0]
    assert ns.StepIdentity(replacement.steps[2]) == (2, fingerprint("step\n.goto Zone,2,2 >> Revised\n"))
    assert ns.StepIdentity(active.steps[2]) == expected
    assert ns.StepIdentity(replacement.steps[1]) == ns.StepIdentity(active.steps[1]), "included changes cannot change the root block"


def test_startup_ambiguity_late_and_imported_source():
    def ambiguous(lua):
        lua.globals().raw = SIMPLE
        lua.globals().edited = SIMPLE.replace("Walk", "Hike")
        lua.execute("native = register(raw); alternative = register(edited)")

    lua, ns = runtime(ambiguous)
    assert ns.StepIdentity(lua.globals().native.steps[1]) is None
    assert ns.StepIdentity(lua.globals().alternative.steps[1]) is None
    # The actual returned pointer from a later parse has unambiguous source.
    native = lua.globals().RXP.ParseGuide(SIMPLE)[0]
    expected = (1, fingerprint(SIMPLE_BLOCK))
    assert ns.StepIdentity(native.steps[1]) == expected

    def imported(lua):
        lua.globals().raw = "--123/account\n" + SIMPLE
        lua.execute("native = RXP.ParseGuide(raw); RXP.guides[native.key] = native; "
                    "RXP.db.profile.guides.imported = {groupOrContent='compressed', key=native.key}; "
                    "LibStub = function() return {DecompressDeflate=function(_, data) "
                    "if data == 'compressed' then return raw end end} end")

    lua, ns = runtime(imported, late=True)
    assert ns.StepIdentity(lua.globals().native.steps[1]) == expected
    assert lua.globals().RXP.calls == 1

    def queued_import(lua):
        lua.globals().raw = SIMPLE
        lua.execute("native = RXP.ParseGuide(raw); RXP.guides[native.key] = native; "
                    "RXP.embeddedGuides[1] = {groupOrContent=raw, cache=true}")

    lua, ns = runtime(queued_import)
    assert ns.StepIdentity(lua.globals().native.steps[1]) == expected

    def no_source(lua):
        lua.globals().raw = SIMPLE
        lua.execute("native = RXP.ParseGuide(raw); RXP.guides[native.key] = native")

    lua, ns = runtime(no_source)
    assert ns.StepIdentity(lua.globals().native.steps[1]) is None, "native labels/IDs are not source proof"

    def deferred(lua):
        lua.globals().raw = SIMPLE
        lua.execute("RXP.guideCache = {deferred=function() return RXP.ParseGuide(raw) end}")

    lua, ns = runtime(deferred)
    parsed = lua.globals().RXP.guideCache.deferred()[0]
    assert ns.StepIdentity(parsed.steps[1]) == expected


def test_conditional_group_and_mismatched_native_provenance():
    raw = "#group Warriors << Warrior\n#group Casters << Mage\n#name Conditional\nstep\n.goto Zone,1,1 >> Walk\n"
    lua, ns = runtime()
    parsed = lua.globals().RXP.ParseGuide(raw)[0]
    assert parsed.steps[1].stepId == 100003, "inapplicable group header is removed before native line counting"
    expected = (1, fingerprint("step\n.goto Zone,1,1 >> Walk\n"))
    assert ns.StepIdentity(parsed.steps[1]) == expected

    def startup(lua):
        lua.globals().raw = raw
        lua.execute("native = register(raw)")

    lua, ns = runtime(startup)
    assert lua.globals().native.steps[1].stepId == 100004, "embedded forced group keeps physical header lines"
    assert ns.StepIdentity(lua.globals().native.steps[1]) == expected

    def wrong_layout(lua):
        lua.globals().raw = SIMPLE
        lua.execute("native = register(raw); native.steps[1].stepId = native.steps[1].stepId + 1")

    lua, ns = runtime(wrong_layout)
    assert ns.StepIdentity(lua.globals().native.steps[1]) is None, "matching key alone cannot bind native steps"


def test_legacy_step_group_and_tip_include():
    lua, ns = runtime()
    legacy = "#name Legacy\nstep\n#group 2\n.goto Zone,1,2 >> Group instruction\n"
    parsed = lua.globals().RXP.ParseGuide("Old Route", legacy)[0]
    assert ns.StepIdentity(parsed.steps[1]) == (1, fingerprint("step\n#group 2\n.goto Zone,1,2 >> Group instruction\n"))
    root = "#group Route\n#name Tips\nstep\n#tip\n* Tip note\n#include Route||Included\nstep\n.goto Zone,1,1 >> Main\n"
    included = "#group Route\n#name Included\nstep\n.goto Zone,2,2 >> Included\n"
    native = lua.globals().RXP.ParseGuide(root)[0]
    lua.globals().RXP.guides["Route||Included"] = lua.globals().RXP.ParseGuide(included)[0]
    active = lua.globals().RXP.ProcessGuideTable(native)[0]
    assert len(active.steps) == 2
    assert ns.StepIdentity(active.steps[1]) == (1, fingerprint("step\n.goto Zone,2,2 >> Included\n"))
    assert ns.StepIdentity(active.steps[2]) == (2, fingerprint("step\n.goto Zone,1,1 >> Main\n"))


def test_native_active_step_serialization_and_stale_proof():
    root = "#group Route\n#name Broadcast\nstep\n.goto Zone,1,1 >> Root instruction\nstep\n#include Route||Included\n"
    included = "#group Route\n#name Included\nstep\n.goto Zone,2,2 >> Included instruction\n"
    lua, ns = runtime()
    lua.globals().native = lua.globals().RXP.ParseGuide(root)[0]
    lua.globals().RXP.guides["Route||Included"] = lua.globals().RXP.ParseGuide(included)[0]
    lua.execute("active = RXP.ProcessGuideTable(native); encoded = RXP.v2:EncodePlayerActiveSteps(active.steps)")
    assert lua.globals().encoded == "serialized active steps"
    lua.execute("""
        assert(#RXP.comms.lastSerialized == 2)
        assert(RXP.comms.lastSerialized[1].text:find("Root instruction", 1, true))
        assert(RXP.comms.lastSerialized[2].text:find("Included instruction", 1, true))
        for _, step in ipairs(RXP.comms.lastSerialized) do
            assert(step.__glideProof == nil, "opaque table proof must not enter the party payload")
        end
        stale = {stepId=active.steps[1].stepId, elements={}, __glideProof={}}
        generated = {stepId=active.steps[1].stepId, elements={}}
        wrongId = {stepId=active.steps[1].stepId+1, __glideProof=active.steps[1].__glideProof}
        staleActive = RXP.ProcessGuideTable({steps={stale}})
    """)
    assert ns.StepIdentity(lua.globals().active.steps[1]) == (1, fingerprint("step\n.goto Zone,1,1 >> Root instruction\n"))
    assert ns.StepIdentity(lua.globals().active.steps[2]) == (1, fingerprint("step\n.goto Zone,2,2 >> Included instruction\n"))
    assert ns.StepIdentity(lua.globals().RXP.comms.lastSerialized[1]) is None
    assert ns.StepIdentity(lua.globals().staleActive.steps[1]) is None, "processing cannot revive a deserialized capability"
    assert ns.StepIdentity(lua.globals().generated) is None
    assert ns.StepIdentity(lua.globals().wrongId) is None


def test_startup_native_normalized_keys():
    ordinary = SIMPLE.replace("#name Same", "#name 1-9 Same")

    def ordinary_startup(lua):
        lua.globals().raw = ordinary
        lua.execute("native = register(raw)")

    lua, ns = runtime(ordinary_startup)
    assert lua.globals().native.key == "Route||01-09 Same"
    assert ns.StepIdentity(lua.globals().native.steps[1]) == (1, fingerprint(SIMPLE_BLOCK))
    assert lua.globals().RXP.calls == 1
    raw = "#group Warriors << Warrior\n#group RestedXP Survival Guide << Mage\n#subgroup RestedXP Survival Guide 1-60\n#name 1-9 Normalized\nstep\n.goto Zone,1,1 >> True authored source\n"

    def startup(lua):
        lua.globals().raw = raw
        lua.execute("RXP.game = 'TBC'; native = register(raw); RXP.guides.alias = native")

    lua, ns = runtime(startup)
    native = lua.globals().native
    assert native.key == "RXP TBC Survival Guide|TBC Survival Guide 1-60|01-09 Normalized"
    expected = (1, fingerprint("step\n.goto Zone,1,1 >> True authored source\n"))
    assert ns.StepIdentity(native.steps[1]) == expected
    assert ns.StepIdentity(lua.globals().RXP.ProcessGuideTable(native)[0].steps[1]) == expected
    assert lua.globals().RXP.calls == 1, "startup normalization must not execute ParseGuide"
    alias = raw.replace("RestedXP Survival Guide << Mage", "RXP TBC Survival Guide << Mage").replace(
        "#subgroup RestedXP Survival Guide 1-60", "#subgroup TBC Survival Guide 1-60",
    ).replace("True authored source", "Conflicting authored source")

    def ambiguous(lua):
        startup(lua)
        lua.globals().alias = alias
        lua.execute("alternative = register(alias)")

    lua, ns = runtime(ambiguous)
    assert lua.globals().native.key == lua.globals().alternative.key
    assert ns.StepIdentity(lua.globals().native.steps[1]) is None
    assert ns.StepIdentity(lua.globals().alternative.steps[1]) is None, "normalized aliases must not choose either source"
    assert ns.StepIdentity(lua.globals().RXP.ProcessGuideTable(lua.globals().native)[0].steps[1]) is None
    classic = "#group RestedXP Alliance 1-20\n#classic\n#name 1-9 Classic\nstep\n.goto Zone,1,1 >> Original\n"

    def classic_startup(lua):
        lua.globals().raw = classic
        lua.execute("RXP.game = 'TBC'; native = register(raw)")

    lua, ns = runtime(classic_startup)
    assert lua.globals().native.key == "C-RestedXP Alliance 1-20|Classic 1-20|01-09 Classic"
    assert ns.StepIdentity(lua.globals().native.steps[1]) == (1, fingerprint("step\n.goto Zone,1,1 >> Original\n"))
    conditional = "#group First << Mage\n#group Last << Mage\n#name 1-9 Conditional\nstep\n.goto Zone,1,1 >> Selected\n"
    expected = (1, fingerprint("step\n.goto Zone,1,1 >> Selected\n"))

    def embedded_groups(lua):
        lua.globals().raw = conditional
        lua.execute("native = register(raw)")

    lua, ns = runtime(embedded_groups)
    assert lua.globals().native.key == "First||01-09 Conditional"
    assert ns.StepIdentity(lua.globals().native.steps[1]) == expected

    def imported_groups(lua):
        lua.globals().raw = conditional
        lua.execute("native = RXP.ParseGuide(raw); RXP.guides[native.key] = native; "
                    "RXP.embeddedGuides[1] = {groupOrContent=raw, cache=true}")

    lua, ns = runtime(imported_groups)
    assert lua.globals().native.key == "Last||01-09 Conditional"
    assert ns.StepIdentity(lua.globals().native.steps[1]) == expected


if __name__ == "__main__":
    for test in (test_md5, test_startup_cache_and_no_rehash, test_unrelated_edits_and_original_numbering,
                 test_canonical_and_native_line_mapping, test_class_action_and_settings_filtering,
                 test_included_sources_and_duplicate_steps, test_startup_ambiguity_late_and_imported_source,
                 test_conditional_group_and_mismatched_native_provenance, test_legacy_step_group_and_tip_include,
                 test_native_active_step_serialization_and_stale_proof, test_startup_native_normalized_keys):
        test()
    print("OK: addon MD5 and captured source-step identity")
