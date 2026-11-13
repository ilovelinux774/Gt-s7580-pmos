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
| `fragment-audit.py` | checks every fragment symbol against the tree's own Kconfig **before** compiling, because `scripts/config` accepts nonsense and `olddefconfig` then drops it silently | run on the real tree: found that the fragment's `DRM_PANEL_SAMSUNG_S6D7AT0B01` does not exist there |
| `build-kernel.sh` | defconfig → fragments → force the exact symbols systemd needs → build → dtbs → append DTB → audit | not yet run to completion (no `flex`/`bison` in this sandbox) |

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

The local work only contains 3.18 trees (armv7 **and** arm64). It does not
contain a 5.10 anything, and no public 5.10 Qualcomm tree with this SoC was
found. So the pin is not 5.10: it is the **msm89x7 mainline tree this
repository's own `pmos-j4primelte-20260927` branch already builds**, at commit
`15c904f9ffe0` on `msm89x7/7.1.3`.

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

**The next concrete step** is a real build of the pinned mainline tree. In order
of increasing cost:

1. Put it in `sources.env` (or use the workflow's `kernel_tarball_url` +
   `kernel_tarball_sha256` inputs, so a tree that only exists on your disk does
   not have to be pushed anywhere).
2. Run the workflow with `dry_run=true`. `verify-tree.sh` will say whether the
   tree has the SoC, which defconfig it would use, and whether a j4primelte DTS
   is present. This costs about a minute.
3. Run it for real. The output `boot-k510.img` is the donor boot image with the
   new kernel, the dm-verity-free pmOS ramdisk unchanged, and
   `systemd.unified_cgroup_hierarchy=1` appended to the cmdline.

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

## Device tree: advisory, never blocking

The pinned tree has no `j4primelte` device tree, so `mainline/msm8917-samsung-j4primelte.dts`
is compiled by its **own early workflow step** into `board-dtb/j4-board.dtb`, which
is then appended to the kernel payload (`Image.gz-dtb`, what Samsung's loader expects).

That step is deliberately non-fatal. If `cpp` or `dtc` fails, the run prints a
warning, sets `DTB_APPENDED=no` in the job summary, and the kernel build carries
on. Rationale: this is a test build. A kernel image with no DTB is still a
useful artifact (it is exactly what the first green run produced); a failed
20-minute compile because the optional device tree would not compile is not.

What the DTB decides in practice:

| | with DTB | without DTB |
|---|---|---|
| Boots on this phone | maybe | only if the bootloader supplies one |
| Screen / panel | as configured in the DTS (M1: simple-framebuffer, no MDSS nodes) | bootloader's framebuffer state survives |
| USB, eMMC, keys, ramoops | from the DTS nodes | nothing describes them |

So "no DTB" is not cosmetic for a boot test - but it is not a build failure
either, and the artifact now states which kind it is (`DTB_APPENDED`).

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
