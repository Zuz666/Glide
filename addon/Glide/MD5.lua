-- Narrow Lua 5.1 LIB32 MD5 slice from pure_lua_SHA, nonsecurity content matching only.
-- Upstream: https://github.com/Egor-Skriptunoff/pure_lua_SHA
-- Pin: 6adac177c16c3496899f69d220dfb20bc31c03df, sha2.lua
-- Reused: LIB32 md5_feed_64, MD5 constants, incremental md5 wrapper.
-- Adapted: WoW's existing bit library, addon namespace, hexadecimal formatting.
--[[
MIT License

Copyright (c) 2018-2022  Egor Skriptunoff

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
]]
local _, ns = ...
local b = _G.bit
local AND, OR, XOR = b.band, b.bor, b.bxor
local ROR = b.ror or b.rrotate or function(value, bits)
    return OR(b.rshift(value, bits), b.lshift(value, 32 - bits))
end
local byte, sub, char, gsub = string.byte, string.sub, string.char, string.gsub
local string_rep, table_concat, floor = string.rep, table.concat, math.floor
local md5_K, common_W = {}, {}
local md5_next_shift = {0, 0, 0, 0, 0, 0, 0, 0, 28, 25, 26, 27, 0, 0, 10, 9, 11, 12, 0, 15, 16, 17, 18, 0, 20, 22, 23, 21}
local function HEX(x)
    return string.format("%08x", x % 4294967296)
end

do
    local sin, abs, modf = math.sin, math.abs, math.modf
    for idx = 1, 64 do
        local hi, lo = modf(abs(sin(idx)) * 2^16)
        md5_K[idx] = hi * 65536 + floor(lo * 2^16)
    end
end

local function md5_feed_64(H, str, offs, size)
    -- offs >= 0, size >= 0, size is multiple of 64
    local W, K, shifts = common_W, md5_K, md5_next_shift
    local h1, h2, h3, h4 = H[1], H[2], H[3], H[4]
    for pos = offs, offs + size - 1, 64 do
        for j = 1, 16 do
            pos = pos + 4
            local a, c, d, e = byte(str, pos - 3, pos)
            W[j] = ((e * 256 + d) * 256 + c) * 256 + a
        end
        local a, c, d, e = h1, h2, h3, h4
        local s = 25
        for j = 1, 16 do
            local F = ROR(AND(c, d) + AND(-1-c, e) + a + K[j] + W[j], s) + c
            s = shifts[s]
            a, e, d, c = e, d, c, F
        end
        s = 27
        for j = 17, 32 do
            local F = ROR(AND(e, c) + AND(-1-e, d) + a + K[j] + W[(5*j-4) % 16 + 1], s) + c
            s = shifts[s]
            a, e, d, c = e, d, c, F
        end
        s = 28
        for j = 33, 48 do
            local F = ROR(XOR(XOR(c, d), e) + a + K[j] + W[(3*j+2) % 16 + 1], s) + c
            s = shifts[s]
            a, e, d, c = e, d, c, F
        end
        s = 26
        for j = 49, 64 do
            local F = ROR(XOR(d, OR(c, -1-e)) + a + K[j] + W[(j*7-7) % 16 + 1], s) + c
            s = shifts[s]
            a, e, d, c = e, d, c, F
        end
        h1, h2 = (a + h1) % 2^32, (c + h2) % 2^32
        h3, h4 = (d + h3) % 2^32, (e + h4) % 2^32
    end
    H[1], H[2], H[3], H[4] = h1, h2, h3, h4
end

function ns.MD5(message)
    local H, length, tail = {0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476}, 0.0, ""
    local function partial(message_part)
        if message_part then
            if tail then
                length = length + #message_part
                local offs = 0
                if tail ~= "" and #tail + #message_part >= 64 then
                    offs = 64 - #tail
                    md5_feed_64(H, tail..sub(message_part, 1, offs), 0, 64)
                    tail = ""
                end
                local size = #message_part - offs
                local size_tail = size % 64
                md5_feed_64(H, message_part, offs, size - size_tail)
                tail = tail..sub(message_part, #message_part + 1 - size_tail)
                return partial
            else
                error("Adding more chunks is not allowed after receiving the result", 2)
            end
        else
            if tail then
                local final_blocks = {tail, "\128", string_rep("\0", (-9 - length) % 64)}
                tail = nil
                length = length * 8
                for j = 4, 11 do
                    local low_byte = length % 256
                    final_blocks[j] = char(low_byte)
                    length = (length - low_byte) / 256
                end
                final_blocks = table_concat(final_blocks)
                md5_feed_64(H, final_blocks, 0, #final_blocks)
                for j = 1, 4 do H[j] = HEX(H[j]) end
                H = gsub(table_concat(H), "(..)(..)(..)(..)", "%4%3%2%1")
            end
            return H
        end
    end
    if message then return partial(message)() end
    return partial
end
