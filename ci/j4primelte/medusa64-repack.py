#!/usr/bin/env python3
"""Pack one audited Medusa ARM64 Rev06 DTB and the existing pmOS initramfs.

The result is an UNTESTED, debug-shell-only Android v0 BOOT image. This is not
lk2nd: the Samsung bootloader selects its appended downstream DTB directly.
Nothing here touches a phone, an installed rootfs, or its /vendor partition.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import struct
import subprocess
import zlib

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('j4_mainline_repack', HERE / 'mainline-repack-boot.py')
r = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r)
DONOR_SHA = '3ba9fa06e0d410dbd2e1f8e700090ab1eebc68f491a40dcfde53d805373ae80e'
RAMDISK_SHA = 'f2c18fcea31fed9a1af3eb5bec700b0d8f1d5006382537a394a5ea2c32b68ef6'
ROOT_UUID = '86e7262b-b628-4b89-8a6e-e96ef9b1aab6'
TOUCH_NODE = '/soc/i2c@78b6000/touchscreen@48'
LOADER_KERNEL = 0x80080000
LOADER_TAGS = 0x83400000
LOADER_RAMDISK = 0x83600000
REQUIRED_Y = ('COMPAT COMPAT_BINFMT_ELF BUILD_ARM64_APPENDED_DTB_IMAGE BUILD_ARM64_KERNEL_COMPRESSION_GZIP '
              'SEC_J4PRIMELTE_PROJECT MACH_J4PRIMELTE_SEA_OPEN SYSVIPC '
              'DEVPTS_MULTIPLE_INSTANCES DEVTMPFS DEVTMPFS_MOUNT BLK_DEV_INITRD '
              'RD_GZIP BINFMT_ELF BINFMT_SCRIPT EXT4_FS USB_G_ANDROID USB_MSM_OTG '
              'PRONTO_WLAN BT_HCI_SMD MSM_KGSL FIRMWARE_IN_KERNEL '
              'TOUCHSCREEN_SEC_INCELL_TS').split()
REQUIRED_NOT_Y = ('ANDROID_PARANOID_NETWORK SECURITY_SELINUX DM_VERITY FRAMEBUFFER_CONSOLE').split()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_config(path):
    lines = set(path.read_text().splitlines())
    for option in REQUIRED_Y:
        if f'CONFIG_{option}=y' not in lines:
            raise ValueError(f'expected CONFIG_{option}=y in the final kernel config')
    for option in REQUIRED_NOT_Y:
        if f'CONFIG_{option}=y' in lines:
            raise ValueError(f'unexpected CONFIG_{option}=y in the final kernel config')


def fdtget(path, node, prop, kind='x'):
    try:
        cp = subprocess.run(['fdtget', '-t', kind, str(path), node, prop],
                            check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f'cannot read DTB property {node}:{prop}') from exc
    return cp.stdout.strip()


def cells(path, node, prop):
    try:
        return [int(v, 16) for v in fdtget(path, node, prop).split()]
    except ValueError as exc:
        raise ValueError(f'invalid cells in DTB property {node}:{prop}') from exc


def validate_dtb(path):
    blob = path.read_bytes()
    if len(blob) < 40 or len(blob) > r.DTB_WINDOW:
        raise ValueError('DTB has an unexpected size')
    magic, size = struct.unpack_from('>II', blob)
    if magic != 0xd00dfeed or size != len(blob):
        raise ValueError('DTB header/total size is invalid')
    if cells(path, '/', 'qcom,board-id') != [8, 6]:
        raise ValueError('DTB is not the recorded J415F board Rev06')
    if cells(path, '/', 'qcom,msm-id') != [303, 0, 308, 0, 309, 0]:
        raise ValueError('DTB has unexpected supported Qualcomm SoC IDs')
    if cells(path, '/', 'qcom,pmic-id') != [0x10019, 0, 0, 0]:
        raise ValueError('DTB has an unexpected PMIC ID')
    if cells(path, TOUCH_NODE, 'sec,bringup') != [1]:
        raise ValueError('automatic touchscreen probe update is not disabled')
    if fdtget(path, TOUCH_NODE, 'sec,firmware_name', 's') != 'tsp_sec/s6d7at01a01_j4prime.fw':
        raise ValueError('unexpected touchscreen firmware path')
    return blob


def validate_image_gz(blob):
    if len(blob) < 18 or blob[:3] != b'\x1f\x8b\x08' or blob[3] != 0:
        raise ValueError('expected a plain gzip ARM64 Image')
    d = zlib.decompressobj(31)
    try:
        raw = d.decompress(blob) + d.flush()
    except zlib.error as exc:
        raise ValueError('invalid gzip ARM64 Image') from exc
    if not d.eof or d.unused_data:
        raise ValueError('Image.gz must be one complete gzip stream with no appended DTBs')
    if len(raw) < 64 or raw[56:60] != b'ARM\x64':
        raise ValueError('missing ARM64 Image header')
    offset, image_size, flags = struct.unpack_from('<QQQ', raw, 8)
    if offset != 0x80000 or flags & 1:
        raise ValueError('unsupported ARM64 text offset or endianness')
    if image_size < len(raw) or LOADER_KERNEL + image_size > LOADER_TAGS:
        raise ValueError('kernel effective image, including BSS, overlaps loader DTB area')
    return raw, offset, image_size, flags


def build(donor_data, image_gz, dtb, donor_sha=DONOR_SHA, ramdisk_sha=RAMDISK_SHA):
    if sha(donor_data) != donor_sha:
        raise ValueError('donor boot image SHA-256 mismatch')
    base = r.BootImage(donor_data)
    if base.page_size != 2048 or base.second_size:
        raise ValueError('unexpected donor page size or second payload')
    if sha(base.ramdisk) != ramdisk_sha:
        raise ValueError('donor initramfs SHA-256 mismatch')
    roots = [v for v in r.split_args(base.cmdline) if v.startswith('pmos_root_uuid=')]
    if roots != [f'pmos_root_uuid={ROOT_UUID}']:
        raise ValueError('donor does not have the recorded root UUID exactly once')
    if 'pmos.debug-shell' in r.split_args(base.cmdline):
        raise ValueError('donor already requests debug shell')
    raw, offset, image_size, flags = validate_image_gz(image_gz)
    payload = image_gz + dtb
    if r.find_appended_dtb(payload) != len(image_gz):
        raise ValueError('DTB must immediately follow exactly one gzip stream')
    r.check_gzip_boundary(payload, len(image_gz))
    addr = r.arm64_kernel_addr(base.kernel_addr, payload)
    if addr != LOADER_KERNEL:
        raise ValueError('ARM64 kernel header address does not match recorded loader placement')
    if LOADER_TAGS + len(dtb) + 0x8c8 > LOADER_RAMDISK:
        raise ValueError('DTB plus loader fixup space overlaps initramfs area')
    if LOADER_RAMDISK + len(base.ramdisk) > 0x85000000:
        raise ValueError('initramfs overlaps the recorded low-memory debug area')
    # Unlike the smaller ARM32 donor, this ARM64 image's effective size can
    # overlap the donor's *header* DTB/initramfs addresses. The recorded aboot
    # overrides those addresses, but also write its observed safe placements to
    # the header so that the advertised layout is internally non-overlapping.
    safe_header = bytearray(donor_data)
    struct.pack_into('<I', safe_header, 20, LOADER_RAMDISK)
    struct.pack_into('<I', safe_header, 32, LOADER_TAGS)
    safe_base = r.BootImage(bytes(safe_header))
    cmdline = r.with_cmdline(base.cmdline, ['pmos.debug-shell'])
    result = safe_base.repack(payload, cmdline=cmdline, kernel_addr=addr)
    check = r.BootImage(bytes(result.data))
    if (check.kernel != payload or check.ramdisk != base.ramdisk or
            check.second != base.second or check.name != base.name or
            check.kernel_addr != addr or check.ramdisk_addr != LOADER_RAMDISK or
            check.tags_addr != LOADER_TAGS or check.page_size != base.page_size):
        raise ValueError('Android boot payload/header round-trip mismatch')
    if r.split_args(check.cmdline).count('pmos.debug-shell') != 1:
        raise ValueError('debug shell flag missing or duplicated')
    digest = hashlib.sha1()
    for part in (check.kernel, check.ramdisk, check.second):
        digest.update(part)
        digest.update(struct.pack('<I', len(part)))
    if bytes(check.data[576:608]) != digest.digest().ljust(32, b'\0'):
        raise ValueError('Android v0 payload ID is wrong')
    report = {
        'status': 'UNTESTED experimental BOOT; not proof of a working phone or GPU',
        'donor_sha256': sha(donor_data), 'donor_ramdisk_sha256': sha(base.ramdisk),
        'kernel_image_gz_sha256': sha(image_gz), 'kernel_image_sha256': sha(raw),
        'dtb_sha256': sha(dtb), 'kernel_payload_sha256': sha(payload),
        'boot_image_sha256': sha(bytes(check.data)), 'boot_bytes': len(check.data),
        'root_uuid': ROOT_UUID, 'ramdisk_copied_byte_for_byte': True,
        'boot_header_kernel_addr': hex(check.kernel_addr),
        'boot_header_dtb_addr': hex(check.tags_addr),
        'boot_header_initramfs_addr': hex(check.ramdisk_addr),
        'recorded_loader_overrides_header_addresses': True,
        'simulated_loader_kernel_addr': hex(LOADER_KERNEL),
        'simulated_loader_dtb_addr': hex(LOADER_TAGS),
        'simulated_loader_initramfs_addr': hex(LOADER_RAMDISK),
        'arm64_text_offset': hex(offset), 'arm64_effective_size_bytes': image_size,
        'arm64_flags': hex(flags), 'cmdline': check.cmdline,
        'debug_shell_requested': True,
        'packaging_writes_phone_rootfs_or_vendor': False,
    }
    return bytes(check.data), report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--donor', type=Path, required=True)
    ap.add_argument('--image-gz', type=Path, required=True)
    ap.add_argument('--dtb', type=Path, required=True)
    ap.add_argument('--config', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--report', type=Path, required=True)
    a = ap.parse_args()
    validate_config(a.config)
    dtb = validate_dtb(a.dtb)
    image, report = build(a.donor.read_bytes(), a.image_gz.read_bytes(), dtb)
    report['config_sha256'] = sha(a.config.read_bytes())
    report['selected_dtb'] = 'j4primelte SEA open Rev06'
    report['touchscreen_automatic_probe_update_disabled'] = True
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(image)
    a.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
