#!/usr/bin/env python3
"""
Build a recompilation/analysis-only ELF for .hack//INFECTION DEMO.PRG.

Version 4 fixes an important PS2Recomp interaction:
PS2Recomp's ElfParser::readWord() walks section headers in order and returns
from the first data-bearing section whose address range covers the guest PC.
The original SLUS contains pre-existing overlay-related sections that overlap
0x00400800..., so simply appending .demo_overlay is not sufficient.

This script:
  * maps DEMO.PRG into the exact 0x00400800 PT_LOAD reservation,
  * neutralizes pre-existing SHF_ALLOC sections overlapping that reservation,
  * adds authoritative .demo_overlay and .demo_bss sections,
  * performs a PS2Recomp-style first-match read self-check at known addresses.

The result is for Ghidra/PS2Recomp analysis only. Do NOT boot it.
"""

from __future__ import annotations
import argparse
import struct
from pathlib import Path

PT_LOAD = 1
PF_X = 1

SHT_PROGBITS = 1
SHT_NOBITS = 8

SHF_WRITE = 0x1
SHF_ALLOC = 0x2
SHF_EXECINSTR = 0x4


def u16(b, o): return struct.unpack_from("<H", b, o)[0]
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def p16(b, o, v): struct.pack_into("<H", b, o, v & 0xFFFF)
def p32(b, o, v): struct.pack_into("<I", b, o, v & 0xFFFFFFFF)


def align_up(v: int, a: int) -> int:
    return v if a <= 1 else (v + a - 1) & ~(a - 1)


def congruent_append_offset(current: int, align: int, vaddr: int) -> int:
    if align <= 1:
        return current
    return current + ((vaddr % align - current % align) % align)


def parse_mwo3(prg: bytes):
    if len(prg) < 0x40 or prg[:4] != b"MWo3":
        raise ValueError("overlay is not MWo3")
    kind, load, f0c, f10, bss, ctor_a, ctor_b = struct.unpack_from("<7I", prg, 4)
    name = prg[0x20:0x40].split(b"\0", 1)[0].decode("ascii", "replace")
    return {
        "kind": kind, "load": load, "bss": bss,
        "ctor_a": ctor_a, "ctor_b": ctor_b, "name": name
    }


def make_shdr(entsize, *, name, typ, flags, addr, offset, size, addralign=1):
    if entsize < 40:
        raise ValueError("unexpected ELF32 section-header size")
    out = bytearray(entsize)
    struct.pack_into("<10I", out, 0,
                     name, typ, flags, addr, offset, size,
                     0, 0, addralign, 0)
    return out


