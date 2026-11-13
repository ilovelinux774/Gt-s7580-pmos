#!/usr/bin/env python3
"""Build/validate the Qualcomm QCDT v3 container expected by legacy aboot.

Qualcomm LK does not treat a bare FDT appended after Image.gz as a device-tree
image. For these boot.img v0 images the QCDT table follows the ramdisk; its
byte length is stored in the vendor-reused dt_size word at header offset 40.
"""
from __future__ import annotations

import argparse
import itertools
import struct
import sys
from pathlib import Path

QCDT_MAGIC = b"QCDT"
FDT_MAGIC = 0xD00DFEED
FDT_HEADER_SIZE = 40
FDT_BEGIN_NODE = 1
FDT_END_NODE = 2
FDT_PROP = 3
FDT_NOP = 4
FDT_END = 9


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) & ~(boundary - 1)


def parse_root_cells(blob: bytes) -> dict[str, list[int]]:
    """Read root-node uint32-cell properties from a flattened device tree."""
    if len(blob) < FDT_HEADER_SIZE:
        raise ValueError("DTB is shorter than an FDT header")
    fields = struct.unpack_from(">10I", blob)
    magic, total, struct_off, strings_off, _rsvmap_off, version, _last, _bootcpu, strings_size, struct_size = fields
    if magic != FDT_MAGIC:
        raise ValueError("input does not start with FDT magic")
    if total < FDT_HEADER_SIZE or total > len(blob):
        raise ValueError(f"invalid FDT totalsize {total} for {len(blob)} input bytes")
    if version < 16:
        raise ValueError(f"unsupported FDT version {version}")
    if struct_off + struct_size > total or strings_off + strings_size > total:
        raise ValueError("FDT structure or strings block runs past totalsize")

    struct_block = blob[struct_off:struct_off + struct_size]
    strings = blob[strings_off:strings_off + strings_size]
    pos = 0
    depth = -1
    root: dict[str, list[int]] = {}
    saw_root = False
    saw_end = False

    while pos + 4 <= len(struct_block):
        token = struct.unpack_from(">I", struct_block, pos)[0]
        pos += 4
        if token == FDT_BEGIN_NODE:
            end = struct_block.find(b"\0", pos)
            if end < 0:
                raise ValueError("unterminated FDT node name")
            name = struct_block[pos:end]
            pos = align(end + 1, 4)
            depth += 1
            if depth == 0:
                if name:
                    raise ValueError("FDT root node has a nonempty name")
                saw_root = True
        elif token == FDT_END_NODE:
            depth -= 1
            if depth < -1:
                raise ValueError("unbalanced FDT end-node token")
        elif token == FDT_PROP:
            if pos + 8 > len(struct_block):
                raise ValueError("truncated FDT property header")
            size, nameoff = struct.unpack_from(">II", struct_block, pos)
            pos += 8
            if pos + size > len(struct_block):
                raise ValueError("FDT property data runs past structure block")
            if nameoff >= len(strings):
                raise ValueError("FDT property name offset is invalid")
            name_end = strings.find(b"\0", nameoff)
            if name_end < 0:
                raise ValueError("unterminated FDT property name")
            name = strings[nameoff:name_end].decode("ascii")
            value = struct_block[pos:pos + size]
            pos = align(pos + size, 4)
            if depth == 0 and name in ("qcom,msm-id", "qcom,board-id", "qcom,pmic-id"):
                if len(value) % 4:
                    raise ValueError(f"{name} is not a uint32-cell array")
                root[name] = list(struct.unpack(f">{len(value) // 4}I", value))
        elif token == FDT_NOP:
            continue
        elif token == FDT_END:
            saw_end = True
            break
        else:
            raise ValueError(f"unknown FDT structure token 0x{token:08x}")

    if not saw_root or not saw_end:
        raise ValueError("FDT has no complete root structure")
    required = ("qcom,msm-id", "qcom,board-id", "qcom,pmic-id")
    missing = [name for name in required if name not in root]
    if missing:
        raise ValueError("DTB is missing root matching properties: " + ", ".join(missing))
    if len(root["qcom,msm-id"]) == 0 or len(root["qcom,msm-id"]) % 2:
        raise ValueError("qcom,msm-id must contain chipset/revision pairs")
    if len(root["qcom,board-id"]) == 0 or len(root["qcom,board-id"]) % 2:
        raise ValueError("qcom,board-id must contain board/subtype pairs")
    if len(root["qcom,pmic-id"]) != 4:
        raise ValueError("qcom,pmic-id must contain exactly four cells")
    return root


