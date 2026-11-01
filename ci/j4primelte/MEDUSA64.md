# J4+ SM-J415F: Project Medusa ARM64 experiment

**Status: experimental, not proven to boot.** This is the old downstream
Project Medusa Linux **3.18.120** kernel, not mainline and not an automatic GPU
acceleration fix. The first artifact is a debug-shell BOOT image, not a full
postmarketOS/rootfs image. The normal procedure uses OrangeFox, not fastboot;
no flashing should be attempted without separate review and owner approval.

The previously tested M1 firmware-RAM image `063e5792...` failed on the phone:
lk2nd/Tux, brief solid blue with no text, then reboot. **Do not retry it.** A
similar flash reportedly occurred once with a downstream kernel; this neither
proves nor excludes hardware, bootloader, firmware or kernel causes. The public
M1 release is marked FAILED ON DEVICE and must not be retried.

## Inputs and boundaries

* Kernel: `ProjectMedusaAndroid/android_kernel_samsung_msm8917_64` commit
  `a5ca5d08ebbb559944e6661f54e7decdcd167bd6` with the ARM64 J4+ defconfig.
  The source's own `build_kernel.sh` hardcodes `ARCH=arm`; we do not use it.
* Compiler: `ProjectMedusaAndroid/aarch64-gcc-4.9-brillo-m10-release` commit
  `42f42a5b9afe816b5b5dd93ed9ea5642bdf3d0e4`. Pinned inputs and checksums are
  in `medusa64-sources.env`. Host package versions are not fully pinned, so
  this is a traceable recipe, not a promise of bit-identical builds everywhere.
* Android v0 donor: the SHA-checked #37 BOOT image used only as a source of the
  existing pmOS initramfs and root UUID. This does **not** require flashing or
  rolling back to #37. Repacking copies its initramfs byte for byte.
* The existing recorded rootfs is ARMv7; its initramfs busybox and musl loader
  are 32-bit ARM EABI. The generated kernel enables `CONFIG_COMPAT` and
  `CONFIG_COMPAT_BINFMT_ELF`, but that is necessary, not proof it will run.
  Existing ARM32 kernel modules cannot be loaded as ARM64 modules.
* The compiled source normally appends **six** J4 SEA revision DTBs, in
  filesystem-dependent order. Packaging explicitly chooses only the recorded
  Rev06 DTB, verifies its Qualcomm SoC/board/PMIC IDs and places it immediately
  after a single gzip stream. Do not use this Rev06-only image on another
  revision without a separate device/loader validation.
* `BT_HCI_SMD` uses the existing in-tree adaptation from `kernel-bt.py`; WLAN,
  KGSL, MDSS and the Android USB gadget are built in. Their runtime behavior,
  firmware availability and compatibility are not established by config alone.

## Touchscreen firmware safeguard

The untouched Medusa 64 tree contains a different J4+ touchscreen firmware
file than the pinned 32-bit Q source: `447237c4...` vs `749efaba...` in Intel
HEX form. In the saved working-kernel log, the Q version matched the controller
and its update was skipped. The Medusa 64 touchscreen driver can otherwise
update firmware during probe, including a forced-update path. A BOOT-only test
should not silently introduce that change.

`medusa64-stage.py` checks exact source hashes, uses the pinned Q touchscreen
firmware in the new kernel, and sets **only the selected Rev06** DTB's
`sec,bringup` to `1`. In the audited driver this returns before the automatic
firmware-update-on-probe path. This is not a claim that all possible other
hardware/firmware writes are impossible, that calibration cannot change, or
that touchscreen behavior has been tested. Rebuilding will fail if its expected
source or firmware hashes change.

## Build and validation

The existing dispatchable `.github/workflows/build-mainline.yml` offers
`medusa64_diagnostic=true` on the approved branch. Selecting it skips mainline
and builds a standalone downstream ARM64 kernel. It does not publish a release
or flash a phone. The job must run exclusively on
`pmos-j4primelte-20260927`. `test-medusa64.py` checks staging, failure guards,
board IDs, the debug flag, donor/ramdisk integrity, ARM64 load size and
single-DTB packing. `medusa64-repack.py` additionally validates the final config,
checks the chosen DTB and Android v0 payload ID, and reports the actual SHA-256
values. `verify-medusa64.py` independently parses the image and checks the
embedded Q touchscreen firmware, load layout, selected DTB and donor initramfs.
CI output should still be reviewed before publication.

The donor's second-stage debug shell precedes its normal rootfs discovery,
filesystem resize and mount. Do not run `pmos_continue_boot` while diagnosing:
continuing can write the root filesystem. Even a debug BOOT can have early udev,
USB, hardware, firmware or `misc` side effects, and a bad image can bootloop.
The built-in Samsung debug code also contains paths that write the `param`
partition (for example on a recovery reboot); recovery itself may write. This
workflow does not modify `/vendor` or make a
rootfs image, but it cannot guarantee runtime code has no persistent effects.
Replacing the existing rootfs would erase the live LXQt, Wi-Fi and audio state.

## Graphics

This kernel exposes Qualcomm's downstream KGSL interface, not the mainline
MSM DRM/KMS stack. A Mesa KGSL backend exists, but compatibility with this
specific Linux 3.18 KGSL, Adreno 308, current ARMv7 pmOS userspace and its X11
presentation path is unverified. A KGSL device node alone is not evidence of
accelerated LXQt. Validate boot and the existing userspace first; investigate
Mesa/ION/dma-buf compatibility separately, without modifying `/vendor`.
The source includes ION and KGSL dma-buf import, but this alone does not show
that a particular Mesa build can allocate/share buffers or present frames.
The [Freedreno-on-Android presentation](https://indico.freedesktop.org/event/4/contributions/198/attachments/128/188/XDC_2023_Lucas_Fryzek_Freedreno_on_Android.pdf)
explains a KGSL backend. A [Redmi 4X report](https://github.com/JulioFerrero/pocket-foundry/issues/7)
shows a dma-buf/GL failure in a different 3.18 device and software stack; that
package targets newer GPUs, so it is a warning, not a diagnosis of this phone.
