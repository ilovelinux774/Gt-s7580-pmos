# J4+ Linux 3.18 prototype and backports

This directory records the J4+ (SM-J415F / msm8917) Linux 3.18.120 prototype,
its reproducible source inputs, and the first system-call backport.

## Build basis

- Kernel tree: `wonderful-mobile/android_kernel_samsung_msm8917_64`
- Branch: `3.18.120-A11`
- Pinned commit: `4fcc1090e5e07e7ccf877793e1bfe151641424bf`
- Architecture: ARM64; GCC 4.9 Android cross compiler
- Baseline config: `build2.config` (the extracted, working Build 2 config)
- Config fragment: `systemd.config.fragment`

The prototype starts from the extracted Build 2 ARM64 config, enables the
existing `CONFIG_CPUSETS`, `CONFIG_FHANDLE`, and `CONFIG_MEMCG` implementations,
adds `CONFIG_MEMBARRIER=y`, and clears only the unavailable
`CONFIG_EXTRA_FIRMWARE` entries. `build.sh` now also disables the legacy
framebuffer/MDSS display stack, its panel-specific Samsung options, and unused
LCD/backlight class drivers for a headless build. It keeps the virtual-terminal
console and Adreno KGSL GPU support; DRM/KMS was already disabled. The checked-in
`prototype.config` remains the previously tested config and still has display
enabled; the next build output `.config` is the headless config and must be
compiled and boot-tested separately. Patches 0001 and 0002 provide the
`membarrier()` syscall and the small
MEMCG/SWAP-disabled preprocessor guard needed to build this vendor tree with
`CONFIG_MEMCG=y`; they do not add cgroup v2.

## Patches

Apply in numeric order from the root of the pinned source tree:

1. `patches/0000-gcc-wrapper-python3.patch` — host build compatibility only;
   this old wrapper otherwise requires Python 2.
2. `patches/0001-membarrier-syscall.patch` — Linux 4.5-era shared membarrier
   syscall/UAPI, using syscall number 283 for native ARM64 and 389 for the
   AArch32 compat table. The implementation supports QUERY and SHARED only;
   it calls `synchronize_sched()` when multiple CPUs are online.
3. `patches/0002-memcg-config-guard.patch` — makes the existing vendor memcg
   swappiness conditional safe when `CONFIG_MEMCG_SWAP` is off, so the already
   present memcg implementation builds with the requested config.

This 3.18 kernel artifact is a separate source-backport prototype, not the
current Arch Linux ARM target. It contains no Arch rootfs and is not intended to
run the rolling current systemd. The full-system target now pairs the signed
rolling Arch Linux ARM rootfs with the mainline 7.1.3 J4+ kernel in
`ci/j4primelte/k510/`.

## Build and test status

- Rebuilt `Image.gz dtbs` with patches 0001/0002 and
  `CONFIG_MEMBARRIER=y`, `CONFIG_CPUSETS=y`, `CONFIG_FHANDLE=y`, and
  `CONFIG_MEMCG=y`; compile/link succeeded. The old tree emits existing
  Makefile/Kconfig warnings.
- Output artifacts are also packaged as `artifacts/j4primelte-k318-prototype.tar.gz`
  (kernel, selected DTB, tested config, and checksums); package SHA-256 is
  `a81828176afc69de751f78e2d0369cc9667391b1484e26d903de5a7ec1fcc210`. Payload SHA-256:
  `Image.gz` `5175e5c7bda169b1268d1159d3205d41e688c67c7e8f9cc715cbeb6fabdc8e1c`;
  J4+ `r06` DTB `bd413438c5f8f216e736a3ecfb1d6f2082a5992e6f520cb3ce89dd80257f9763`.
- `CONFIG_EXTRA_FIRMWARE` remains empty because the referenced A300 firmware
  files were not present in the source tree. This is a build workaround, not
  proof that the firmware configuration is correct for every device function.
- The candidate uses its vendor `USB_G_ANDROID` composite gadget (including
  built-in RNDIS implementation), not generic `USB_ETH`/ChipIdea config symbols;
  gadget enumeration and networking are not yet proven on the phone.
