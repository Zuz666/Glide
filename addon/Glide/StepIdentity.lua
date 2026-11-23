-- Content identity, not authentication. Source strings stay private to this module.
-- Native lifecycle and line accounting follow RXPGuides 605234891cc9a8512a9d5cb65ca35b4d29805228:
-- GuideLoader.lua ParseGuide / LoadGuide and GuideWindow.lua included-step copying.
local _, ns = ...
local records = {}
local proofs = setmetatable({}, {__mode = "k"})
local installed
local concat = table.concat

local function trim(line)
    return (line:gsub("^[ \t\v\f]+", ""):gsub("[ \t\v\f]+$", ""))
end

local function canonicalLines(text)
    local lines = {}
    for line in (text:gsub("\r\n", "\n"):gsub("\r", "\n").."\n"):gmatch("([^\n]*)\n") do
        line = trim(line:gsub("%-%-.*$", "")):gsub("[ \t]+", " ")
        if line ~= "" then lines[#lines + 1] = line end
    end
    return lines
end

local function sourceRecord(first, text)
    if type(first) ~= "string" or (text ~= nil and type(text) ~= "string") then return end
    local source = text or first
    if records[source] then return records[source] end
    local lines, steps = canonicalLines(source), {}
    for number, line in ipairs(lines) do
        if line:sub(1, 4) == "step" then
            if #steps > 0 then steps[#steps].last = number - 1 end
            steps[#steps + 1] = {first = number}
        end
    end
    if #steps == 0 then return end
    steps[#steps].last = #lines
    local record = {lines = lines, steps = steps}
    records[source] = record
    return record
end

local function applies(R, line)
    local condition = line:match("<<%s*(.+)")
    return not condition or R.applies(condition)
end

-- Reproduce only native text preprocessing and line counting, never call the parser.
local function nativeLayout(R, first, text, embedded, group)
    if type(first) ~= "string" or (text ~= nil and type(text) ~= "string") then return end
    if text == nil then
        text = first
        if embedded or not first:match("^%-%-(%d+)") then
            text = text:gsub("%-%-[^\r\n]*[\r\n]+", "\n")
        end
        if not group then
            text = text:gsub("(#group[ \t]*)([^\r\n]-)[ \t]*<<[ \t]*([^\r\n]-)[ \t]*[\r\n]+",
                function(prefix, value, condition)
                    if R.applies(condition) then return prefix..value.."\n" end
                    return "\n"
                end)
        end
    else
        text = text:gsub("%-%-[^\r\n]*[\r\n]", "\n")
    end
    local layout, ordinal, number = {}, 0, 0
    for line in text:gmatch("[^\n\r]+") do
        number = number + 1
        line = line:gsub("^%s+", ""):gsub("%s+$", "")
        if line:sub(1, 4) == "step" then
            ordinal = ordinal + 1
            if applies(R, line) then layout[#layout + 1] = {line = number, ordinal = ordinal} end
        end
    end
    return layout, ordinal
end

local function compatible(guide, record, layout, total)
    if type(guide) ~= "table" or type(guide.steps) ~= "table" or type(guide.guideId) ~= "number"
        or not layout or total ~= #record.steps or #layout ~= #guide.steps then return false end
    for number, native in ipairs(layout) do
        local step = guide.steps[number]
        if type(step) ~= "table" or step.stepId ~= guide.guideId + native.line then return false end
    end
    return true
end

local function bind(guide, record, layout, total)
    if not compatible(guide, record, layout, total) then return end
    for number, native in ipairs(layout) do
        local step, proof = guide.steps[number], {}
        -- Native includes shallow-copy step fields. Table identity survives those copies,
        -- but not SavedVariables or the scalar-only active-step party payload.
        proofs[proof] = {record = record, ordinal = native.ordinal, stepId = step.stepId}
        step.__glideProof = proof
    end
end

local function decompress(data)
    if type(data) ~= "string" then return end
    if C_EncodingUtil and C_EncodingUtil.DecompressString then
        local method = Enum and Enum.CompressionMethod and Enum.CompressionMethod.Deflate or 0
        local ok, out = pcall(C_EncodingUtil.DecompressString, data, method)
        if ok and type(out) == "string" and #out > 0 then return out end
    end
    local LD = LibStub and LibStub("LibDeflate", true)
    if LD then
        local ok, out = pcall(LD.DecompressDeflate, LD, data)
        if ok and type(out) == "string" then return out end
    end
end

-- Startup binding requires the complete native key AND the actual source's native stepId layout.
-- Multiple current registration/cache contents for the same key are ambiguous, even at equal line counts.
local function startupKey(R, first, text, embedded)
    local body, group = text or first, text and first or nil
    if not text then
        if embedded or not first:match("^%-%-(%d+)") then
            body = body:gsub("%-%-[^\r\n]*[\r\n]+", "\n")
        end
        if not embedded then
            -- Direct/imported ParseGuide uses the LAST applicable conditional group.
            -- Embedded LoadGuide instead pre-scans the FIRST applicable group and passes it in.
            body = body:gsub("(#group[ \t]*)([^\r\n]-)[ \t]*<<[ \t]*([^\r\n]-)[ \t]*[\r\n]+",
                function(prefix, value, condition)
                    if R.applies(condition) then group = value; return prefix..value.."\n" end
                    return "\n"
                end)
        end
    end
    local header = {}
    for line in body:gmatch("[^\r\n]+") do
        line = trim(line:gsub("%-%-.*$", ""))
        if line:sub(1, 4) == "step" then break end
        if applies(R, line) then
            line = line:gsub("%s*<<.*$", "")
            local tag, assignment, value = line:match("^#(%S+)%s*(=?)%s*(.*)")
            if tag then
                if assignment == "=" then return end -- Dynamic header evaluation is a parser side effect.
                if not header[tag] then header[tag] = value end
            end
        end
    end
    group = group or header.group
    header.group = group
    if not header.group or not header.name then return end
    -- ParseGuide normalizes metadata BEFORE its final BuildGuideKey (GuideLoader.lua:1260-1300).
    -- Keep the original group for native physical-line layout; normalized aliases are only lookup hints.
    local normalizedGroup = header.group
    if type(R.GroupOverride) == "function" then normalizedGroup = R.GroupOverride(header) or normalizedGroup end
    if R.game == "TBC" and header.classic and not header.wotlk and not normalizedGroup:find("TBC")
        and normalizedGroup:sub(1, 1) ~= "+" then
        local range = header.subgroup and header.subgroup:match("( %d+%-%d+)$")
        if range then
            if not R.locale or type(R.locale.Get) ~= "function" then return end
            header.subgroup = R.locale.Get("Classic")..range
        end
        if normalizedGroup:sub(1, 8) == "RestedXP" then
            if type(R.classicPrefix) ~= "string" then return end
            header.group = R.classicPrefix..normalizedGroup
        end
    end
    if header.name:match("^(%d)-(%d%d?)") then
        if type(R.affix) ~= "function" then return end
        header.name = header.name:gsub("^(%d)-(%d%d?)", R.affix)
    end
    -- Native overwrites any authored #key after normalization.
    return R.BuildGuideKey(header), group
end

local function captureStartup(R)
    if type(R.BuildGuideKey) ~= "function" then return end
    local candidates = {}
    local function capture(first, text, embedded, forcedKey)
        if type(first) ~= "string" or (text ~= nil and type(text) ~= "string") then return end
        local record = sourceRecord(first, text)
        if not record then return end
        local key, group = startupKey(R, first, text, embedded)
        if not key or (forcedKey and key ~= forcedKey) then return end
        local list = candidates[key]
        if not list then list = {}; candidates[key] = list end
        -- Modern embedded loading passes its pre-scanned group; legacy text uses the supplied group.
        local layout, total = nativeLayout(R, first, text, embedded, embedded and group or nil)
        list[#list + 1] = {record = record, layout = layout, total = total}
    end
    for _, queued in pairs(R.embeddedGuides or {}) do
        if type(queued) == "table" then
            if queued.cache then
                -- ImportGuide's startup queue holds RAW text; only db.profile.guides is compressed.
                capture(queued.groupOrContent, nil, false, queued.key)
            else
                capture(queued.groupOrContent, queued.text, true, queued.key)
            end
        end
    end
    local profile = R.db and R.db.profile
    for _, imported in pairs(profile and profile.guides or {}) do
        if type(imported) == "table" then
            capture(decompress(imported.groupOrContent), imported.text, false, imported.key)
        end
    end
    local seen = {}
    for _, guide in pairs(R.guides or {}) do
        if type(guide) == "table" and not seen[guide] then
            seen[guide] = true
            local list = candidates[guide.key]
            local candidate, source, ambiguous
            for _, item in ipairs(list or {}) do
                if source and source ~= item.record then ambiguous = true end
                source = item.record
                if compatible(guide, item.record, item.layout, item.total) then candidate = item end
            end
            if candidate and not ambiguous then
                bind(guide, candidate.record, candidate.layout, candidate.total)
            end
        end
    end
end

local function pack(...)
    return {n = select("#", ...), ...}
end

local function install()
    local R = _G.RXP
    if R == installed or type(R) ~= "table" or type(R.ParseGuide) ~= "function" or type(R.applies) ~= "function" then return end
    installed = R
    local parse = R.ParseGuide
    R.ParseGuide = function(first, text, defaultFor, embedded, group, ...)
        local result = pack(parse(first, text, defaultFor, embedded, group, ...))
        if type(result[1]) == "table" and not result[2] then
            local record = sourceRecord(first, text)
            if record then
                local layout, total = nativeLayout(R, first, text, embedded, group)
                bind(result[1], record, layout, total)
            end
        end
        return unpack(result, 1, result.n)
    end
    captureStartup(R)
end

function ns.StepIdentity(step)
    if type(step) ~= "table" then return end
    local proof = proofs[step.__glideProof]
    if not proof or step.stepId ~= proof.stepId then return end
    local record = proof.record
    local block = record.steps[proof.ordinal]
    if not block.fingerprint then
        block.fingerprint = "s1:"..ns.MD5("Glide:RXP:step:1\n"
            ..concat(record.lines, "\n", block.first, block.last).."\n")
    end
    return proof.ordinal, block.fingerprint
end

-- OptionalDeps puts this before RXP OnEnable wipes its already-parsed raw registration queue.
install()
if ns.rec and ns.rec.On then ns.rec.On("ADDON_LOADED", install) end
