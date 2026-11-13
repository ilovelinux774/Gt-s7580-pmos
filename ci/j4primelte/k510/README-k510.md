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
| `repack-boot.c` | v0 Android boot image `inspect` / `build` / `repack` | self-tested in CI on every run, including the refusal paths |
| `build-kernel.sh` | defconfig → force the exact symbols systemd needs → build → dtbs → append DTB → audit | not yet run against a suitable tree |

`repack-boot repack` is the operation bring-up needs: it copies **every** header
field from the phone's own known-good boot image and swaps only the kernel
payload, then reads the result back and refuses to keep it if anything does not
match. Repacking the 3.18 boot image with its own kernel payload is
byte-identical outside the informational SHA-1 id field — that round trip is
checked locally and the tool's behaviour is re-checked on every CI run.

## The open problem: which 5.10 tree?

Nothing in `sources.env` is a working base yet. What was checked, and what was
found:

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

**The next concrete step** is a 5.10 Qualcomm tree that carries msm8917/8937
(scuba / khaje / sdm429 board support). Once one is in hand, in order of
increasing cost:

1. Put it in `sources.env` (or use the workflow's `kernel_tarball_url` +
   `kernel_tarball_sha256` inputs, so a tree that only exists on your disk does
   not have to be pushed anywhere).
2. Run the workflow with `dry_run=true`. `verify-tree.sh` will say whether the
   tree has the SoC, which defconfig it would use, and whether a j4primelte DTS
   is present. This costs about a minute.
3. Run it for real. The output `boot-k510.img` is the donor boot image with the
   new kernel, the dm-verity-free pmOS ramdisk unchanged, and
   `systemd.unified_cgroup_hierarchy=1` appended to the cmdline.

## Testing on the phone (no UART, no pstore)

1. Keep the known-good 3.18 `boot.img` next to you. Flashing is reversible: the
   BOOT partition is the only thing that changes.
2. Flash `boot-k510.img` to BOOT only. Do **not** touch SYSTEM: the rootfs stays
   the working OpenRC armv7 install.
3. Watch what happens:
   * **Nothing at all / stuck on the boot logo** → the kernel never reached
     userspace. The donor's ramdisk would have produced USB RNDIS within a few
     seconds on 3.18, so no USB gadget means failure before the initramfs.
   * **`172.16.42.1` appears on your host's USB interface** → the kernel booted
     and the initramfs is running. That already proves the kernel works on this
     board; everything after it is userspace.
   * **Screen output** → note the last line verbatim; that is the most useful
     single piece of data this test can produce.
4. Boot back into the 3.18 image afterwards. A failed 5.10 attempt costs one
   flash cycle and tells you exactly which of the three cases above happened.

Do not attempt to run systemd on the 3.18 kernel in the meantime: it is exactly
the configuration whose missing symbols are listed above, and cgroup v2 does not
exist there at all.
