# J415F experimental HLOS handoff diagnostic

**Not a proven fix.** The previous J415F lk2nd M1 image displayed Tux, then a
blue screen and rebooted. Neither Linux entry nor the cause has been shown.
The Samsung T380/T385 reports motivate an HLOS-memory SCM experiment, but do
not establish the J415F XPU policy. The previous image is marked device-failed.

This combined **BOOT-only** diagnostic retains geckyn's 1-MiB in-memory split,
uses the same original mainline kernel and unchanged #37 debug initramfs, and
tries a relocated ARM32-to-AArch64 secure-monitor call when the detected SCM ABI
is modern. The original legacy SCM call remains intact. Nothing here flashes
or generates a rootfs image. The debug-shell check precedes the donor's normal
rootfs-discovery/mount/resize paths, but effects from earlier hooks/udev and the
real hardware have not been proven. Do **not** use `pmos_continue_boot` during a
preservation-focused diagnostic. Some geckyn recovery-message paths can write
`misc`; `SAFE_MODE` is not a guarantee of zero persistent writes everywhere.

## Marker semantics and limitations

The diagnostic deliberately overwrites the **previous volatile pmsg RAM ring**
(not the p47 filesystem) with one stage message at physical `0x860c0000`:

- `A0`: patched lk2nd entered its SCM handoff function.
- `A1: legacy SCM path`: detection selected the unmodified legacy call.
- `A1: ARMv8 HLOS SMC path`: detection selected the relocated modern call.
- `R`: the SCM call returned instead of handing off.
- `B`: the AArch64 entry **wrapper** ran; this is not proof Linux itself ran.

Only the most recently committed stage is retained; later attempts or boots may
overwrite earlier ones. Recovery must use the same vendor pmsg layout and retain
RAM across reboot. OrangeFox's exact DTB, the actual retention, and hardware
permissions have not been verified: **no marker is inconclusive**. The companion
diagnostic DTB reduces the mainline ramoops region to dump/console/ftrace, and
reserves the marker quarter separately with `no-map` so mainline does not claim
it as ftrace. This tree does not alter recovery's own DTB.

The marker is written after lk2nd's `arch_disable_cache(UCACHE)` and MMU-off;
the ARM32 stub writes parameter data and instruction words to low HLOS RAM,
executes `dsb`/`isb` and enters an SMC from there. The values and retry-on-1
behavior match the audited original path. Real cache, XPU, secure firmware and
register behavior remain hardware-dependent. A simulated successful jump is not
a hardware test.

## Reproduction

The manual Actions workflow has an `hlos_diagnostic_only` switch. That quick
mode downloads the **failed** release image solely as a SHA-pinned source of
the unchanged mainline kernel, DTBs and debug initramfs; it **does not rebuild
the mainline kernel**. It rebuilds the patched lk2nd using the pinned geckyn
revision, verifies both Android headers, checks the DTB semantic delta, tests
real compiled ARM/ARM64 marker code with Unicorn 2.1.4, and emits only the new
`lk2nd-hlos-diagnostic.img` in its boot artifact. A previous failed BOOT is not
an output or flash target. On a local Linux host with dependencies installed,
`reproduce-lk2nd-hlos-diag.sh` performs the same reconstruction.

Synthetic tests cover cached/lazy SCM-detection branches, the original legacy
path, relocated modern parameters and SMC instructions, simulated interrupt
retry/error, register preservation, pmsg bounds and commits, the ARM64 wrapper,
several DTB-corruption cases and both Android image layers. They stub the
secure monitor and cannot prove physical storage, RAM access, retention, Linux,
USB or rootfs behavior. The private J415F aboot is not part of the repository.
