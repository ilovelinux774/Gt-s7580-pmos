# J4+ lk2nd + M1 debug BOOT (experimental)

The first J415F `lk2nd-m1-debug.img` was **device-tested and failed**: Tux,
then blue screen and reboot. It is not evidence that Linux, USB, Wi-Fi, display
or rootfs boots, and must not be presented as working. The three earlier M1
images also failed. A separate hardware-untested HLOS handoff diagnostic is
documented in [diag/README.md](diag/README.md).

The combined image replaces **only BOOT** using OrangeFox; it contains no
rootfs image and requires no fastboot. It must not be flashed to recovery or any
other partition. The debug flag asks the unchanged #37 initramfs to stop in a
shell before its normal rootfs mount; early hooks and hardware-side effects
remain unverified. Do not run `pmos_continue_boot` during a preservation test:
normal continuation can
mount, check, resize or write the existing filesystem. Replacing the rootfs in
a later milestone would erase the existing LXQt, Wi-Fi and audio state.
Special recovery-message paths in the pinned lk2nd source can write `misc` even
though `SAFE_MODE` blocks certain device-info and RPMB writes. Preserve a way to
return to OrangeFox/Odin, and do not claim that all writes are impossible.

## Layout and source

* BOOT is the recorded `/dev/mmcblk0p23`, 32 MiB. Nothing here changes GPT.
* The outer Android v0 image contains an ARM32 lk2nd binary, **no initramfs**,
  and the attached J4+ outer device tree. Samsung's primary bootloader still
  loads and fixes this tree; lk2nd is not a replacement for Samsung aboot.
* The remainder of the first **1 MiB** is zero padding. At byte 1,048,576 is the
  complete inner Android v0 image: the M1 ARM64 kernel and its DTB plus #37's
  unchanged initramfs, with `pmos.debug-shell`. This offset comes from the pinned
  Geckyn fork. Do **not** use modern upstream's 512-KiB split here.
* Source: `https://github.com/geckyn/lk2nd.git`, commit
  `7bc82f56d1339b1d081c48ad3c664ec6119257af`, target `msm8917-secondary`.
  The outer DTS in this directory is derived from that project, with the
  recorded PMIC ID and the `klog_region` node required by the inspected
  J415FXXU6BVJ1 Samsung loader's flag-dependent DTB fixup.
* The secondary loader ignores Android header load addresses: it computes the
  kernel, DTB and ramdisk locations from its detected DDR base, then asks secure
  firmware to enter AArch64. A valid packed image is not proof this occurs.
* Flashing *any other complete BOOT image* later replaces lk2nd as well. The
  logical split is in lk2nd's in-memory partition table, not an on-disk GPT edit.

## Build and checks

The mainline GitHub Actions job first builds and independently checks the M1
kernel and donor-based debug image, then builds the pinned lk2nd and calls
`build-lk2nd.sh`. The job uses Ubuntu 22.04's GCC 10 bare-metal ARM toolchain,
`-fcommon` for the old LK sources, device-tree-compiler and `libfdt-dev`. The
script removes its own generated LK build directory so old objects from another
compiler cannot silently be reused. Output records source and tool versions.

`test-lk2nd.py` uses synthetic fixtures to check the layout, image IDs, split,
size, debug arguments, donor ramdisk identity, gzip/DTB boundaries and several
corruption cases. `lk2nd-image.py` re-parses the finished image independently
and checks the compiled outer DTS properties. These checks do **not** run the
primary bootloader, secure monitor, Linux kernel or device hardware.

During development, additional private emulation exercised selected instruction
paths from the exact Samsung aboot supplied by the device owner and the built
secondary lk2nd, with simulated IDs, memory, storage and hardware stubs. Those
simulations are separate from the Actions tests, do not include the proprietary
aboot in this repository, and do not replace a real device test.
