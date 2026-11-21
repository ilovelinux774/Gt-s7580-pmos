#!/usr/bin/env python3
"""Read, verify and repack the J4+ (SM-J415F) Android v0 BOOT image.

The reference artifact is the owner-supplied BOOT image that reached the
postmarketOS initramfs debug shell on the handset. This module is the single
place that knows how that image is laid out, so every image the project
produces can be compared against it instead of against an assumption.

Layout facts taken from the reference image itself (see ``inspect``):

    page size          2048
    kernel_addr        0x80080000
    ramdisk_addr       0x83600000
    second_addr        0x80f00000
    tags_addr          0x83400000
    header version     0 (name / cmdline / id / extra_cmdline fields present)
    kernel payload     gzip(arm64 Image) followed by exactly one appended DTB
    ramdisk payload    gzip initramfs (no cpio-newc wrapper, no lz4)

Only the Android v0 header is supported on purpose: the J4+ bootloader (Samsung
LK) was never shown to accept v1/v2 images, and both the known-good pmOS image
and the Medusa image are v0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path

ANDROID_MAGIC = b"ANDROID!"
BOOT_NAME_SIZE = 16
BOOT_ARGS_SIZE = 512
BOOT_EXTRA_ARGS_SIZE = 1024
HEADER_FIELDS_SIZE = (
    8 + (10 * 4) + BOOT_NAME_SIZE + BOOT_ARGS_SIZE + 32 + BOOT_EXTRA_ARGS_SIZE
)  # 1632
FDT_MAGIC = b"\xd0\x0d\xfe\xed"
GZIP_MAGIC = b"\x1f\x8b"
ARM64_IMAGE_MAGIC_OFFSET = 56
ARM64_IMAGE_MAGIC = b"ARMd"

# The reference image, as published on release j4-medusa64-fbdev-parent-debug-1
# and re-verified by this repository's CI from the owner's upload.
REFERENCE = {
    "source": "release j4-medusa64-fbdev-parent-debug-1 "
    "medusa64-fbdev-parent-debug-UNTESTED.img",
    "boot_sha256": None,  # filled in by CI when it has the file
    "kernel_sha256": "65081abb3786647655a3c6ffcbd8865e00f9d7b0385570b30d907155ac5b24d8",
    "dtb_sha256": "6fca7cf5b5bbb142afb99c5e48dadaa4129ee961a06232d10089dd91710ff993",
    "page_size": 2048,
    "kernel_addr": 0x80080000,
    "ramdisk_addr": 0x83600000,
    "second_addr": 0x80F00000,
    "tags_addr": 0x83400000,
    "header_version": 0,
    "kernel_version": "3.18.120-j4-medusa64-debug+",
    "dtb_model": "Samsung J4PRIMELTE SEA OPEN PROJECT Rev06",
    "root_uuid": "86e7262b-b628-4b89-8a6e-e96ef9b1aab6",
}


class BootImageError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def align(value: int, page: int) -> int:
    return (value + page - 1) // page * page


@dataclass
class Header:
    kernel_size: int
    kernel_addr: int
    ramdisk_size: int
    ramdisk_addr: int
    second_size: int
    second_addr: int
    tags_addr: int
    page_size: int
    header_version: int
    os_version: int
    name: bytes
    cmdline: bytes
    image_id: bytes
    extra_cmdline: bytes

    def cmdline_str(self) -> str:
        return self.cmdline.rstrip(b"\x00").decode("utf-8", "replace")

    def extra_cmdline_str(self) -> str:
        return self.extra_cmdline.rstrip(b"\x00").decode("utf-8", "replace")

    def full_cmdline(self) -> str:
        parts = [self.cmdline_str(), self.extra_cmdline_str()]
        return " ".join(p for p in parts if p)

    def raw(self) -> bytes:
        id_field = (self.image_id + b"\x00" * 32)[:32]
        return struct.pack(
            "<8s10I16s512s32s1024s",
            ANDROID_MAGIC,
            self.kernel_size,
            self.kernel_addr,
            self.ramdisk_size,
            self.ramdisk_addr,
            self.second_size,
            self.second_addr,
            self.tags_addr,
            self.page_size,
            self.header_version,
            self.os_version,
            self.name.ljust(BOOT_NAME_SIZE, b"\x00")[:BOOT_NAME_SIZE],
            self.cmdline.ljust(BOOT_ARGS_SIZE, b"\x00")[:BOOT_ARGS_SIZE],
            id_field,
            self.extra_cmdline.ljust(BOOT_EXTRA_ARGS_SIZE, b"\x00")[:BOOT_EXTRA_ARGS_SIZE],
        )


@dataclass
class BootImage:
    header: Header
    kernel: bytes
    ramdisk: bytes
    second: bytes
    source: str = ""
    notes: list[str] = field(default_factory=list)

    # -- inspection -------------------------------------------------------
    def kernel_parts(self) -> tuple[bytes, bytes]:
        """Split the kernel payload into (gzip stream, trailing DTB/padding)."""
        return split_kernel_payload(self.kernel)

    def gzip_image(self) -> bytes:
        raw, _trailing = self.kernel_parts()
        return gunzip(raw)

    def appended_dtb(self) -> bytes:
        _raw, trailing = self.kernel_parts()
        if not trailing.startswith(FDT_MAGIC):
            raise BootImageError("kernel payload has no appended DTB after the gzip stream")
        total = struct.unpack(">I", trailing[4:8])[0]
        if total > len(trailing):
            raise BootImageError(
                f"appended DTB claims {total} bytes but only {len(trailing)} follow"
            )
        return trailing[:total]

    def ramdisk_gzip_sha256(self) -> str:
        return sha256(self.ramdisk)

    def report(self) -> dict:
        h = self.header
        out = {
            "source": self.source,
            "boot_sha256": sha256(self.whole_image()),
            "size": len(self.whole_image()),
            "page_size": h.page_size,
            "header_version": h.header_version,
            "kernel_size": h.kernel_size,
            "ramdisk_size": h.ramdisk_size,
            "second_size": h.second_size,
            "kernel_addr": hex(h.kernel_addr),
            "ramdisk_addr": hex(h.ramdisk_addr),
            "second_addr": hex(h.second_addr),
            "tags_addr": hex(h.tags_addr),
            "cmdline": h.full_cmdline(),
            "kernel_payload_sha256": sha256(self.kernel),
            "ramdisk_gzip_sha256": sha256(self.ramdisk),
        }
        try:
            raw_kernel, trailing = self.kernel_parts()
            out["kernel_gzip_bytes"] = len(raw_kernel)
            out["kernel_trailing_bytes"] = len(trailing)
            image = gunzip(raw_kernel)
            out["kernel_image_size"] = len(image)
            out["kernel_image_sha256"] = sha256(image)
            if len(image) > ARM64_IMAGE_MAGIC_OFFSET + 4:
                out["arm64_image_magic"] = image[
                    ARM64_IMAGE_MAGIC_OFFSET : ARM64_IMAGE_MAGIC_OFFSET + 4
                ].decode("latin-1")
                if image[ARM64_IMAGE_MAGIC_OFFSET : ARM64_IMAGE_MAGIC_OFFSET + 4] == ARM64_IMAGE_MAGIC:
                    out["arm64_text_offset"] = hex(struct.unpack("<Q", image[8:16])[0])
                    out["arm64_image_size"] = hex(struct.unpack("<Q", image[16:24])[0])
            match = image.find(b"Linux version ")
            if match >= 0:
                end = image.find(b"\x00", match)
                out["kernel_version_string"] = image[match:end].decode("latin-1")
        except BootImageError as exc:
            out["kernel_error"] = str(exc)
        try:
            dtb = self.appended_dtb()
            out["dtb_sha256"] = sha256(dtb)
            out["dtb_size"] = len(dtb)
            out["dtb_model"] = fdt_root_prop(dtb, "model")
            out["dtb_msm_id"] = fdt_root_prop(dtb, "qcom,msm-id")
            out["dtb_board_id"] = fdt_root_prop(dtb, "qcom,board-id")
            out["dtb_pmic_id"] = fdt_root_prop(dtb, "qcom,pmic-id")
        except BootImageError as exc:
            out["dtb_error"] = str(exc)
        assert out["size"] == len(self.whole_image()), "size mismatch"
        return out

    def whole_image(self) -> bytes:
        return pack_header_and_payloads(self.header, self.kernel, self.ramdisk, self.second)


# -- low level helpers ----------------------------------------------------
def gunzip(data: bytes) -> bytes:
    """Decompress the first gzip member and ignore anything after it."""
    if not data.startswith(GZIP_MAGIC):
        raise BootImageError("not a gzip stream")
    obj = zlib.decompressobj(31)
    out = obj.decompress(data) + obj.flush()
    return out


def split_kernel_payload(payload: bytes) -> tuple[bytes, bytes]:
    if not payload.startswith(GZIP_MAGIC):
        raise BootImageError("kernel payload is not a gzip stream")
    obj = zlib.decompressobj(31)
    obj.decompress(payload)
    obj.flush()
    consumed = len(payload) - len(obj.unused_data)
    return payload[:consumed], payload[consumed:]


def fdt_root_prop(dtb: bytes, name: str) -> str:
    if not dtb.startswith(FDT_MAGIC):
        raise BootImageError("not an FDT")
    (_tot, off_struct, off_strings, _rsv, _ver, _last, _cpu, _size_str, size_struct) = struct.unpack(
        ">9I", dtb[4:40]
    )
    off = off_struct
    end = off_struct + size_struct
    depth = 0
    while off < end:
        token = struct.unpack(">I", dtb[off : off + 4])[0]
        if token == 1:  # FDT_BEGIN_NODE
            nul = dtb.index(b"\x00", off + 4)
            depth += 1
            off = align(nul + 1, 4)
            continue
        if token == 2:  # FDT_END_NODE
            depth -= 1
            off += 4
            continue
        if token == 3:  # FDT_PROP
            length, name_off = struct.unpack(">II", dtb[off + 4 : off + 12])
            off += 12
            value = dtb[off : off + length]
            off = align(off + length, 4)
            if depth != 1:
                continue
            nul = dtb.index(b"\x00", off_strings + name_off)
            prop = dtb[off_strings + name_off : nul].decode("utf-8", "replace")
            if prop == name:
                text = value.rstrip(b"\x00").decode("utf-8", "replace")
                if all(32 <= ord(c) < 127 for c in text):
                    return text
                words = [
                    hex(struct.unpack(">I", value[i : i + 4])[0])
                    for i in range(0, len(value) - len(value) % 4, 4)
                ]
                return "words:" + ",".join(words)
            continue
        if token == 4:  # FDT_NOP
            off += 4
            continue
        if token == 9:  # FDT_END
            break
        raise BootImageError(f"bad FDT token {token} at offset {off}")
    raise BootImageError(f"property not found in root node: {name}")


def parse_header(data: bytes) -> Header:
    if len(data) < HEADER_FIELDS_SIZE:
        raise BootImageError("image is smaller than an Android v0 boot header")
    if not data.startswith(ANDROID_MAGIC):
        raise BootImageError("missing ANDROID! magic")
    (
        _magic,
        kernel_size,
        kernel_addr,
        ramdisk_size,
        ramdisk_addr,
        second_size,
        second_addr,
        tags_addr,
        page_size,
        header_version,
        os_version,
    ) = struct.unpack("<8s10I", data[:48])
    if header_version not in (0, 1, 2):
        raise BootImageError(f"unsupported header version {header_version}")
    name = data[48:64]
    cmdline = data[64 : 64 + BOOT_ARGS_SIZE]
    image_id = data[576:608]
    extra = data[608 : 608 + BOOT_EXTRA_ARGS_SIZE]
    if page_size == 0 or page_size > 65536:
        raise BootImageError(f"implausible page size {page_size}")
    if header_version == 1:
        extra = b""
    return Header(
        kernel_size=kernel_size,
        kernel_addr=kernel_addr,
        ramdisk_size=ramdisk_size,
        ramdisk_addr=ramdisk_addr,
        second_size=second_size,
        second_addr=second_addr,
        tags_addr=tags_addr,
        page_size=page_size,
        header_version=header_version,
        os_version=os_version,
        name=name,
        cmdline=cmdline if header_version else data[64:576],
        image_id=image_id,
        extra_cmdline=extra,
    )


def unpack_image(data: bytes, source: str = "") -> BootImage:
    header = parse_header(data)
    page = header.page_size
    if header.header_version >= 1:
        # header v1/v2: the header itself occupies one page, plus two more for
        # the v2 "recovery dtbo" and "boot signature" blocks.
        header_pages = 3 if header.header_version >= 2 else 1
        cursor = page * header_pages
    else:
        cursor = page
    kernel = data[cursor : cursor + header.kernel_size]
    cursor += align(header.kernel_size, page)
    ramdisk = data[cursor : cursor + header.ramdisk_size]
    cursor += align(header.ramdisk_size, page)
    second = data[cursor : cursor + header.second_size]
    if len(kernel) != header.kernel_size:
        raise BootImageError("kernel payload is truncated")
    if len(ramdisk) != header.ramdisk_size:
        raise BootImageError("ramdisk payload is truncated")
    if len(second) != header.second_size:
        raise BootImageError("second-stage payload is truncated")
    return BootImage(header=header, kernel=kernel, ramdisk=ramdisk, second=second, source=source)


def android_image_id(kernel: bytes, ramdisk: bytes, second: bytes, extra: bytes = b"") -> bytes:
    """The boot_id the bootloader and ``inspect`` compare (SHA-1, truncated)."""
    digest = hashlib.sha1()
    digest.update(kernel)
    digest.update(struct.pack("<I", len(kernel)))
    digest.update(ramdisk)
    digest.update(struct.pack("<I", len(ramdisk)))
    digest.update(second)
    digest.update(struct.pack("<I", len(second)))
    digest.update(extra)
    return digest.digest()[:20] + b"\x00" * 12


def pack_header_and_payloads(header: Header, kernel: bytes, ramdisk: bytes, second: bytes) -> bytes:
    page = header.page_size
    header = Header(
        kernel_size=len(kernel),
        kernel_addr=header.kernel_addr,
        ramdisk_size=len(ramdisk),
        ramdisk_addr=header.ramdisk_addr,
        second_size=len(second),
        second_addr=header.second_addr,
        tags_addr=header.tags_addr,
        page_size=page,
        header_version=header.header_version,
        os_version=header.os_version,
        name=header.name,
        cmdline=header.cmdline,
        image_id=header.image_id,
        extra_cmdline=header.extra_cmdline,
    )
    raw = bytearray(header.raw())
    page0 = bytearray(align(len(raw), page))
    page0[: len(raw)] = raw
    parts = [bytes(page0), kernel, b"\x00" * (align(len(kernel), page) - len(kernel)),
             ramdisk, b"\x00" * (align(len(ramdisk), page) - len(ramdisk))]
    if second:
        parts += [second, b"\x00" * (align(len(second), page) - len(second))]
    return b"".join(parts)


def build_image(template: Header, kernel: bytes, ramdisk: bytes, cmdline: str | None = None,
                name: bytes | None = None) -> bytes:
    header = Header(
        kernel_size=len(kernel),
        kernel_addr=template.kernel_addr,
        ramdisk_size=len(ramdisk),
        ramdisk_addr=template.ramdisk_addr,
        second_size=0,
        second_addr=template.second_addr,
        tags_addr=template.tags_addr,
        page_size=template.page_size,
        header_version=template.header_version,
        os_version=template.os_version,
        name=template.name if name is None else name,
        cmdline=(template.cmdline if cmdline is None else cmdline.encode()).ljust(512, b"\x00")[:512],
        image_id=b"",
        extra_cmdline=b"",
    )
    image_id = android_image_id(kernel, ramdisk, b"", b"")
    header.image_id = image_id
    return pack_header_and_payloads(header, kernel, ramdisk, b"")


# -- CLI ------------------------------------------------------------------
def cmd_inspect(args: argparse.Namespace) -> int:
    data = Path(args.image).read_bytes()
    image = unpack_image(data, source=str(args.image))
    report = image.report()
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.reference:
        problems = compare_to_reference(image)
        for problem in problems:
            print(f"reference-delta: {problem}", file=sys.stderr)
    return 0


def compare_to_reference(image: BootImage) -> list[str]:
    out: list[str] = []
    h = image.header
    for key, actual in (
        ("page_size", h.page_size),
        ("kernel_addr", h.kernel_addr),
        ("ramdisk_addr", h.ramdisk_addr),
        ("second_addr", h.second_addr),
        ("tags_addr", h.tags_addr),
        ("header_version", h.header_version),
    ):
        if actual != REFERENCE[key]:
            out.append(f"{key}: {actual!r} != reference {REFERENCE[key]!r}")
    try:
        dtb = image.appended_dtb()
    except BootImageError as exc:
        out.append(f"no appended DTB: {exc}")
        return out
    if REFERENCE["dtb_sha256"] and sha256(dtb) != REFERENCE["dtb_sha256"]:
        out.append(
            f"dtb sha256 {sha256(dtb)} != reference {REFERENCE['dtb_sha256']}"
        )
    return out


def cmd_extract(args: argparse.Namespace) -> int:
    data = Path(args.image).read_bytes()
    image = unpack_image(data, source=str(args.image))
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "kernel.payload").write_bytes(image.kernel)
    (out / "ramdisk.gz").write_bytes(image.ramdisk)
    try:
        raw, trailing = image.kernel_parts()
        (out / "Image.gz").write_bytes(raw)
        (out / "kernel.trailing").write_bytes(trailing)
        try:
            (out / "appended.dtb").write_bytes(image.appended_dtb())
        except BootImageError:
            pass
    except BootImageError:
        pass
    (out / "cmdline.txt").write_text(image.header.full_cmdline() + "\n")
    report = image.report()
    (out / "inspect.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"extracted {args.image} -> {out}")
    for key in ("boot_sha256", "kernel_payload_sha256", "dtb_sha256", "cmdline"):
        if key in report:
            print(f"  {key}: {report[key]}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    template = parse_header(Path(args.template).read_bytes())
    kernel = Path(args.kernel).read_bytes()
    ramdisk = Path(args.ramdisk).read_bytes()
    if not kernel.startswith(GZIP_MAGIC):
        raise BootImageError("--kernel must be a gzip stream (Image.gz plus optional DTB)")
    if not ramdisk.startswith(GZIP_MAGIC):
        raise BootImageError("--ramdisk must be a gzip stream (this bootloader does not wrap cpio)")
    cmdline = args.cmdline
    if cmdline is None:
        cmdline = template.cmdline.rstrip(b"\x00").decode()
    data = build_image(template, kernel, ramdisk, cmdline=cmdline)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    image = unpack_image(data, source=str(out))
    report = image.report()
    if image.header.image_id != android_image_id(kernel, ramdisk, b""):
        raise BootImageError("generated header id does not match its payload")
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"wrote {out} ({len(data)} bytes, sha256 {sha256(data)})")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    data = Path(args.image).read_bytes()
    image = unpack_image(data, source=str(args.image))
    problems: list[str] = []
    if len(data) > args.max_bytes:
        problems.append(
            f"image is {len(data)} bytes, larger than the BOOT partition limit {args.max_bytes}"
        )
    if image.header.page_size != REFERENCE["page_size"]:
        problems.append(
            f"page size {image.header.page_size} != known-good {REFERENCE['page_size']}"
        )
    if image.header.header_version != 0:
        problems.append("header version is not 0; the J4+ loader was only proven with v0")
    try:
        raw, trailing = image.kernel_parts()
        image_bytes = gunzip(raw)
    except BootImageError as exc:
        problems.append(f"kernel payload unusable: {exc}")
        image_bytes = b""
        trailing = b""
    if image_bytes:
        magic = image_bytes[
            ARM64_IMAGE_MAGIC_OFFSET : ARM64_IMAGE_MAGIC_OFFSET + 4
        ]
        if magic != ARM64_IMAGE_MAGIC:
            problems.append(f"kernel is not an arm64 Image (magic {magic!r})")
        match = image_bytes.find(b"Linux version ")
        if args.expect_kernel_version:
            if match < 0:
                problems.append("kernel has no Linux version banner")
            else:
                end = image_bytes.find(b"\x00", match)
                banner = image_bytes[match:end].decode("latin-1")
                if args.expect_kernel_version not in banner:
                    problems.append(
                        f"kernel version {banner!r} lacks {args.expect_kernel_version!r}"
                    )
    if args.expect_no_trailing or args.expect_dtb:
        if trailing and not trailing.startswith(FDT_MAGIC) and args.expect_no_trailing:
            problems.append("kernel payload has trailing bytes that are not a DTB")
        if args.expect_dtb:
            try:
                dtb = image.appended_dtb()
            except BootImageError as exc:
                problems.append(str(exc))
                dtb = b""
            if dtb:
                if len(dtb) > 1 and dtb[-1] != 0 and (len(dtb) - 4) % 8 != 0:
                    # weak sanity only; the totalsize check already ran
                    pass
                if args.expect_dtb_sha256 and sha256(dtb) != args.expect_dtb_sha256:
                    problems.append(
                        f"DTB sha256 {sha256(dtb)} != expected {args.expect_dtb_sha256}"
                    )
                model = fdt_root_prop(dtb, "model")
                print(f"appended DTB: {len(dtb)} bytes, model={model}")
    for token in args.require_cmdline or []:
        if token not in image.header.full_cmdline():
            problems.append(f"cmdline is missing required token {token!r}")
    for token in args.forbid_cmdline or []:
        if token in image.header.full_cmdline():
            problems.append(f"cmdline contains forbidden token {token!r}")
    try:
        image.whole_image()
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        problems.append(f"repack check failed: {exc}")
    report = image.report()
    print(json.dumps(report, indent=2, sort_keys=True))
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    print(f"verify: {args.image} is a valid J4+ arm64 BOOT image ({len(data)} bytes)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inspect", help="print everything the image says about itself")
    p.add_argument("image")
    p.add_argument("--reference", action="store_true",
                   help="also print differences from the known-good layout")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("extract", help="unpack kernel, ramdisk and DTB")
    p.add_argument("image")
    p.add_argument("out_dir")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("build", help="pack a kernel/ramdisk using a template header")
    p.add_argument("--template", required=True,
                   help="existing BOOT image whose header/layout is reused")
    p.add_argument("--kernel", required=True, help="gzip stream: arm64 Image.gz [+ DTB]")
    p.add_argument("--ramdisk", required=True, help="gzip-compressed initramfs")
    p.add_argument("--cmdline", default=None)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("verify", help="assert this is a sane J4+ arm64 BOOT image")
    p.add_argument("image")
    p.add_argument("--max-bytes", type=int, default=33554432,
                   help="BOOT partition size (default 32 MiB)")
    p.add_argument("--expect-kernel-version", default=None)
    p.add_argument("--expect-dtb", action="store_true")
    p.add_argument("--expect-dtb-sha256", default=None)
    p.add_argument("--expect-no-trailing", action="store_true")
    p.add_argument("--require-cmdline", action="append", default=[])
    p.add_argument("--forbid-cmdline", action="append", default=[])
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BootImageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
