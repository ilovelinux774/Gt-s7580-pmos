# J4+ / SM-J415F: getting onto a 5.10 kernel for systemd

This is the state of the work, what is proven, and what is not. It is written
so that a later session (or a different person) does not have to rediscover any
of it.

## The requirement, in systemd's own words

From systemd's README (v262, the version postmarketOS ships):

```
⛔ Kernel versions below 5.10 ("minimum baseline") are not supported at all
⚠️  Kernel versions below 5.14 ("recommended baseline") ... taint 'old-kernel'
```

and, for cgroups:

* v256: cgroup v1 ("legacy"/"hybrid") is obsolete and systemd **refuses to
  boot** under it unless `SYSTEMD_CGROUP_ENABLE_LEGACY_FORCE=1` is on the
  cmdline.
* v258: cgroup v1 support is removed.

So the two hard requirements are **(a) kernel ≥ 5.10** and **(b) cgroup v2
unified**. Neither one is about the kernel's bitness.

## What that means for "armv7 vs arm64"

The previous attempt stalled on producing an armv7 (32-bit) 5.10 kernel. That
was the wrong axis:

* systemd checks the kernel *version*, not whether the kernel is 32-bit.
* An **arm64 5.10 kernel with `CONFIG_COMPAT`** runs the existing armv7 Alpine
  rootfs unchanged. That is the standard arrangement on this generation of
  Qualcomm devices.
* Every public 5.10 base with real msm8917/8937 support that could be found is
  arm64. CAF's `la_kernel_msm-5.10` has no `arch/arm/mach-qcom` at all; the
  upstream tree has no msm8917 DTS before ~6.13. An armv7 5.10 for this SoC
  would mean back-porting a whole SoC port into `arch/arm`.

Where the 32-bit question *does* matter is userspace: the pmOS armv7 rootfs
needs `CONFIG_COMPAT`, `CONFIG_COMPAT_32BIT_TIME` and the usual Linux-visibility
symbols. `config-audit.py --mode userspace` checks exactly that.

## What the current 3.18 kernel actually lacks

`config-audit.py` was run against the kernel config captured from the phone
(`j4-diagnostics.tar.gz`, `config.gz`, 3.18.140-pmos-j4-debug, armv7, OpenRC).
It reports **6 required symbols missing** even for the "systemd" set:

    CONFIG_CPUSETS       (cgroup v2 cpu affinity)
    CONFIG_FHANDLE       (open_by_handle_at)
    CONFIG_MEMBARRIER    (systemd core)
    CONFIG_CGROUP_PIDS   (not present in 3.18 at all)
    CONFIG_CGROUP_BPF    (not present in 3.18 at all)
    CONFIG_AUTOFS4_FS

plus no cgroup v2 unified hierarchy (`/sys/fs/cgroup/cgroup.controllers` can
never exist there; cgroup v2 needs kernel ≥ 4.15). This is the diagnosis, with
numbers, in one command:

    python3 ci/j4primelte/k510/config-audit.py /path/to/config.gz.plain --mode systemd

## The pipeline that now exists

`ci/j4primelte/k510/`, driven by `.github/workflows/j4primelte-k510.yml`
(manual dispatch, plus a path-scoped push trigger so it can run from a feature
branch before it is on `main`).

| File | What it does | Proven how |
|---|---|---|
| `sources.env` | all pins: tree, branch, commit, toolchain, donor boot image, boot header addresses | it is the only place pins live |
| `verify-tree.sh` | rejects a tree that is not the pin, and separately **rejects a tree with no msm8917/8937 support** | run in CI: it correctly rejected the current CAF pin |
| `config-audit.py` | checks a `.config` against systemd's documented requirements; `hard`/`systemd`/`userspace` severities, alias-aware, `--json` | run against the real 3.18 config (above) and in CI |
| `repack-boot.c` | Qualcomm v0 Android boot image `inspect` / `build` / `repack`; supports QCDT, raw appended FDT, replacement ramdisk, and sanitized command-line replacement | synthetic layouts and ramdisk replacement round-trip in CI |
| `qcdt.py` | validates the board FDT metadata and creates a Qualcomm QCDT v3 table | unit tests plus C packer integration self-test |
| `fragment-audit.py` | checks every fragment symbol against the tree's own Kconfig **before** compiling, because `scripts/config` accepts nonsense and `olddefconfig` then drops it silently | run on the real tree: found that the fragment's `DRM_PANEL_SAMSUNG_S6D7AT0B01` does not exist there |
| `build-kernel.sh` | defconfig → fragments → force the exact symbols systemd needs → build → stage kernel and board DTB → audit | CI kernel build; device boot still requires a hardware test |

