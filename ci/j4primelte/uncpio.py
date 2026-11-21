#!/usr/bin/env python3
"""Extract a (gzipped) cpio-newc initramfs, preserving symlinks, modes and owners.

    python3 ci/j4primelte/uncpio.py initramfs.gz outdir/

Written because the reference BOOT image's ramdisk is a plain gzip cpio archive
with symlinked top-level directories (``bin -> usr/bin``), and a naive unpack
silently turns those into broken regular files.
"""

from __future__ import annotations

import argparse
import gzip
import os
import sys
from pathlib import Path

S_IFMT = 0o170000
S_IFDIR = 0o040000
S_IFLNK = 0o120000
S_IFREG = 0o100000


def read_newc(data: bytes):
    """Yield (name, mode, uid, gid, data, link_target_or_None, mtime)."""
    off = 0
    while off + 110 <= len(data):
        magic = data[off : off + 6]
        if magic != b"070701":
            raise SystemExit(f"not a newc archive: magic {magic!r} at {off}")
        fields = [
            int(data[off + 6 + i * 8 : off + 6 + (i + 1) * 8], 16) for i in range(13)
        ]
        (ino, mode, uid, gid, _nlink, mtime, size, _dmaj, _dmin, _rmaj, _rmin,
         namesize, _check) = fields
        name_start = off + 110
        name = data[name_start : name_start + namesize - 1].decode("utf-8", "replace")
        header_end = name_start + namesize
        header_end += (-header_end) % 4
        body = data[header_end : header_end + size]
        off = header_end + size
        off += (-off) % 4
        if name == "TRAILER!!!":
            return
        yield name, mode, uid, gid, body, mtime


def extract(blob: bytes, out: Path) -> int:
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
    out.mkdir(parents=True, exist_ok=True)
    count = 0
    for name, mode, uid, gid, body, mtime in read_newc(blob):
        rel = os.path.normpath(name.lstrip("/"))
        if rel in (".", ""):
            continue
        target = out / rel
        ftype = mode & S_IFMT
        if ftype == S_IFDIR:
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        if ftype == S_IFLNK:
            os.symlink(body.decode(), target)
        elif ftype == S_IFREG:
            target.write_bytes(body)
            os.chmod(target, mode & 0o7777)
        else:
            # device nodes and fifos cannot be created unprivileged; record them
            (out / ".unpacked-specials").open("a").write(f"{rel} mode={oct(mode)}\n")
            continue
        try:
            os.chown(target, uid, gid, follow_symlinks=False)
        except (PermissionError, OSError):
            pass
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive")
    parser.add_argument("out_dir")
    args = parser.parse_args()
    count = extract(Path(args.archive).read_bytes(), Path(args.out_dir))
    print(f"uncpio: extracted {count} entries into {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