- Not boot-tested on hardware. No partitions were flashed or modified.

The extracted Build 2 config contains `CONFIG_CGROUPS=y` and the freezer, but
has CPUSETS, MEMCG, and FHANDLE disabled; the prototype config now enables
those existing 3.18 implementations. To reproduce the build, use a clean source
checkout at the pinned commit and a GCC 4.9 cross toolchain, then run:

```sh
KERNEL_TREE=/path/to/clean/kernel \
CROSS_COMPILE=/path/to/toolchain/bin/aarch64-linux-android- \
ci/j4primelte/k318/build.sh
```

A `bc` executable must be available in `PATH` (the build used the BusyBox `bc`
applet). The output directory defaults to `out/j4primelte-k318`; set `OUT` to
change it. Further systemd compatibility work still needs source-level,
independently tested backports, especially the newer cgroup interfaces, and
validation against actual userspace requirements.

## Headless build (display stack removed)

`build.sh` now also disables `CONFIG_FB` and with it the whole legacy MDSS
framebuffer/panel path (`FB_MSM*`), `MSM_DBA`, the LCD/backlight class drivers,
`SUPPORT_WHITERGB` and the Samsung incell touchscreen driver. DRM/KMS was never
enabled in this config, so nothing replaces the framebuffer: the kernel has no
display output at all, and `console=tty0`/`fbcon=disable` become irrelevant.
The virtual-terminal console and Adreno KGSL stay enabled.

Two build-time consequences were found by actually compiling the tree, not by
inspection:

1. `CONFIG_TOUCHSCREEN_SEC_INCELL_TS` must be disabled too.
   `sec_ts_fb_notifier_cb()` is `#ifdef CONFIG_FB` and calls
   `sec_ts_input_open()`, which is only defined under `USE_OPEN_CLOSE`. With FB
   off the call site disappears and the definition becomes an unused-function
   warning, which this tree's `gcc-wrapper.py` upgrades to a hard error
   (`error, forbidden warning: sec_ts.c:2234`).
2. `CONFIG_EXTRA_FIRMWARE` must stay empty. `build2.config` points it at
   `a300_pm4.fw a300_pfp.fw`, which are not in the tree, so the `firmware`
   target fails regardless of the display options.

Local verification on 2026-11-13 (x86_64 host, GCC 4.9.4 Android prebuilt from
`LineageOS/android_prebuilts_gcc_linux-x86_aarch64_aarch64-linux-android-4.9`,
installed by hand because this sandbox cannot run `apt-get`). Note that this
local pass used a slightly different config sequence than `build.sh` and came
out with `MEMCG`/`CPUSETS`/`FHANDLE` cleared by `olddefconfig`; the exact
`build.sh` sequence was re-run afterwards and keeps all four enabled, so trust
`build.sh` (and the CI job) as the reference:

- Pinned tree `4fcc1090e5e07e7ccf877793e1bfe151641424bf` plus patches 0000-0002
  and the headless `scripts/config` edits: `make ARCH=arm64 Image.gz dtbs`
  exited 0. No forbidden warnings.
- `Image.gz` sha256 `3aa56865a416f1f9be76512c0487db86be05ecfef6e18865a2909de2851051ba`
- `msm8917-sec-j4primelte-sea-open-r06.dtb` sha256
  `bd413438c5f8f216e736a3ecfb1d6f2082a5992e6f520cb3ce89dd80257f9763`
  (identical to the recorded Build 2 DTB)
- Resulting `.config`: 148 symbols differ from `build2.config`; the only
  functional losses are the display/touch ones above plus `CONFIG_BT_HCI_SMD`,
  which no longer exists in the tree and was already being dropped by
  `olddefconfig`. `CONFIG_MEMBARRIER/CPUSETS/FHANDLE/MEMCG` are unchanged.
- The packaged artifacts are in `artifacts/j4primelte-k318-headless/`.

This kernel is **not boot-tested** and no BOOT image has been packed from it
yet.