def section_first_match_word(elf: bytes, addr: int):
    """Mimic PS2Recomp ElfParser::readWord section-order behavior."""
    shoff = u32(elf, 0x20)
    shentsize = u16(elf, 0x2E)
    shnum = u16(elf, 0x30)

    for i in range(shnum):
        sh = shoff + i * shentsize
        typ = u32(elf, sh + 0x04)
        sec_addr = u32(elf, sh + 0x0C)
        sec_off = u32(elf, sh + 0x10)
        sec_size = u32(elf, sh + 0x14)

        if sec_size < 4 or addr < sec_addr:
            continue

        rel = addr - sec_addr
        if rel > sec_size - 4:
            continue

        # NOBITS -> PS2Recomp section.data == nullptr, so it keeps looking.
        if typ == SHT_NOBITS:
            continue

        if sec_off + rel + 4 > len(elf):
            continue

        return i, struct.unpack_from("<I", elf, sec_off + rel)[0]

    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("main_elf", type=Path)
    ap.add_argument("overlay_prg", type=Path)
    ap.add_argument("output_elf", type=Path)
    args = ap.parse_args()

    elf = bytearray(args.main_elf.read_bytes())
    prg = args.overlay_prg.read_bytes()
    mw = parse_mwo3(prg)

    if elf[:4] != b"\x7fELF" or elf[4] != 1 or elf[5] != 1:
        raise ValueError("expected little-endian ELF32")

    e_phoff = u32(elf, 0x1C)
    e_shoff = u32(elf, 0x20)
    e_phentsize = u16(elf, 0x2A)
    e_phnum = u16(elf, 0x2C)
    e_shentsize = u16(elf, 0x2E)
    e_shnum = u16(elf, 0x30)
    e_shstrndx = u16(elf, 0x32)

    if not e_shoff or not e_shnum:
        raise ValueError("input ELF has no section table")

    load = int(mw["load"])
    bss = int(mw["bss"])
    memsz = len(prg) + bss
    end = load + memsz

    # Snapshot original section headers.
    old = [
        bytearray(elf[e_shoff+i*e_shentsize:e_shoff+(i+1)*e_shentsize])
        for i in range(e_shnum)
    ]

    # Preserve and extend the section-name string table.
    shstr_hdr = old[e_shstrndx]
    shstr_off = u32(shstr_hdr, 0x10)
    shstr_size = u32(shstr_hdr, 0x14)
    shstr = bytearray(elf[shstr_off:shstr_off+shstr_size])

    def add_name(s: str) -> int:
        enc = s.encode() + b"\0"
        pos = shstr.find(enc)
        if pos >= 0:
            return pos
        pos = len(shstr)
        shstr.extend(enc)
        return pos

    demo_name = add_name(".demo_overlay")
    bss_name = add_name(".demo_bss")

    # Find the exact DEMO PT_LOAD reservation.
    matches = []
    for i in range(e_phnum):
        ph = e_phoff + i*e_phentsize
        if u32(elf, ph) != PT_LOAD:
            continue
        if u32(elf, ph+0x08) != load:
            continue
        if u32(elf, ph+0x14) == memsz:
            matches.append((i, ph))

    if len(matches) != 1:
        raise ValueError(f"expected one PT_LOAD at 0x{load:08X} with memsz 0x{memsz:X}; got {len(matches)}")

    seg_i, ph = matches[0]
    p_align = u32(elf, ph+0x1C)
    old_flags = u32(elf, ph+0x18)

    overlay_off = congruent_append_offset(len(elf), p_align, load)
    elf.extend(b"\0" * (overlay_off-len(elf)))
    elf.extend(prg)

    p32(elf, ph+0x04, overlay_off)
    p32(elf, ph+0x10, len(prg))
    p32(elf, ph+0x18, old_flags | PF_X)

    # Critical v4 fix:
    # Neutralize every pre-existing section whose numeric address range
    # intersects the DEMO reservation, regardless of SHF_ALLOC.
    #
    # PS2Recomp's readWord() currently uses section address/size and data
    # presence for its first-match lookup; it does not require SHF_ALLOC or
    # SHF_EXECINSTR. Infection's ELF has a huge non-alloc ".debug" section
    # starting at sh_addr=0 which numerically covers 0x0040F600 and otherwise
    # wins before .demo_overlay.
    neutralized = []
    for i, sh in enumerate(old):
        typ = u32(sh, 0x04)
        flags = u32(sh, 0x08)
        addr = u32(sh, 0x0C)
        size = u32(sh, 0x14)

        if size == 0:
            continue

        sec_end = addr + size
        if addr < end and sec_end > load:
            neutralized.append((i, addr, size, flags, typ))
            # Preserve contents/indices, but remove it from address matching.
            p32(sh, 0x0C, 0)      # sh_addr
            p32(sh, 0x14, 0)      # sh_size
            p32(sh, 0x08, flags & ~(SHF_ALLOC | SHF_EXECINSTR | SHF_WRITE))

    # Write extended shstrtab.
    new_shstr_off = align_up(len(elf), 4)
    elf.extend(b"\0" * (new_shstr_off-len(elf)))
    elf.extend(shstr)
    p32(old[e_shstrndx], 0x10, new_shstr_off)
    p32(old[e_shstrndx], 0x14, len(shstr))

    demo_sh = make_shdr(
        e_shentsize, name=demo_name, typ=SHT_PROGBITS,
        flags=SHF_ALLOC|SHF_EXECINSTR,
        addr=load, offset=overlay_off, size=len(prg), addralign=16
    )
    demo_bss_sh = make_shdr(
        e_shentsize, name=bss_name, typ=SHT_NOBITS,
        flags=SHF_ALLOC|SHF_WRITE,
        addr=load+len(prg), offset=0, size=bss, addralign=16
    )

    new_shoff = align_up(len(elf), 4)
    elf.extend(b"\0" * (new_shoff-len(elf)))
    for sh in old:
        elf.extend(sh)
    elf.extend(demo_sh)
    elf.extend(demo_bss_sh)

    p32(elf, 0x20, new_shoff)
    p16(elf, 0x30, e_shnum+2)

    # Self-check exactly the way PS2Recomp readWord() chooses section data.
    checks = [0x00400900, 0x0040F600, 0x0040F61C, 0x0040F6B0, 0x0040F760]
    for addr in checks:
        prg_off = addr - load
        if not (0 <= prg_off <= len(prg)-4):
            continue
        expected = struct.unpack_from("<I", prg, prg_off)[0]
        sec_i, actual = section_first_match_word(bytes(elf), addr)
        if actual != expected:
            raise RuntimeError(
                f"self-check failed at 0x{addr:08X}: "
                f"PS2Recomp-style first match gives {actual!r}, "
                f"expected 0x{expected:08X}"
            )
        print(f"[self-check] 0x{addr:08X} -> 0x{actual:08X} via section #{sec_i}")

    args.output_elf.parent.mkdir(parents=True, exist_ok=True)
    args.output_elf.write_bytes(elf)

    print()
    print(f"MWo3:              {mw['name']}")
    print(f"load:              0x{load:08X}")
    print(f"file image:        0x{len(prg):X}")
    print(f"BSS:               0x{bss:X}")
    print(f"mapped end:        0x{end:08X}")
    print(f"patched PT_LOAD:   #{seg_i}")
    print(f"overlay file off:  0x{overlay_off:X}")
    print(f"neutralized:       {len(neutralized)} overlapping original section(s)")
    for i, addr, size, flags, typ in neutralized:
        print(f"  section #{i}: addr=0x{addr:08X} size=0x{size:X} flags=0x{flags:X} type=0x{typ:X}")
    print(f"output:            {args.output_elf}")


if __name__ == "__main__":
    main()
