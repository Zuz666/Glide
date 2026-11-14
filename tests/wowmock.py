"""Mocked WoW API (Lua) shared by the addon tests (test_link.py, test_extras.py, test_addon_panel.py).

UI widgets keep what a test needs to check: a font string's text and font object, a check button's state, a
button's enabled state and text, the sounds played, the frames Escape closes (UISpecialFrames). Methods are listed
one by one, never stubbed wholesale, so an API the client doesn't have still fails a test."""
MOCK = r"""
textures = {}
local function newTex()
  local t = {r=0,g=0,b=0,x=0,y=0,w=1,h=1,shown=true}
  function t:SetColorTexture(r,g,b) self.r,self.g,self.b = r,g,b end
  function t:SetPoint(_,_,_,x,y) if type(x) == "number" then self.x, self.y = x, y else self.skip = true end end
  function t:SetSize(w,h) self.w, self.h = w, h end
  function t:SetHeight(h) self.h = h end function t:SetWidth(w) self.w = w end
  function t:ClearAllPoints() end function t:Show() self.shown=true end function t:Hide() self.shown=false end
  function t:SetSnapToPixelGrid() end function t:SetTexelSnappingBias() end
  function t:SetTexture() end function t:SetTexCoord() end
  function t:SetDesaturated(v) self.desat = v end function t:SetVertexColor(r, g) self.vg = g end
  function t:SetAllPoints() self.shown = false end   -- gauge backgrounds: not painted in the mock
  textures[#textures+1] = t
  return t
end
handlers = {}
SOUNDS = {}
SOUNDKIT = { IG_MAINMENU_OPTION_CHECKBOX_ON = 856, IG_MAINMENU_OPTION_CHECKBOX_OFF = 857,
             IG_CHARACTER_INFO_OPEN = 839, IG_CHARACTER_INFO_CLOSE = 840 }
function PlaySound(id) SOUNDS[#SOUNDS+1] = id end
UISpecialFrames = {}
tinsert = table.insert
FONTS = { GameFontNormal = true, GameFontNormalLarge = true, GameFontHighlight = true, GameFontHighlightSmall = true,
          GameFontDisable = true, GameFontDisableSmall = true }
local function newFontString(parent, font)
  assert(font == nil or FONTS[font], "unknown font object " .. tostring(font))
  local fs = {font = font, text = nil, shown = true, parent = parent}
  function fs:SetPoint() end function fs:ClearAllPoints() end
  function fs:SetText(t) self.text = t end function fs:GetText() return self.text end
  function fs:SetFontObject(f) assert(FONTS[f], "unknown font object " .. tostring(f)); self.font = f end
  function fs:GetStringWidth() return #(self.text or "") * 6 end
  function fs:SetJustifyH() end function fs:SetWidth() end function fs:SetWordWrap() end
  function fs:SetTextColor(r, g, b) self.color = {r, g, b} end
  function fs:Show() self.shown = true end function fs:Hide() self.shown = false end
  parent.fontstrings = parent.fontstrings or {}
  parent.fontstrings[#parent.fontstrings+1] = fs
  return fs
end
TEMPLATES = { UICheckButtonTemplate = true, UIPanelButtonTemplate = true, UIPanelCloseButton = true, BackdropTemplate = true }
function CreateFrame(kind, name, parent, template)
  assert(template == nil or TEMPLATES[template], "unknown template " .. tostring(template))
  local f = {events={}, kind=kind, template=template, parent=parent, enabled=true}
  if parent and type(parent) == "table" then parent.children = parent.children or {}; parent.children[#parent.children+1] = f end
  if name then _G[name] = f end
  function f:RegisterEvent(e) self.events[e]=true end
  for _, m in ipairs({"SetFrameStrata","SetFrameLevel","SetIgnoreParentScale","SetScale","ClearAllPoints","SetPoint","SetSize",
                      "RegisterForClicks","RegisterForDrag","SetHighlightTexture","EnableMouse","SetClampedToScreen"}) do f[m] = function() end end
  -- OnShow / OnHide run when visibility changes, from Show/Hide or SetShown, as in the game
  function f:Show() local was = self.shown; self.shown = true
    if not was and self.scripts and self.scripts.OnShow then self.scripts.OnShow(self) end end
  function f:Hide() local was = self.shown; self.shown = false
    if was and self.scripts and self.scripts.OnHide then self.scripts.OnHide(self) end end
  function f:SetShown(v) if v then self:Show() else self:Hide() end end
  function f:IsShown() return self.shown end
  function f:SetAllPoints() end
  function f:CreateFontString(_, _, font) return newFontString(self, font) end
  -- windows: moving, the backdrop, layering
  for _, m in ipairs({"SetToplevel","SetMovable","StartMoving","StopMovingOrSizing","SetBackdrop","SetWidth"}) do f[m] = function() end end
  function f:GetPoint() return "CENTER", UIParent, "CENTER", 0, 60 end
  function f:SetSize(w, h) self.w, self.h = w, h end
  function f:SetHeight(h) self.h = h end
  function f:GetHeight() return self.h or 0 end
  -- scroll frames: a child, the wheel, the offset
  if kind == "ScrollFrame" then
    function f:SetScrollChild(c) self.child = c end
    function f:EnableMouseWheel(v) self.wheel = v end
    function f:SetVerticalScroll(v) self.scrolled = v end function f:GetVerticalScroll() return self.scrolled or 0 end
  end
  -- buttons and check buttons
  function f:SetEnabled(v) self.enabled = v and true or false end function f:IsEnabled() return self.enabled end
  function f:Enable() self.enabled = true end function f:Disable() self.enabled = false end
  function f:SetHitRectInsets(l, r) self.hitRight = r end
  function f:SetText(t) self.text = t end function f:GetText() return self.text end
  if kind == "CheckButton" then
    function f:SetChecked(v) self.checked = v and true or false end function f:GetChecked() return self.checked end
    -- the template's own label (renamed across client versions); Glide must not depend on it
    f.Text = newFontString(f, "GameFontHighlight")
  end
  -- a user click: the game toggles a check button before OnClick runs, and ignores clicks on disabled widgets
  function f:Click(button)
    if not self.enabled then return end
    if kind == "CheckButton" then self.checked = not self.checked end
    if self.scripts and self.scripts.OnClick then self.scripts.OnClick(self, button or "LeftButton") end
  end
  function f:SetScript(k, fn) self.scripts = self.scripts or {}; self.scripts[k] = fn; if k == "OnEvent" then self.fn = fn; handlers[#handlers+1] = self end end
  function f:CreateTexture() return newTex() end
  if kind == "StatusBar" then
    -- the engine can display secret values; the mock stands in for that with SECRET_SHOWN
    local fill = newTex(); f.fill = fill; fill.gauge = true
    function f:SetStatusBarTexture() end
    function f:GetStatusBarTexture() return fill end
    function f:SetStatusBarColor(r,g,b) fill:SetColorTexture(r,g,b) end
    function f:SetPoint(_,_,_,x,y) fill.x, fill.y = x, y end
    function f:SetSize(w,h) f.w = w; fill.h = h end
    function f:SetMinMaxValues(a,b) f.max = (b == "SECRET") and 100 or b end
    function f:SetValue(v) if v == "SECRET" then v = SECRET_SHOWN end fill.w = math.floor(f.w * v / f.max + 0.5) end
  end
  return f
end
SECRET_SHOWN = 40
function fire(e, ...) for _, f in ipairs(handlers) do if f.events[e] then f.fn(f, e, ...) end end end
WorldFrame = {}
tickers = {}
-- tick() moves the clock a quarter second; a ticker runs when its interval is up (at most once per tick)
C_Timer = { NewTicker = function(d, fn) local t = {fn=fn, d=d, due=NOW+d} function t:Cancel() t.dead=true end
                                        tickers[#tickers+1]=t return t end,
            After = function(d, fn) fn() end }
NOW = 100
function tick() NOW = NOW + 0.25
  for _, t in ipairs(tickers) do if not t.dead and NOW + 1e-9 >= t.due then t.due = t.due + t.d; t.fn(t) end end end
function GetPhysicalScreenSize() return 2560, 1440 end
function GetTime() return NOW end function GetServerTime() return math.floor(1791063521 + NOW) end
issecretvalue = function(v) return v == "SECRET" end
function UnitLevel(u) return u == "target" and 3 or 7 end
function UnitXP() return 1234 end function UnitXPMax() return 4500 end function GetXPExhaustion() return 2200 end
function UnitHealth(u) if u == "target" then return "SECRET" end return 80 end
function UnitHealthMax() return 100 end function UnitPower() return 50 end function UnitPowerMax() return 100 end
COMBAT = false
function UnitAffectingCombat() return COMBAT end function UnitIsGhost() return false end function UnitIsDeadOrGhost() return false end
function CreateVector2D(x, y) return {x=x, y=y} end
C_Map = { GetBestMapForUnit = function() return 2521 end,
  GetWorldPosFromMapPos = function(m, v) return 0, { GetXY = function() return 1000 - v.y * 2000, 500 - v.x * 3000 end } end,
  GetPlayerMapPosition = function() return { GetXY = function() return 0.4231, 0.2377 end } end }
function GetPlayerFacing() return 1.57 end function GetUnitSpeed() return 7 end function GetMoney() return 12345 end
C_Container = { GetContainerNumFreeSlots = function(b) return b == 0 and 10 or 2 end, GetItemCooldown = function() return 0, 0 end }
function GetInventoryItemDurability(s) if s == 5 then return 30, 100 end return nil end
function IsResting() return false end function IsMounted() return false end function UnitOnTaxi() return false end
function UnitExists(u) return u == "target" or u == "player" end
function UnitGUID(u) return "Creature-0-1-0-1-250001-0000ABCD" end
function UnitReaction() return 2 end function UnitIsDead() return false end
function strsplit(sep, s) local out = {} for p in (s..sep):gmatch("(.-)%"..sep) do out[#out+1]=p end return unpack(out) end
function UnitClass() return "Warrior", "WARRIOR" end function UnitRace() return "Skyborne", "Skyborne" end
function UnitFactionGroup() return "Horde" end function UnitName() return "Fermin" end
function GetBindLocation() return "Thendal Grove" end function GetZoneText() return "Zephras Isle" end function GetSubZoneText() return "Thendal Grove" end
function UnitClassification() return "normal" end
function UnitPowerType() return 1, "RAGE" end
KNOWN = { [6673] = true, [196] = true, [201] = true, [198] = true, [9078] = true, [9077] = true, [8737] = true, [9116] = true }
function IsPlayerSpell(id) return KNOWN[id] or false end
C_QuestLog = { GetNumQuestLogEntries = function() return 2 end,
  GetInfo = function(i) if i == 1 then return {isHeader=true} end return {questID=92461} end,
  GetQuestObjectives = function() return {{numFulfilled=3, numRequired=8}} end, IsComplete = function() return false end }
C_UnitAuras = { GetAuraDataByIndex = function(u, i, f)
  local list = { {name="Battle Shout", spellId=6673, expirationTime=190, duration=120, isFromPlayerOrPlayerPet=true},
                 {name="Power Word: Fortitude", spellId=1243, expirationTime=1900, duration=1800, isFromPlayerOrPlayerPet=false} }
  return list[i] end }
function GetWeaponEnchantInfo() return true, 600000, 0, 7, false end
local ITEMS = {
  ["|Hitem:2361|h"] = {"Battleworn Hammer", 1, 5, 1, "INVTYPE_2HWEAPON", 2, 5, 2361, {ITEM_MOD_DAMAGE_PER_SECOND_SHORT=3.5}, "Speed 2.90"},
  ["|Hitem:1|h"] = {"Heavy Hand Axe", 2, 14, 9, "INVTYPE_2HWEAPON", 2, 1, 1, {ITEM_MOD_STRENGTH_SHORT=3, ITEM_MOD_DAMAGE_PER_SECOND_SHORT=8.1}, "Speed 3.20",
                    "Equip: Improves your chance to get a critical strike by 1%."},
  ["|Hitem:6948|h"] = {"Hearthstone", 1, 1, 1, "", 15, 0, 6948, {}},
}
function GetInventoryItemLink(u, slot) if slot == 16 then return "|Hitem:2361|h" end end
function GetItemInfo(l) local i = ITEMS[l] if not i then return nil end
  return i[1], l, i[2], i[3], i[4], "", "", 1, i[5], 0, 0, i[6], i[7] end
function GetItemInfoInstant(l) local i = ITEMS[l] return i and i[8] end
C_Item = { GetItemStats = function(l) return ITEMS[l][9] end }
C_TooltipInfo = { GetHyperlink = function(l) local i = ITEMS[l]
  return { lines = { {leftText = i[1]}, {leftText = "Two-Hand", rightText = i[10]}, {leftText = i[11] or ""} } } end }
C_PlayerInfo = { CanUseItem = function() return true end }
C_Container.GetContainerNumSlots = function(b) return b == 0 and 2 or 0 end
C_Container.GetContainerItemInfo = function(b, s) if b == 0 and s == 1 then return {hyperlink="|Hitem:1|h"} end
  if b == 0 and s == 2 then return {hyperlink="|Hitem:6948|h"} end end
function GetNumQuestChoices() return 0 end
C_AddOns = { GetAddOnMetadata = function(a, k) if a == "Glide" and k == "Version" then return "0.15.3" end end }
-- Core.lua (slash /glide, mark) needs these for its recorder session
function UnitFullName() return "Fermin", "TestRealm" end
function GetBuildInfo() return "1.60.1", "70205", "Oct 1 2026", 16001 end
function GetLocale() return "enUS" end function GetRealmName() return "TestRealm" end
function date() return "20261004-120000" end function time() return 1791063521 end
Minimap = { GetWidth = function() return 140 end, GetCenter = function() return 1000, 600 end, GetEffectiveScale = function() return 1 end }
GameTooltip = { lines = {} }
function GameTooltip:SetOwner(o) self.owner = o; self.lines = {} end
function GameTooltip:AddLine(t) self.lines[#self.lines+1] = t end
function GameTooltip:AddDoubleLine(a, b) self.lines[#self.lines+1] = a .. "\t" .. b end
function GameTooltip:SetSpellByID(id) self.spell = id end
function GameTooltip:Show() self.shown = true end function GameTooltip:Hide() self.shown = false end
function GameTooltip:IsOwned(o) return self.owner == o end
UIParent = {}
function GetCursorPosition() return 1000, 700 end
MENU = nil
MenuUtil = { CreateContextMenu = function(owner, gen)
  local items = {}
  local root = { CreateTitle = function() end, CreateDivider = function() end,
    CreateCheckbox = function(_, text, get, set) items[#items+1] = {text=text, get=get, set=set} end,
    CreateButton = function(_, text, fn) items[#items+1] = {text=text, fn=fn} end }
  gen(owner, root); MENU = items end }
RXPCData = { currentStep = 5 }
RXP = { currentGuide = { name = "1-14 Zephras Isle", group = "RestedXP Forever Guide (H)", steps = {
  [5] = { elements = { {tag="goto", x=42.12, y=23.48, zone=2521, arrow=true, text="go"}, {tag="turnin", questId=92461, text="Turn in Harmony in Balance"} } },
  [6] = { elements = { {tag="accept", questId=92462, text="Accept Infestation Investigation"} } } } } }
SlashCmdList = {}
function print() end
"""


def install_bit(lua):
    """WoW's signed 32-bit operations; Lua 5.1 itself does not provide its host's bit library."""
    def signed(value):
        return ((value + 0x80000000) & 0xFFFFFFFF) - 0x80000000

    lua.globals().bit = lua.table_from({
        "band": lambda a, b: signed(int(a) & int(b)),
        "bor": lambda a, b: signed(int(a) | int(b)),
        "bxor": lambda a, b: signed(int(a) ^ int(b)),
        "lshift": lambda a, n: signed(int(a) << (int(n) % 32)),
        "rshift": lambda a, n: signed((int(a) & 0xFFFFFFFF) >> (int(n) % 32)),
    })
