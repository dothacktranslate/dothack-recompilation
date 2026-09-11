#!/usr/bin/env python3
"""
Build a recompilation/analysis-only ELF for .hack//INFECTION by mapping an
MWo3 overlay PRG into the matching reserved PT_LOAD segment of the main SLUS.

The output is NOT intended to replace the original game executable at runtime.
Use it as input to Ghidra/PS2Recomp so statically recompiled functions exist for
the overlay's fixed guest addresses.
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path


PT_LOAD = 1
PF_X = 1


def u16(buf: bytes | bytearray, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def u32(buf: bytes | bytearray, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def p32(buf: bytearray, off: int, value: int) -> None:
    struct.pack_into("<I", buf, off, value & 0xFFFFFFFF)


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

    kind, load_addr, text_size, data_size, bss_size, ctor_start, ctor_end = \
        struct.unpack_from("<7I", prg, 4)

    raw_name = prg[0x20:0x40].split(b"\0", 1)[0]
    name = raw_name.decode("ascii", errors="replace")

    expected_file_size = 0x40 + text_size + data_size
    if expected_file_size != len(prg):
        raise ValueError(
            f"MWo3 size mismatch: header says 0x{expected_file_size:X}, "
            f"file is 0x{len(prg):X}"
        )

    if ctor_end < ctor_start or ((ctor_end - ctor_start) % 4) != 0:
        raise ValueError("invalid MWo3 constructor table range")

    return {
        "kind": kind,
        "load_addr": load_addr,
        "text_size": text_size,
        "data_size": data_size,
        "bss_size": bss_size,
        "ctor_start": ctor_start,
        "ctor_end": ctor_end,
        "name": name,
    }


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
    e_phentsize = u16(elf, 0x2A)
    e_phnum = u16(elf, 0x2C)

    if e_phentsize < 0x20:
        raise ValueError(f"unexpected program-header size: {e_phentsize}")

    load_addr = int(mw["load_addr"])
    full_mem_size = len(prg) + int(mw["bss_size"])

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

    new_offset = congruent_append_offset(len(elf), p_align, load_addr)
    if new_offset > len(elf):
        elf.extend(b"\0" * (new_offset - len(elf)))

    elf.extend(prg)

    # Point the existing reservation at the MWo3 file image.
    p32(elf, ph + 0x04, new_offset)
    p32(elf, ph + 0x10, len(prg))

    # The reservation may originally be RW-only because it held no file data.
    # Mark it executable for analysis/recompilation.
    p32(elf, ph + 0x18, old_flags | PF_X)

    args.output_elf.parent.mkdir(parents=True, exist_ok=True)
    args.output_elf.write_bytes(elf)

    ctor_count = (int(mw["ctor_end"]) - int(mw["ctor_start"])) // 4
    ctor_off = int(mw["ctor_start"]) - load_addr
    ctors = [
        struct.unpack_from("<I", prg, ctor_off + n * 4)[0]
        for n in range(ctor_count)
    ]

    print(f"MWo3 name:          {mw['name']}")
    print(f"MWo3 kind:          {mw['kind']}")
    print(f"load address:       0x{load_addr:08X}")
    print(f"file image size:    0x{len(prg):X}")
    print(f"BSS size:           0x{int(mw['bss_size']):X}")
    print(f"mapped memory size: 0x{full_mem_size:X}")
    print(f"mapped end:         0x{load_addr + full_mem_size:08X}")
    print(f"constructor table:  0x{int(mw['ctor_start']):08X}-0x{int(mw['ctor_end']):08X}")
    print("constructors:       " + ", ".join(f"0x{x:08X}" for x in ctors))
    print()
    print(f"patched PT_LOAD:    #{i}")
    print(f"old filesz:         0x{old_filesz:X}")
    print(f"old offset:         0x{old_offset:X}")
    print(f"new offset:         0x{new_offset:X}")
    print(f"alignment:          0x{p_align:X}")
    print(f"output:             {args.output_elf}")


if __name__ == "__main__":
    main()
