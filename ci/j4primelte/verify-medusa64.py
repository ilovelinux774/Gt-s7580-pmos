#!/usr/bin/env python3
"""Independently verify the experimental Medusa ARM64 Android v0 BOOT image.

This is a static check, not a phone test. It intentionally does not import the
packer. It also confirms the built kernel contains the known Q touchscreen
firmware, and that the selected Rev06 DTB disables automatic probe updates.
"""
from pathlib import Path
import argparse
import hashlib
import json
import struct
import subprocess
import zlib

DONOR_SHA = '3ba9fa06e0d410dbd2e1f8e700090ab1eebc68f491a40dcfde53d805373ae80e'
RAMDISK_SHA = 'f2c18fcea31fed9a1af3eb5bec700b0d8f1d5006382537a394a5ea2c32b68ef6'
TOUCH_Q_SHA = 'e55b70d1ea7c7c18270e395f191df4a8587eb95db012ec9935954de2f005e8db'
GPU_PM4_SHA = 'f78c57e879e96a2c0ceeea31dbd30f7f1d4526f9c876e80481c654f08daaecd3'
GPU_PFP_SHA = 'b6e66400d46d52dc343d370b94362d564a765994bda47dc026961dd47a9ca7a9'
ROOT = 'pmos_root_uuid=86e7262b-b628-4b89-8a6e-e96ef9b1aab6'
PAGE, MAX_BOOT = 2048, 32 * 1024 * 1024
KERNEL, TAGS, INITRD = 0x80080000, 0x83400000, 0x83600000
TOUCH = '/soc/i2c@78b6000/touchscreen@48'


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def parse_boot(blob):
    require(len(blob) >= PAGE and blob[:8] == b'ANDROID!', 'bad Android header')
    ks, ka, rs, ra, ss, sa, tags, page, version, osv = struct.unpack_from('<10I', blob, 8)
    require(page == PAGE and version == 0 and ss == 0, 'unexpected page/version/second')
    align = lambda n: (n + page - 1) // page * page
    kstart, rstart = page, page + align(ks)
    length = rstart + align(rs)
    require(length == len(blob) and length <= MAX_BOOT, 'invalid image length/padding')
    require(ks and rs and kstart + ks <= len(blob) and rstart + rs <= len(blob), 'bad payload bounds')
    kernel, ramdisk = blob[kstart:kstart + ks], blob[rstart:rstart + rs]
    require(not any(blob[kstart + ks:rstart]) and not any(blob[rstart + rs:length]), 'nonzero payload padding')
    digest = hashlib.sha1()
    for part in (kernel, ramdisk, b''):
        digest.update(part)
        digest.update(struct.pack('<I', len(part)))
    require(blob[576:608] == digest.digest().ljust(32, b'\0'), 'Android v0 payload ID mismatch')
    first = blob[64:576].split(b'\0', 1)[0]
    extra = blob[608:1632].split(b'\0', 1)[0]
    cmdline = (first + extra).decode('ascii').split()
    return {'kernel': kernel, 'ramdisk': ramdisk, 'kernel_addr': ka,
            'ramdisk_addr': ra, 'tags': tags, 'second_addr': sa,
            'os_version': osv, 'name': blob[48:64], 'cmdline': cmdline}


def fdt(path, node, prop, typ='x'):
    try:
        return subprocess.check_output(['fdtget', '-t', typ, str(path), node, prop],
                                       text=True, stderr=subprocess.PIPE).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f'missing or invalid DTB property {node}:{prop}') from exc


