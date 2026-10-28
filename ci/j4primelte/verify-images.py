#!/usr/bin/env python3
"""Validate the image formats and produce a separate initramfs-debug boot image.

For boot header v0, cmdline is outside the kernel/ramdisk SHA-1 ID calculation.
Only cmdline header bytes change; the compiled kernel and ramdisk stay identical.
"""
from pathlib import Path
import hashlib
import json
import struct
import sys

folder = Path(sys.argv[1])
boot_path = folder / 'boot.img'
boot = bytearray(boot_path.read_bytes())
assert boot[:8] == b'ANDROID!', 'Not an Android boot image'
ksize, kaddr, rsize, raddr, ssize, saddr, tags, page, version, osver = struct.unpack_from('<10I', boot, 8)
assert version == 0, f'Unsupported boot header version: {version}'
assert page == 2048, f'Unexpected boot page size: {page}'
assert kaddr == 0x80008000 and raddr == 0x82000000, 'Unexpected load addresses'
assert 0 < ksize < len(boot) and 0 < rsize < len(boot), 'Empty/invalid payload'
align = lambda size: (size + page - 1) // page * page
assert len(boot) >= page + align(ksize) + align(rsize) + align(ssize), 'Truncated boot image'
# A guardrail, NOT a claim that the user's physical BOOT partition is this size.
assert len(boot) <= 32 * 1024 * 1024, 'Boot image exceeds the 32 MiB build guardrail'
cmd = bytes(boot[64:576]).split(b'\0', 1)[0] + bytes(boot[608:1632]).split(b'\0', 1)[0]
assert b'pmos.debug-shell' not in cmd, 'Normal boot unexpectedly stops in initramfs'
debug_cmd = cmd + b' pmos.debug-shell'
assert len(debug_cmd) < 1536, 'Boot cmdline too long'
debug = bytearray(boot)
debug[64:576] = debug_cmd[:512].ljust(512, b'\0')
debug[608:1632] = debug_cmd[512:].ljust(1024, b'\0')
assert debug[page:] == boot[page:], 'Debug image payload changed'
(folder / 'boot-debug.img').write_bytes(debug)

# A third variant, for hunting the slab freelist corruption that panics this
# board after tens of minutes (victims fault in __kmalloc_track_caller with a
# poisoned freelist pointer, and one of them is always init). The parameter and
# /sys/kernel/slab/*/validate only exist with CONFIG_SLUB_DEBUG, which this
# vendor defconfig turns off ("# CONFIG_SLUB_DEBUG is not set"); build-kernel.sh
# re-enables it and asserts it, so an image that ignores this one is a build
# failure, not a quiet no-op. panic_on_oops=0 keeps
# the box alive after the first oops, so the report that names the corrupting
# path is not lost along with it.
slub_cmd = cmd + b' slub_debug=FZPU panic_on_oops=0'
assert len(slub_cmd) < 1536, 'Boot cmdline too long'
assert b'pmos.debug-shell' not in slub_cmd, 'Slab-debug image must still boot'
slub = bytearray(boot)
slub[64:576] = slub_cmd[:512].ljust(512, b'\0')
slub[608:1632] = slub_cmd[512:].ljust(1024, b'\0')
assert slub[page:] == boot[page:], 'Slab-debug image payload changed'
(folder / 'boot-slabdebug.img').write_bytes(slub)

# A fourth image: the CONTROL group, not the fix. The kernel patch defers the bind
# (no mdss_fb_open() call during probe, no takeover from the
# FB_EVENT_FB_REGISTERED notifier, bind at late_initcall()) for every cmdline
# without `,veto`. Appending `veto` restores the vendor behaviour -- the veto runs,
# the bind is refused, the panel stays dark, and the boot survives, which is what
# #33 proved. It exists so that "does the deferred bind work" is a one-variable
# question: same kernel, same ramdisk, same DTB, one token.
# Appending a token to fbcon= cannot be done by tacking bytes onto the cmdline,
# because the whole string is one __setup() argument: it has to be substituted
# inside it, and the substitution must be asserted.
veto_cmd = cmd.replace(b'fbcon=map:0,scrollback:256k',
                       b'fbcon=map:0,scrollback:256k,veto')
assert veto_cmd != cmd, ('the base cmdline no longer carries the exact fbcon= '
                         'argument this variant rewrites; refusing to build an '
                         'image that is just a copy of boot.img')
assert b'pmos.debug-shell' not in veto_cmd, 'control image must boot normally'
assert len(veto_cmd) < 1536, 'Boot cmdline too long'
veto = bytearray(boot)
veto[64:576] = veto_cmd[:512].ljust(512, b'\0')
veto[608:1632] = veto_cmd[512:].ljust(1024, b'\0')
assert veto[page:] == boot[page:], 'Control image payload changed'
(folder / 'boot-veto.img').write_bytes(veto)

rootfs = folder / 'rootfs.img'
with rootfs.open('rb') as f:
    f.seek(1080)
    assert f.read(2) == b'\x53\xef', 'Rootfs is not a raw ext4 filesystem'
    f.seek(512)
    assert f.read(8) != b'EFI PART', 'Rootfs unexpectedly contains GPT'
report = {
    'target': 'Samsung Galaxy J4+ SM-J415F / samsung-j4primelte',
    'hardware_tested': False,
    'boot_header_version': version,
    'page_size': page,
    'boot_image_bytes': len(boot),
    'kernel_bytes': ksize,
    'ramdisk_bytes': rsize,
    'rootfs_bytes': rootfs.stat().st_size,
    'cmdline': cmd.decode(),
    'debug_cmdline': debug_cmd.decode(),
    'slubdebug_cmdline': slub_cmd.decode(),
    'veto_cmdline': veto_cmd.decode(),
    'payload_sha256': hashlib.sha256(boot[page:]).hexdigest(),
    'boot_partition_size_verified_on_phone': False,
}
(folder / 'image-info.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