def make_qcdt(dtb: bytes, page_size: int = 4096) -> bytes:
    if page_size <= 0 or page_size & (page_size - 1):
        raise ValueError("page size must be a positive power of two")
    ids = parse_root_cells(dtb)
    msm_pairs = list(zip(ids["qcom,msm-id"][::2], ids["qcom,msm-id"][1::2]))
    board_pairs = list(zip(ids["qcom,board-id"][::2], ids["qcom,board-id"][1::2]))
    pmic = ids["qcom,pmic-id"]

    # Match the Qualcomm dtbTool v3 contract: a 12-byte header, ten uint32s
    # per match record, a zero end-of-table word, then a page-aligned FDT.
    matches = list(itertools.product(msm_pairs, board_pairs))
    if not matches:
        raise ValueError("DTB has no Qualcomm match tuples")
    dtb_padded = dtb + bytes(align(len(dtb), page_size) - len(dtb))
    table_len = align(12 + 40 * len(matches) + 4, page_size)
    entries = bytearray()
    dtb_offset = table_len
    for (chipset, soc_rev), (board_id, subtype) in matches:
        entries += struct.pack("<10I", chipset, board_id, subtype, soc_rev,
                               *pmic, dtb_offset, len(dtb_padded))
    header = QCDT_MAGIC + struct.pack("<II", 3, len(matches))
    table = header + entries + struct.pack("<I", 0)
    table += bytes(table_len - len(table))
    image = table + dtb_padded
    validate_qcdt(image, page_size)
    return image


def validate_qcdt(blob: bytes, page_size: int = 4096) -> dict[str, int]:
    if page_size <= 0 or page_size & (page_size - 1):
        raise ValueError("page size must be a positive power of two")
    if len(blob) < 16 or blob[:4] != QCDT_MAGIC:
        raise ValueError("missing QCDT magic/header")
    version, count = struct.unpack_from("<II", blob, 4)
    record_size = {1: 20, 2: 24, 3: 40}.get(version)
    if record_size is None:
        raise ValueError(f"unsupported QCDT version {version}")
    if count == 0:
        raise ValueError("QCDT table contains no records")
    end_entries = 12 + count * record_size
    if end_entries + 4 > len(blob):
        raise ValueError("QCDT index table is truncated")
    if struct.unpack_from("<I", blob, end_entries)[0] != 0:
        raise ValueError("QCDT end-of-table marker is missing")

    first_offset = None
    first_size = None
    for idx in range(count):
        rec = struct.unpack_from(f"<{record_size // 4}I", blob, 12 + idx * record_size)
        dt_offset, dt_size = rec[-2:]
        if dt_offset % page_size or dt_size == 0 or dt_size % page_size:
            raise ValueError(f"QCDT record {idx} has unaligned DTB offset/size")
        if dt_offset + dt_size > len(blob):
            raise ValueError(f"QCDT record {idx} points beyond the table payload")
        if struct.unpack_from(">I", blob, dt_offset)[0] != FDT_MAGIC:
            raise ValueError(f"QCDT record {idx} does not point to an FDT")
        totalsize = struct.unpack_from(">I", blob, dt_offset + 4)[0]
        if totalsize < FDT_HEADER_SIZE or totalsize > dt_size:
            raise ValueError(f"QCDT record {idx} has an invalid FDT totalsize")
        if first_offset is None:
            first_offset, first_size = dt_offset, dt_size
        elif (dt_offset, dt_size) != (first_offset, first_size):
            raise ValueError("this packer expects all aliases to reference one board DTB")
    if first_offset is None or first_offset < align(end_entries + 4, page_size):
        raise ValueError("QCDT DTB payload overlaps its index")
    if len(blob) != first_offset + first_size:
        raise ValueError("QCDT image has unexpected trailing or missing bytes")
    return {"version": version, "entries": count, "dtb_offset": first_offset,
            "dtb_size": first_size, "total_size": len(blob)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dtb", type=Path, help="compiled board DTB")
    parser.add_argument("--out", type=Path, help="write QCDT v3 image here")
    parser.add_argument("--page-size", type=int, default=4096)
    parser.add_argument("--check", type=Path, help="validate an existing QCDT image")
    args = parser.parse_args()
    try:
        if args.check:
            info = validate_qcdt(args.check.read_bytes(), args.page_size)
        else:
            if not args.dtb or not args.out:
                parser.error("--dtb and --out are required unless --check is used")
            image = make_qcdt(args.dtb.read_bytes(), args.page_size)
            args.out.write_bytes(image)
            info = validate_qcdt(image, args.page_size)
        print("QCDT v{version}: {entries} match records, DTB at +0x{dtb_offset:x}"
              " ({dtb_size} bytes), total {total_size} bytes".format(**info))
        return 0
    except (OSError, ValueError, struct.error) as exc:
        print(f"qcdt: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
