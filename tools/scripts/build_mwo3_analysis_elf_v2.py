#!/usr/bin/env python3
"""
Build a recompilation/analysis-only ELF for .hack//INFECTION by mapping an
MWo3 overlay PRG into the matching reserved PT_LOAD segment of the main SLUS.

Version 2 also adds real ELF section headers for the overlay. This is required
by PS2Recomp: Ghidra-map functions are accepted only when their addresses fall
inside a known executable ELF section.

The output is NOT intended to replace the original game executable at runtime.
Use it only as input to Ghidra/PS2Recomp.
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


def u16(buf: bytes | bytearray, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def u32(buf: bytes | bytearray, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def p16(buf: bytearray, off: int, value: int) -> None:
    struct.pack_into("<H", buf, off, value & 0xFFFF)


def p32(buf: bytearray, off: int, value: int) -> None:
    struct.pack_into("<I", buf, off, value & 0xFFFFFFFF)


def align_up(value: int, alignment: int) -> int:
    if alignment <= 1:
        return value
    return (value + alignment - 1) & ~(alignment - 1)


def congruent_append_offset(current_size: int, align: int, vaddr: int) -> int:
    """Return >= current_size with offset % align == vaddr % align."""
    if align <= 1:
        return current_size
    want = vaddr % align
    have = current_size % align
    return current_size + ((want - have) % align)


def parse_mwo3(prg: bytes) -> dict[str, int | str]:
    if len(prg) < 0x40 or prg[:4] != b"MWo3":
        raise ValueError("overlay is not an MWo3 file")

    kind, load_addr, field_0c, field_10, bss_size, ctor_start, ctor_end = \
        struct.unpack_from("<7I", prg, 4)

    raw_name = prg[0x20:0x40].split(b"\0", 1)[0]
    name = raw_name.decode("ascii", errors="replace")

    if ctor_end < ctor_start or ((ctor_end - ctor_start) % 4) != 0:
        raise ValueError("invalid MWo3 constructor table range")

    return {
        "kind": kind,
        "load_addr": load_addr,
        "field_0c": field_0c,
        "field_10": field_10,
        "bss_size": bss_size,
        "ctor_start": ctor_start,
        "ctor_end": ctor_end,
        "name": name,
    }


def make_shdr(
    entsize: int,
    *,
    name: int,
    sh_type: int,
    flags: int,
    addr: int,
    offset: int,
    size: int,
    link: int = 0,
    info: int = 0,
    addralign: int = 1,
    entity_size: int = 0,
) -> bytes:
    if entsize < 40:
        raise ValueError(f"ELF32 section-header size is too small: {entsize}")

    out = bytearray(entsize)
    struct.pack_into(
        "<10I",
        out,
        0,
        name,
        sh_type,
        flags,
        addr,
        offset,
        size,
        link,
        info,
        addralign,
        entity_size,
    )
    return bytes(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("main_elf", type=Path)
    ap.add_argument("overlay_prg", type=Path)
    ap.add_argument("output_elf", type=Path)
    args = ap.parse_args()

    elf = bytearray(args.main_elf.read_bytes())
    prg = args.overlay_prg.read_bytes()
    mw = parse_mwo3(prg)

    if elf[:4] != b"\x7fELF":
        raise ValueError("main executable is not ELF")
    if elf[4] != 1:
        raise ValueError("expected ELF32")
    if elf[5] != 1:
        raise ValueError("expected little-endian ELF")

    e_phoff = u32(elf, 0x1C)
    e_shoff = u32(elf, 0x20)
    e_phentsize = u16(elf, 0x2A)
    e_phnum = u16(elf, 0x2C)
    e_shentsize = u16(elf, 0x2E)
    e_shnum = u16(elf, 0x30)
    e_shstrndx = u16(elf, 0x32)

    if e_phentsize < 0x20:
        raise ValueError(f"unexpected program-header size: {e_phentsize}")
    if e_shentsize < 0x28:
        raise ValueError(f"unexpected section-header size: {e_shentsize}")
    if e_shoff == 0 or e_shnum == 0:
        raise ValueError("input ELF has no section table")
    if e_shstrndx >= e_shnum:
        raise ValueError("invalid e_shstrndx")

    load_addr = int(mw["load_addr"])
    bss_size = int(mw["bss_size"])
    full_mem_size = len(prg) + bss_size

    # Preserve the original section table before appending anything.
    old_shdrs = [
        bytearray(elf[e_shoff + i * e_shentsize:
                      e_shoff + (i + 1) * e_shentsize])
        for i in range(e_shnum)
    ]

    if any(len(x) != e_shentsize for x in old_shdrs):
        raise ValueError("truncated input section-header table")

    shstr_hdr = old_shdrs[e_shstrndx]
    old_shstr_off = u32(shstr_hdr, 0x10)
    old_shstr_size = u32(shstr_hdr, 0x14)

    if old_shstr_off + old_shstr_size > len(elf):
        raise ValueError("section-name string table is outside the input ELF")

    shstr = bytearray(elf[old_shstr_off:old_shstr_off + old_shstr_size])

    def add_shstr_name(name: str) -> int:
        encoded = name.encode("ascii") + b"\0"
        # Reuse an existing exact null-terminated name if present.
        start = 0
        while True:
            pos = shstr.find(encoded, start)
            if pos < 0:
                break
            if pos == 0 or shstr[pos - 1] == 0:
                return pos
            start = pos + 1

        result = len(shstr)
        shstr.extend(encoded)
        return result

    demo_name_off = add_shstr_name(".demo_overlay")
    bss_name_off = add_shstr_name(".demo_bss")

    # Locate the exact reserved PT_LOAD for DEMO.PRG.
    candidates: list[tuple[int, int, int, int, int, int, int, int]] = []

    for i in range(e_phnum):
        ph = e_phoff + i * e_phentsize
        p_type = u32(elf, ph + 0x00)
        p_offset = u32(elf, ph + 0x04)
        p_vaddr = u32(elf, ph + 0x08)
        p_paddr = u32(elf, ph + 0x0C)
        p_filesz = u32(elf, ph + 0x10)
        p_memsz = u32(elf, ph + 0x14)
        p_flags = u32(elf, ph + 0x18)
        p_align = u32(elf, ph + 0x1C)

        if p_type == PT_LOAD and p_vaddr == load_addr:
            candidates.append(
                (i, ph, p_offset, p_paddr, p_filesz, p_memsz, p_flags, p_align)
            )

    if not candidates:
        raise ValueError(
            f"no PT_LOAD starts at MWo3 load address 0x{load_addr:08X}"
        )

    exact = [c for c in candidates if c[5] == full_mem_size]
    if len(exact) != 1:
        desc = ", ".join(
            f"#{c[0]} filesz=0x{c[4]:X} memsz=0x{c[5]:X}" for c in candidates
        )
        raise ValueError(
            "could not uniquely identify the overlay reservation. "
            f"Expected memsz=0x{full_mem_size:X}; candidates: {desc}"
        )

    i, ph, old_offset, p_paddr, old_filesz, p_memsz, old_flags, p_align = exact[0]

    # Append the MWo3 image at an offset satisfying the PT_LOAD congruence rule.
    overlay_file_off = congruent_append_offset(len(elf), p_align, load_addr)
    if overlay_file_off > len(elf):
        elf.extend(b"\0" * (overlay_file_off - len(elf)))
    elf.extend(prg)

    # Point the existing overlay reservation at the appended MWo3 image.
    p32(elf, ph + 0x04, overlay_file_off)
    p32(elf, ph + 0x10, len(prg))
    p32(elf, ph + 0x18, old_flags | PF_X)

    # Relocate/extend the section-name string table.
    new_shstr_off = align_up(len(elf), 4)
    if new_shstr_off > len(elf):
        elf.extend(b"\0" * (new_shstr_off - len(elf)))
    elf.extend(shstr)

    p32(old_shdrs[e_shstrndx], 0x10, new_shstr_off)
    p32(old_shdrs[e_shstrndx], 0x14, len(shstr))

    # Add a real executable section covering the entire file-backed overlay.
    # This is intentionally broader than only "text": DEMO.PRG contains callable
    # code near the end of its file image (e.g. 0x40F600).
    demo_shdr = make_shdr(
        e_shentsize,
        name=demo_name_off,
        sh_type=SHT_PROGBITS,
        flags=SHF_ALLOC | SHF_EXECINSTR,
        addr=load_addr,
        offset=overlay_file_off,
        size=len(prg),
        addralign=16,
    )

    bss_shdr = make_shdr(
        e_shentsize,
        name=bss_name_off,
        sh_type=SHT_NOBITS,
        flags=SHF_ALLOC | SHF_WRITE,
        addr=load_addr + len(prg),
        offset=0,
        size=bss_size,
        addralign=16,
    )

    # Append a fresh complete section table and point the ELF header to it.
    new_shoff = align_up(len(elf), 4)
    if new_shoff > len(elf):
        elf.extend(b"\0" * (new_shoff - len(elf)))

    for shdr in old_shdrs:
        elf.extend(shdr)
    elf.extend(demo_shdr)
    elf.extend(bss_shdr)

    new_shnum = e_shnum + 2
    p32(elf, 0x20, new_shoff)
    p16(elf, 0x30, new_shnum)

    args.output_elf.parent.mkdir(parents=True, exist_ok=True)
    args.output_elf.write_bytes(elf)

    ctor_count = (int(mw["ctor_end"]) - int(mw["ctor_start"])) // 4
    ctor_off = int(mw["ctor_start"]) - load_addr
    ctors = [
        struct.unpack_from("<I", prg, ctor_off + n * 4)[0]
        for n in range(ctor_count)
    ]

    print(f"MWo3 name:             {mw['name']}")
    print(f"load address:          0x{load_addr:08X}")
    print(f"file image size:       0x{len(prg):X}")
    print(f"BSS size:              0x{bss_size:X}")
    print(f"mapped memory size:    0x{full_mem_size:X}")
    print(f"mapped end:            0x{load_addr + full_mem_size:08X}")
    print(f"constructor table:     0x{int(mw['ctor_start']):08X}-0x{int(mw['ctor_end']):08X}")
    print("constructors:          " + ", ".join(f"0x{x:08X}" for x in ctors))
    print()
    print(f"patched PT_LOAD:       #{i}")
    print(f"old filesz:            0x{old_filesz:X}")
    print(f"old offset:            0x{old_offset:X}")
    print(f"new overlay offset:    0x{overlay_file_off:X}")
    print(f"PT_LOAD alignment:     0x{p_align:X}")
    print()
    print(f"added section:         .demo_overlay")
    print(f"  addr:                0x{load_addr:08X}")
    print(f"  offset:              0x{overlay_file_off:X}")
    print(f"  size:                0x{len(prg):X}")
    print(f"  flags:               ALLOC|EXEC")
    print(f"added section:         .demo_bss")
    print(f"  addr:                0x{load_addr + len(prg):08X}")
    print(f"  size:                0x{bss_size:X}")
    print(f"  flags:               ALLOC|WRITE")
    print(f"new section count:     {new_shnum}")
    print(f"new section table:     0x{new_shoff:X}")
    print(f"output:                {args.output_elf}")


if __name__ == "__main__":
    main()
