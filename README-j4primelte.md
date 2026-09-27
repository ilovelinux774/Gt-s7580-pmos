# Galaxy J4+ / SM-J415F: postmarketOS USB bring-up

**Experimental, hardware-untested test images. A green Actions run proves a build,
not that the phone boots. The screen, touch, Wi-Fi, sound, and modem are NOT
claimed to work.** The first milestone is a reliable shell and useful logs.

## What changed

This branch uses the existing `build-medusa.yml` workflow entry, but builds a
complete postmarketOS image instead of the old Android AnyKernel ZIP. The GT-S7580
workflow and its `boot.img` at the repository root are unchanged. **Do not flash
that root-level boot.img onto the J4+.** Use only this branch's new Actions artifact.

- Target: **SM-J415F**, `j4primelte`, 32-bit ARM / Alpine `armv7` userspace.
- Kernel: ProjectMedusa's 3.18.140, exact commit pinned in `ci/j4primelte/sources.env`.
- Preserves its J4+ SEA device-tree selection, covering the source's board revisions.
  This is a starting assumption from the vendor configuration, pending on-device
  DT/board verification; no claim is made for other SM-J415 variants.
- Adds devtmpfs, SysV IPC, PTYs, initrd and other Linux userspace options. Disables
  Android-only network permission enforcement. No speculative display patches.
- Uses postmarketOS **v25.12 / OpenRC** rather than systemd on a Linux 3.18 kernel.
- Builds the kernel with pinned Android GCC 4.9, then packages it locally for pmOS.
  Kernel packaging does not silently fall back to a generic kernel.
- Disables modern ext4 features that are unsuitable for this old kernel.
- Uses **Dropbear**, not the OpenSSH configuration from the deleted installation.
- Public source/tool revisions are pinned. Stable package mirrors can move; see
  `installed-packages.txt`. This is not a claim of bit-for-bit reproducibility.

## Run the build

Open the existing **build-medusa.yml** Actions workflow, choose the
`pmos-j4primelte-20260927` branch, and run it. Its sidebar label may still be the
ProjectMedusa Android-kernel name from `main`; this branch runs the pmOS workflow.
It has read-only repository permissions and requires no PAT in the workflow or
its artifacts. The operator's token is only used externally to commit the branch
and dispatch the run.

The workflow first checks pmbootstrap's channel metadata and initializes the
native APK/chroot environment, BEFORE the long kernel compile. The release tree
and `origin/main` channel metadata have separate commit pins; fetching metadata
does not switch the build to edge. Successful kernel outputs are saved before
rootfs packaging and reused only after recipe and file-checksum verification.
Changing only the userspace configuration does not invalidate the kernel cache.
The first cache-producing run still needs a compile; the previous failed run
saved logs only, so its compiled binary cannot be recovered from that artifact.

Three artifacts are expected as their respective stages succeed:

1. `pmos-j4primelte-SM-J415F-usb-debug-<run>` — validated-format test images.
2. `j4primelte-build-logs-<run>` — logs, final kernel config, source pins and patches,
   uploaded even if compilation or packaging fails.
3. `j4primelte-kernel-inputs-<run>` — compiler outputs and provenance for rebuilding
   packages; **NOT boot.img or a recovery-flashable ZIP**. Saved even if the later
   rootfs stage fails.

## Image files

- `boot.img`: normal boot; attempts to start the installed OpenRC rootfs.
- `boot-debug.img`: same kernel and ramdisk, with `pmos.debug-shell` added to the
  v0 Android boot header. Stops in the initramfs for debugging; it is an alternative
  boot image, not something to flash to another partition.
- `rootfs.img.xz`: compressed **raw ext4** filesystem, without a partition table.
  Decompress with `xz -dk rootfs.img.xz` before using an image-flashing tool.
- `SHA256SUMS`: hashes for the downloaded files.
- `ROOTFS-UNCOMPRESSED.sha256`: hash after decompression.
- `image-info.json`: actual sizes and boot-header validation results.

No automatic flashing script is supplied. The custom recovery name and actual
partition sizes/paths still need verification. Back up the current BOOT and SYSTEM
and any Android data you want to keep before replacing anything. The intended
layout is the test boot image on BOOT and the flat root filesystem on SYSTEM;
**do not assume numeric mmcblk partition numbers, format USERDATA, or touch EFS,
PERSIST, modem, or bootloader partitions.** Confirm image sizes against the actual
partition capacities, even if the CI size check passes. Preserve a way back to
recovery / Download Mode. The old kernel is not suitable for a secure daily driver.

## Access from Linux after a successful boot

The phone's USB address is `172.16.42.1/24`. Use the Linux computer for this first
test; the workflow does not claim to fix Windows RNDIS drivers.

If DHCP doesn't configure the host, identify the NEW USB network interface with
`ip -br link` before/after connecting. Substitute its real name below:

```sh
USB_IF=YOUR_USB_INTERFACE
sudo ip link set "$USB_IF" up
sudo ip addr replace 172.16.42.2/24 dev "$USB_IF"
ssh root@172.16.42.1
```

This is a deliberately **passwordless USB debug image**. Root's password hash is
empty (not locked), and Dropbear is started with `-B`. If your SSH client displays
a password prompt, press Enter. A first-connection host-key prompt is normal and
is not a request for a login key. Host keys are generated on the phone, not shared
in the artifact.

Fallback after normal boot:

```sh
telnet 172.16.42.1 23
```

Both rootfs listeners bind the USB IP; iptables additionally drops access to their
ports arriving on any non-USB interface. The service refuses to open them if those
rules cannot be installed. No Wi-Fi is configured. This access is intentionally
unauthenticated; use it only with a trusted directly connected computer and remove
it before enabling other networking or using the phone normally.

With `boot-debug.img`, connect by telnet to inspect the initramfs. Use
`cat /pmOS_init.log`, `dmesg`, and `ip address`. Run `pmos_continue_boot` if you want
to continue booting. That image is specifically for failures before OpenRC starts.

## Collect logs

From a working rootfs shell:

```sh
j4-collect-logs
```

Then from the host (works without an SFTP server):

```sh
ssh root@172.16.42.1 'cat /var/log/j4-diagnostics.tar.gz' > j4-diagnostics.tar.gz
```

Logs are under `/var/log/j4/`. If rootfs boot got far enough, they can also be
retrieved through recovery after mounting the pmOS filesystem. A failure before
that filesystem is mounted may leave no persistent logs; use the debug boot image
and check pstore/last_kmsg from recovery instead.

Next steps are based on those logs: boot stages and USB first, then actual MDSS
framebuffer/panel behaviour, then the Wi-Fi driver/firmware/calibration chain.