### What `fragment-audit.py` already caught

The fragment carried over from the mainline work asks for
`DRM_PANEL_SAMSUNG_S6D7AT0B01` — the generated panel driver that exists on
`pmos-j4primelte-20260927` together with its own Kconfig entry. The pinned tree
does not have that symbol; it has `DRM_PANEL_SAMSUNG_S6D7AA0`, the same
controller family. Without this check, the build would have succeeded and the
phone would have booted with no panel driver, with nothing in any log saying so.

`ci/j4primelte/k510/mainline/k510-overrides.fragment` enables the in-tree driver
and declares the substitution explicitly (`! SUPERSEDES ...`), so the difference
is visible in the repository rather than buried in a typo. 272 directives,
21602 symbols in the tree, zero unknown — verified locally against a real clone
of the pinned commit.

`repack-boot repack` is the operation bring-up needs: it copies **every** header
field from the phone's own known-good boot image and swaps only the kernel
payload, then reads the result back and refuses to keep it if anything does not
match. Repacking the 3.18 boot image with its own kernel payload is
byte-identical outside the informational SHA-1 id field — that round trip is
checked locally and the tool's behaviour is re-checked on every CI run.

## Which tree: mainline msm89x7, not 5.10

The vendor-kernel prototype is Linux 3.18 (armv7 and arm64), but the Arch
systemd target now uses the **msm89x7 mainline tree this repository's own
`pmos-j4primelte-20260927` branch builds**, at commit `15c904f9ffe0` on
`msm89x7/7.1.3`.

That satisfies the actual requirement (kernel ≥ 5.10) and brings hardware
support that no 5.10 vendor tree would have: MSM8917/8937 support, and the
generated DSI panel driver the J4+ panel needs. The pipeline therefore treats
5.10 as a **floor**, not an equality: `verify-tree.sh` rejects 5.4 and accepts
7.1.3, with tests for both directions.

Pinned:

* tree `https://github.com/msm89x7-mainline/linux.git` @ `15c904f9ffe0`
  (`msm89x7/7.1.3`), probe `arch/arm64/boot/dts/qcom/msm8917.dtsi`
* defconfig `defconfig`
* fragment `ci/j4primelte/k510/mainline/j4mainline.fragment` (358 lines, copied
  from the mainline work, applied through `scripts/config` so a symbol that does
  not exist is reported instead of silently dropped)
* board DTS `ci/j4primelte/k510/mainline/msm8917-samsung-j4primelte.dts` (697
  lines, same provenance) for the case where the pinned tree has no j4primelte
  device tree of its own

What was checked, for the record:

* `CodeLinaro-mirror/la_kernel_msm-5.10` @ `aosp-new/android12-5.10/android12-5.10-2022-07`
  (Linux 5.10.110) — **no msm8917/msm8937/khaje/scuba anywhere**; only genuine
  upstream content. Kept in the file as a shape/documentation pin, and
  `verify-tree.sh` refuses it by name.
* CAF 4.19 trees (`lineage-22.1-k4.19` and friends) — same story, and 4.19 is
  below systemd's baseline anyway.
* Mainline — no `msm8917` device tree before ~6.13; mainline is a separate,
  much larger project and is not what this pipeline is for.
