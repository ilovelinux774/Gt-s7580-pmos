#!/usr/bin/env python3
"""Write a deterministic gzip-compressed `newc` archive of an initramfs tree."""
from __future__ import annotations
import gzip
import os
import stat
import sys
from pathlib import Path


def align4(value: int) -> int:
    return (value + 3) & ~3


def build(root: Path) -> bytes:
    root = root.resolve()
    result = bytearray()
    ino = 1
    paths = [root, *sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())]
    for path in paths:
        rel = "." if path == root else path.relative_to(root).as_posix()
        name = rel.encode("utf-8") + b"\0"
        st = path.lstat()
        mode = st.st_mode
        if stat.S_ISREG(mode):
            data = path.read_bytes()
        elif stat.S_ISLNK(mode):
            data = os.readlink(path).encode("utf-8")
        elif stat.S_ISDIR(mode):
            data = b""
        else:
            raise ValueError(f"unsupported initramfs file type: {rel}")
        # This initramfs needs no hard-link semantics; emit independent inode
        # records so the archive remains correct if source packages share files.
        fields = (ino, mode, 0, 0, 1, int(st.st_mtime), len(data),
                  0, 0, 0, 0, len(name), 0)
        result.extend(b"070701")
        for field in fields:
            result.extend(f"{field:08x}".encode("ascii"))
        result.extend(name)
        result.extend(b"\0" * (align4(len(result)) - len(result)))
        result.extend(data)
        result.extend(b"\0" * (align4(len(result)) - len(result)))
        ino += 1
    name = b"TRAILER!!!\0"
    fields = (ino, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, len(name), 0)
    result.extend(b"070701")
    for field in fields:
        result.extend(f"{field:08x}".encode("ascii"))
    result.extend(name)
    result.extend(b"\0" * (align4(len(result)) - len(result)))
    return bytes(result)


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <root-directory> <output.cpio.gz>", file=sys.stderr)
        return 2
    root, output = Path(sys.argv[1]), Path(sys.argv[2])
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(gzip.compress(build(root), compresslevel=9, mtime=0))
    print(f"created {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