def verify(image: bytes, donor: bytes, kernel_gz: bytes, dtb_path: Path,
           config: str, touch: bytes, pm4: bytes, pfp: bytes):
    require(sha(donor) == DONOR_SHA, 'unexpected donor SHA')
    old, new = parse_boot(donor), parse_boot(image)
    require(sha(old['ramdisk']) == RAMDISK_SHA, 'unexpected donor initramfs')
    require(new['ramdisk'] == old['ramdisk'], 'initramfs differs from donor')
    require(new['name'] == old['name'] and new['os_version'] == old['os_version'] and
            new['second_addr'] == old['second_addr'], 'unexpected unrelated header changes')
    require((new['kernel_addr'], new['tags'], new['ramdisk_addr']) == (KERNEL, TAGS, INITRD),
            'unsafe advertised load addresses')
    require(old['cmdline'].count(ROOT) == 1 and 'pmos.debug-shell' not in old['cmdline'],
            'unexpected donor root UUID/debug flag')
    require(new['cmdline'] == old['cmdline'] + ['pmos.debug-shell'], 'unexpected boot arguments')
    dtb = dtb_path.read_bytes()
    require(len(dtb) >= 40 and dtb[:4] == b'\xd0\x0d\xfe\xed' and
            struct.unpack_from('>I', dtb, 4)[0] == len(dtb), 'invalid DTB length')
    require(new['kernel'] == kernel_gz + dtb, 'not exactly one gzip Image followed by selected DTB')
    require(len(kernel_gz) >= 18 and kernel_gz[:4] == b'\x1f\x8b\x08\x00', 'unexpected gzip header')
    dec = zlib.decompressobj(31)
    try:
        raw = dec.decompress(kernel_gz) + dec.flush()
    except zlib.error as exc:
        raise ValueError('invalid kernel gzip') from exc
    require(dec.eof and not dec.unused_data and len(raw) >= 64 and raw[56:60] == b'ARM\x64',
            'invalid or concatenated ARM64 gzip')
    offset, effective, flags = struct.unpack_from('<QQQ', raw, 8)
    require(offset == 0x80000 and not (flags & 1), 'unsupported ARM64 Image header')
    require(len(raw) <= effective and KERNEL + effective <= TAGS, 'kernel overlaps FDT')
    require(TAGS + len(dtb) + 0x8c8 <= INITRD and INITRD + len(new['ramdisk']) <= 0x85000000,
            'FDT/initramfs overlaps another load or recorded low-memory debug area')
    require(sha(touch) == TOUCH_Q_SHA and len(touch) == 120144,
            'wrong Q touchscreen firmware bytes')
    require(raw.count(touch) == 1, 'pinned Q touchscreen firmware not present exactly once')
    require(sha(pm4) == GPU_PM4_SHA and len(pm4) == 9220, 'wrong A300 PM4 firmware bytes')
    require(sha(pfp) == GPU_PFP_SHA and len(pfp) == 1156, 'wrong A300 PFP firmware bytes')
    require(raw.count(pm4) == 1 and raw.count(pfp) == 1,
            'pinned A300 firmware is not present exactly once in the kernel Image')
    require(fdt(dtb_path, '/', 'qcom,board-id') == '8 6', 'DTB is not board Rev06')
    require(fdt(dtb_path, '/', 'qcom,msm-id') == '12f 0 134 0 135 0', 'unexpected SoC IDs')
    require(fdt(dtb_path, '/', 'qcom,pmic-id') == '10019 0 0 0', 'unexpected PMIC ID')
    require(fdt(dtb_path, TOUCH, 'sec,bringup') == '1', 'automatic touch probe update is enabled')
    require(fdt(dtb_path, TOUCH, 'sec,firmware_name', 's') == 'tsp_sec/s6d7at01a01_j4prime.fw',
            'unexpected touchscreen firmware path')
    cfg = set(config.splitlines())
    for opt in ('COMPAT', 'COMPAT_BINFMT_ELF', 'BUILD_ARM64_APPENDED_DTB_IMAGE',
                'BUILD_ARM64_KERNEL_COMPRESSION_GZIP', 'SEC_J4PRIMELTE_PROJECT',
                'MACH_J4PRIMELTE_SEA_OPEN', 'BLK_DEV_INITRD', 'RD_GZIP',
                'DEVTMPFS', 'DEVTMPFS_MOUNT', 'SYSVIPC', 'DEVPTS_MULTIPLE_INSTANCES',
                'EXT4_FS', 'USB_G_ANDROID', 'USB_MSM_OTG', 'PRONTO_WLAN',
                'BT_HCI_SMD', 'MSM_KGSL', 'FW_LOADER', 'FIRMWARE_IN_KERNEL', 'TOUCHSCREEN_SEC_INCELL_TS'):
        require(f'CONFIG_{opt}=y' in cfg, f'required config CONFIG_{opt} not enabled')
    require('CONFIG_EXTRA_FIRMWARE="a300_pm4.fw a300_pfp.fw"' in cfg,
            'expected A300 builtin firmware list missing')
    require('CONFIG_EXTRA_FIRMWARE_DIR="firmware"' in cfg, 'unexpected firmware source directory')
    require('CONFIG_LOCALVERSION="-j4-medusa64-gpufw-debug"' in cfg, 'unexpected local version')
    for opt in ('ANDROID_PARANOID_NETWORK', 'SECURITY_SELINUX', 'DM_VERITY', 'FRAMEBUFFER_CONSOLE'):
        require(f'CONFIG_{opt}=y' not in cfg, f'unexpected config CONFIG_{opt}=y')
    return {'result': 'PASS: static checks only; physical boot/GPU/USB untested',
            'boot_sha256': sha(image), 'image_gz_sha256': sha(kernel_gz),
            'dtb_sha256': sha(dtb), 'initramfs_sha256': sha(new['ramdisk']),
            'touch_firmware_sha256': sha(touch), 'config_sha256': sha(config.encode()),
            'gpu_pm4_firmware_sha256': sha(pm4), 'gpu_pfp_firmware_sha256': sha(pfp),
            'gpu_firmware_occurs_once_in_raw_image': True,
            'arm64_effective_size_bytes': effective, 'boot_size_bytes': len(image),
            'initramfs_order': 'pinned donor scripts separately inspected; hardware effects untested'}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for opt in ('image', 'donor', 'image-gz', 'dtb', 'config', 'touch-q-bin', 'gpu-pm4', 'gpu-pfp'):
        ap.add_argument('--' + opt, type=Path, required=True)
    ap.add_argument('--report', type=Path, required=True)
    a = ap.parse_args()
    report = verify(a.image.read_bytes(), a.donor.read_bytes(), a.image_gz.read_bytes(),
                    a.dtb, a.config.read_text(), a.touch_q_bin.read_bytes(),
                    a.gpu_pm4.read_bytes(), a.gpu_pfp.read_bytes())
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