* Vendor 3.18 trees — have the real J4+ IoT DTS (`msm8917-sec-j4primelte-sea-open-r06.dts`,
  board-id `<8 6>`, matching the phone's `androidboot.revision=6`) but are 3.18.
* `msm89x7-mainline/linux` — **the chosen base**. Branches run 6.6 … 7.1.3; there
  is no 5.10 branch and there never needed to be.

The pinned mainline kernel and its debug images already have a successful CI
build. This branch now extends that workflow with a separate full Arch Linux
ARM image: authenticated rolling rootfs, ext4 image, root-switch initramfs,
and QCDT boot image. The new combined rootfs/BOOT workflow has not yet run in
CI or been boot-tested on hardware. The existing `boot-k510.img` and appended-
FDT image remain pmOS diagnostic candidates; `boot-k510-arch-systemd.img` is
the separate Arch/systemd candidate.

## First green build: what it produced

Run [`43382757960`](https://github.com/ilovelinux774/Gt-s7580-pmos/actions/runs/43382757960)
completed in 18m54s: pinned tree validated, fragment audited, kernel compiled,
config audit passed, donor boot image repacked. Artifact `j4-k510-43382757960`
(36 MB) contains `boot-k510.img`, its sha256, `config.full`, `config-audit.json`,
the tree/fragment/donor reports and the packing self-test output.

Getting there fixed four real defects, each found by a mechanism rather than by
luck:

| Symptom | Cause | Now caught by |
|---|---|---|
| bare "exit code 1", no reason | failures were unreadable in the log | `::error` annotations on the run |
| `hashtable.h: 'for' loop declarations only in C99` | 3.18-era `HOSTCFLAGS=-std=gnu89 -fcommon` forced onto a 7.x tree | applied only when `VERSION < 5` |
| "no board defconfig found" | `sources.env` assigned `J4_K510_DEFCONFIG` twice; the empty one won | `check-pins.sh` |
| kernel compiled but `CGROUP_BPF ABSENT` | `CGROUP_BPF` depends on `BPF_SYSCALL`, which the verbatim fragment disables | the audit itself, as a hard failure |

That last one is the exact failure mode this pipeline exists to prevent: a
kernel that builds perfectly, boots maybe, and cannot run systemd for a reason
nothing in the build output mentions.

## Device tree: QCDT and the Medusa kernel-appended layout

The first hardware report was `Can't get fdt` in Odin/Download Mode. A QCDT v3
image after the ramdisk, described by the Qualcomm `dt_size` word at boot-header
offset 40, removed that bootloader error. USB still did not enumerate, so a
second handoff layout is being tested rather than replacing the passing image.

The user supplied a working Project Medusa J4+ boot image for comparison. Its
kernel identifies as `3.18.140-ProjectMedusa-CodeAurora/LA.UM.8.6.r1-05300-89xx.0`.
It contains six concatenated FDTs at the end of the kernel payload, with
`dt_size=0`; the Rev06 FDT matches board `<8 6>` and PMIC ID `<0x10019 0 0 0>`.
The other supplied `boot.txt` is a Kylepro/GT-S7580 image and is not used for
J4+.

The Rev06 Medusa DTB itself is **not copied verbatim** into the 7.1.3 image:
its USB controller uses the downstream compatible `qcom,hsusb-otg`, whereas
this mainline kernel binds `qcom,ci-hdrc` and the mainline 28 nm PHY. A raw
vendor DTB could therefore prevent the mainline USB controller driver from
probing. The mainline board FDT already carries the matching J4+ IDs and
mainline-compatible controller/PHY nodes.

CI now emits two separate BOOT-only candidates from the same kernel, donor
ramdisk and mainline board DTB:

1. `boot-k510.img`: QCDT v3 after ramdisk, `dt_size` set to the QCDT length;
   this is the format that got past the reported FDT error.
2. `boot-k510-appended-dtb.img`: the mainline-compatible raw FDT appended to
the kernel payload, `dt_size=0`, matching the layout observed in the Medusa
boot image. This tests the handoff placement without feeding a 3.18 vendor DTB
to the 7.1.3 drivers.

Both variants are validated by readback and uploaded separately. The synthetic
CI test proves their container layout only; it cannot prove that LK selects the
FDT or that the kernel boots. They are not interchangeable with the Medusa
kernel's actual six downstream FDTs, and neither should be treated as a proven
USB fix until tested on the phone.

### Screen and USB diagnostics

The M1 DTS intentionally keeps a simple framebuffer and omits the panel driver.
A Samsung logo that remains visible is therefore not proof that boot hung; it
may simply be the bootloader's framebuffer left on screen. The first artifact
also enabled configfs RNDIS functions but did not instantiate a gadget, so it
could not guarantee USB enumeration. The diagnostic build now enables built-in
`USB_ETH` + `USB_ETH_RNDIS` (`g_ether`), which should bind automatically when the
MSM8917 ChipIdea UDC probes. The config audit now requires those symbols and the
controller integration to be built in. Host-visible USB enumeration tests the
kernel UDC/gadget path; an IP address still depends on userspace configuring the
network interface and DHCP.

## Standalone systemd debug image (no installed rootfs)

The workflow also emits a separate `boot-k510-systemd-debug.img` specifically
to test systemd as PID 1. It is **not** either of the pmOS-rootfs boot images.
Its initramfs writes boot-stage status directly to framebuffer VT1: kernel/initramfs
entry, systemd startup, USB-controller discovery, gadget binding, and RNDIS setup.
This is independent of pstore and host USB enumeration. A visible status screen
therefore tells us how far userspace got; a stuck Samsung logo with no status
still leaves the kernel/framebuffer handoff unresolved. It depends on the
bootloader-provided simple framebuffer being usable by the mainline kernel.

Its components are:

* the same arm64 7.1.3 kernel and mainline-compatible J4+ FDT;
* a newly built minimal xz-compressed initramfs assembled from Ubuntu 22.04
  arm64 systemd, BusyBox-static, and iproute2 packages;
* `/init` execs systemd directly, with `rd.systemd.unit=emergency.target`;
* systemd starts a dedicated configfs gadget unit for RNDIS (`usb0`,
  `172.16.42.1/24`) and an ACM serial function for a direct shell on
  `/dev/ttyGS0` (normally `/dev/ttyACM0` on the host). If that UDC cannot
  allocate enough endpoints for both functions, the unit retries as RNDIS-only;
  the emergency shell remains on `/dev/console`.

There is no pmOS `/init`, rootfs discovery, rootfs mount, or switch-root in this
diagnostic ramdisk. The separate Arch BOOT artifact uses a different initramfs
and is documented below. The systemd emergency shell is also assigned to
`/dev/console`. RNDIS has no DHCP
server or SSH daemon; for a network test, configure the host side as
`172.16.42.2/24`. The ACM function is the direct interactive debug channel.

This image tests whether the kernel and this packaged systemd initramfs can
start systemd, bind the device controller, configure RNDIS, and reach an
emergency shell. The known J4+ BOOT partition is 33,554,432 bytes; CI refuses
the debug image if it exceeds that limit. CI validates package/image structure
only; phone boot, USB enumeration, and interactive console behavior still need
hardware testing. Keep the known-good recovery image available and flash BOOT
only.

## Full Arch Linux ARM image with systemd as PID 1

The workflow now also builds `boot-k510-arch-systemd.img`, paired with
`rootfs.img`. It uses the signature-verified, rolling Arch Linux ARM aarch64
rootfs and its normal `/sbin/init` (systemd) after the root-switch initramfs.
The minimal root-switch initramfs only discovers `LABEL=ARCHROOT`, mounts ext4,
and executes `switch_root`; it is not a replacement init system. The standalone
Ubuntu/systemd emergency image remains a separate diagnostic artifact.

The rootfs is 2,400 MiB, ext4, labeled `ARCHROOT`, and the BOOT image retains
the J4 donor header while replacing the kernel, ramdisk, and Qualcomm QCDT.
CI verifies the Arch ARM archive signature, AArch64 userspace, rootfs filesystem,
boot-image structure, root label/cmdline, and the 32 MiB BOOT size limit. The
rootfs tarball, disk image, boot image, initramfs, and checksums are attached to
the workflow run. Nothing is written to the handset by CI.

This is now the intended Arch Linux target: current Arch userspace + current
systemd on the pinned Linux 7.1.3 kernel, rather than trying to make the 3.18
prototype run current systemd. A successful workflow proves only build and
packaging consistency; there has not yet been a hardware boot of this combined
Arch/systemd image. The earlier LK2ND-Tux-only observation remains inconclusive.

## Testing the pmOS-rootfs images on the phone (no UART; pstore is best-effort)

1. Keep the known-good Medusa 3.18 `boot.img` and the current QCDT image next
   to you. Flashing is reversible: change BOOT only.
2. To test the newly observed handoff layout with the pmOS initramfs, flash
   `boot-k510-appended-dtb.img` to BOOT only. Do **not** touch SYSTEM. Test the
   QCDT `boot-k510.img` separately; never combine their payloads. For the
   systemd-only debug experiment, use `boot-k510-systemd-debug.img` instead.
3. Record the screen from power-on and observe USB on the host:
   * **Samsung logo remains** → expected with the M1 simple-framebuffer; it does
     not by itself prove a hang.
   * **A USB device/interface enumerates** → the UDC and built-in gadget driver
     bound. A missing `172.16.42.1` after that points to initramfs network setup,
     not FDT selection.
   * **No USB enumeration** → note the timing and screen behavior; possible
     causes include an early kernel stop, UDC/PHY/DT failure, or host/cable
     behavior. A missing pstore record is not enough to distinguish these.
   * **Any new screen text** → record the last line verbatim.
4. Boot back into the 3.18 image afterwards. A failed 5.10 attempt costs one
   flash cycle and tells you exactly which of the three cases above happened.

The 3.18 artifact is still only a kernel prototype; its output package does not
contain an Arch rootfs or establish a supported systemd boot. The Arch target in
this workflow deliberately pairs current Arch/systemd with the mainline 7.1.3
kernel and cgroup-v2 configuration.
