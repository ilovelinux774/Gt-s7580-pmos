#!/usr/bin/env python3
"""Read the facts about a binary that decide whether it starts on a 3.18 kernel.

Two things are read, both straight out of the ELF image, never inferred from a
file name:

* the ``PT_INTERP`` string, i.e. which C library the binary links against;
* the ``.note.ABI-tag`` note (``NT_GNU_ABI_TAG``), which glibc's build writes
  from ``--enable-kernel``/the architecture minimum. The dynamic loader
  compares that number against ``uname`` at startup and prints

      Fatal: kernel too old

  and exits, before ``main``, when the running kernel is older. It is a
  build-time constant: no configuration file can lower it.

musl has no such note; it uses the syscalls the kernel provides.

The note is the only trustworthy source for the floor. A binary that links
glibc but carries no note is reported as ``None`` (unknown), never as "fine".

Pure stdlib, py3.8+, so CI can run it without installing anything.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

PT_NOTE = 4
PT_INTERP = 3
SHT_NOTE = 7
NT_GNU_ABI_TAG = 1
GNU_NOTE_NAME = b"GNU\x00"
ELF_OSABI_LINUX = 3

EM_ARM = 40
EM_AARCH64 = 183
EM_X86_64 = 62
MACHINES = {
    EM_ARM: "arm32",
    EM_AARCH64: "aarch64",
    EM_X86_64: "x86_64",
}


@dataclass
class ElfFacts:
    """What the loader will care about, or why it could not be read."""

    path: str
    readable: bool = False
    reason: str = ""
    bits: int = 0
    machine: str = ""
    interpreter: str | None = None
    #: (major, minor, patch) from .note.ABI-tag, or None when absent
    abi_floor: tuple[int, int, int] | None = None
    #: every note name/type pair found, for diagnostics
    notes: list[tuple[str, int]] = field(default_factory=list)

    @property
    def kind(self) -> str:
        if not self.readable:
            return "unreadable"
        if self.interpreter is None:
            return "static"
        lowered = self.interpreter.lower()
        if "ld-musl" in lowered:
            return "musl"
        if "ld-linux" in lowered or "ld64.so" in lowered:
            return "glibc"
        return f"other ({self.interpreter})"

    @property
    def floor_text(self) -> str:
        if self.abi_floor is None:
            return "unknown"
        return ".".join(str(part) for part in self.abi_floor)


def parse_version(text: str) -> tuple[int, int, int]:
    """'3.18.120-j4-medusa64-debug+' -> (3, 18, 120)."""
    head = text.strip().split("-")[0].split("+")[0]
    parts: list[int] = []
    for chunk in head.split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
        if len(parts) == 3:
            break
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _parse_notes(blob: bytes) -> list[tuple[str, int, bytes]]:
    """Walk one note segment/section. Returns (name, type, descriptor)."""
    found: list[tuple[str, int, bytes]] = []
    offset = 0
    total = len(blob)
    while offset + 12 <= total:
        namesz, descsz, ntype = struct.unpack_from("<III", blob, offset)
        offset += 12
        if namesz > 4096 or descsz > 4096:
            break
        name_bytes = blob[offset : offset + namesz]
        offset += (namesz + 3) & ~3
        desc = blob[offset : offset + descsz]
        offset += (descsz + 3) & ~3
        name = name_bytes.rstrip(b"\x00").decode("utf-8", "replace")
        found.append((name, ntype, desc))
        if offset > total:
            break
    return found


def _abi_floor_from_notes(notes: list[tuple[str, int, bytes]]) -> tuple[int, int, int] | None:
    for name, ntype, desc in notes:
        if ntype != NT_GNU_ABI_TAG or name != "GNU" or len(desc) < 16:
            # GNU property notes (NT_GNU_PROPERTY_TYPE_0) are not ABI tags.
            continue
        os_id, major, minor, subminor = struct.unpack_from("<IIII", desc, 0)
        if os_id != 0:  # 0 = Linux
            continue
        return int(major), int(minor), int(subminor)
    return None


def read_elf(path: str | Path) -> ElfFacts:
    """Read interpreter + ABI tag. Never raises: failures land in ``reason``."""
    path = Path(path)
    facts = ElfFacts(path=str(path))
    try:
        with path.open("rb") as handle:
            head = handle.read(64)
            if len(head) < 52 or head[:4] != b"\x7fELF":
                facts.reason = "not an ELF file"
                return facts
            ei_class, ei_data = head[4], head[5]
            if ei_class not in (1, 2) or ei_data not in (1, 2):
                facts.reason = f"unsupported ELF class/data {ei_class}/{ei_data}"
                return facts
            is64 = ei_class == 2
            little = ei_data == 1
            endian = "<" if little else ">"
            facts.bits = 64 if is64 else 32
            (e_machine,) = struct.unpack_from(endian + "H", head, 18)
            facts.machine = MACHINES.get(e_machine, f"machine {e_machine}")
            if is64:
                (e_phoff,) = struct.unpack_from(endian + "Q", head, 32)
                (e_shoff,) = struct.unpack_from(endian + "Q", head, 40)
                e_phentsize, e_phnum, e_shentsize, e_shnum = struct.unpack_from(
                    endian + "HHHH", head, 54
                )
            else:
                (e_phoff,) = struct.unpack_from(endian + "I", head, 28)
                (e_shoff,) = struct.unpack_from(endian + "I", head, 32)
                e_phentsize, e_phnum, e_shentsize, e_shnum = struct.unpack_from(
                    endian + "HHHH", head, 42
                )
            facts.readable = True

            segments: list[tuple[int, int, int]] = []
            if e_phnum and e_phentsize >= (56 if is64 else 32) and e_phoff:
                handle.seek(e_phoff)
                table = handle.read(e_phentsize * e_phnum)
                for index in range(e_phnum):
                    base = index * e_phentsize
                    if is64:
                        if base + 56 > len(table):
                            break
                        p_type, _flags = struct.unpack_from(endian + "II", table, base)
                        p_offset, _vaddr, _paddr, p_filesz = struct.unpack_from(
                            endian + "QQQQ", table, base + 8
                        )
                    else:
                        if base + 32 > len(table):
                            break
                        p_type = struct.unpack_from(endian + "I", table, base)[0]
                        p_offset, _vaddr, _paddr, p_filesz = struct.unpack_from(
                            endian + "IIII", table, base + 4
                        )
                    segments.append((p_type, p_offset, p_filesz))
                for p_type, p_offset, p_filesz in segments:
                    if p_type != PT_INTERP or not p_filesz:
                        continue
                    handle.seek(p_offset)
                    facts.interpreter = (
                        handle.read(p_filesz).rstrip(b"\x00").decode("utf-8", "replace")
                    )
                for p_type, p_offset, p_filesz in segments:
                    if p_type != PT_NOTE or not p_filesz:
                        continue
                    handle.seek(p_offset)
                    for name, ntype, desc in _parse_notes(handle.read(p_filesz)):
                        facts.notes.append((name, ntype))
                        if ntype == NT_GNU_ABI_TAG and name == "GNU" and len(desc) >= 16:
                            floor = _abi_floor_from_notes([(name, ntype, desc)])
                            if floor is not None and facts.abi_floor is None:
                                facts.abi_floor = floor

            # Some linkers keep the note only in a section. Fall back to the
            # section table, which is also where a stripped binary still keeps
            # .note.ABI-tag in practice.
            if facts.abi_floor is None and e_shnum and e_shentsize and e_shoff:
                handle.seek(e_shoff)
                sections = handle.read(e_shentsize * e_shnum)
                for index in range(e_shnum):
                    base = index * e_shentsize
                    if base + e_shentsize > len(sections):
                        break
                    if is64:
                        sh_type, = struct.unpack_from(endian + "I", sections, base + 4)
                        sh_offset, sh_size = struct.unpack_from(
                            endian + "QQ", sections, base + 24
                        )
                    else:
                        sh_type, = struct.unpack_from(endian + "I", sections, base + 4)
                        sh_offset, sh_size = struct.unpack_from(
                            endian + "II", sections, base + 16
                        )
                    if sh_type != SHT_NOTE or not sh_size or sh_size > 1 << 16:
                        continue
                    handle.seek(sh_offset)
                    floor = _abi_floor_from_notes(_parse_notes(handle.read(sh_size)))
                    if floor is not None:
                        facts.abi_floor = floor
                        break
    except (OSError, struct.error) as exc:
        facts.readable = False
        facts.reason = str(exc)
    return facts


def floor_exceeds(floor: tuple[int, int, int] | None, kernel: tuple[int, int, int]) -> bool:
    """True when the binary refuses to start on ``kernel``."""
    return floor is not None and floor > kernel


if __name__ == "__main__":  # tiny debug helper
    import argparse
    import json

    parser = argparse.ArgumentParser(description="print ELF floor facts")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    out = []
    for item in args.paths:
        facts = read_elf(item)
        out.append(
            {
                "path": facts.path,
                "kind": facts.kind,
                "interpreter": facts.interpreter,
                "abi_floor": facts.floor_text,
                "machine": facts.machine,
                "bits": facts.bits,
                "readable": facts.readable,
                "reason": facts.reason,
            }
        )
    print(json.dumps(out, indent=2))
