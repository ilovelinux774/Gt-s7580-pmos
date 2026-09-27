# Galaxy J4+ / SM-J415F: postmarketOS USB bring-up

**Experimental bring-up, not a secure daily-driver image.** A green Actions run
alone is not hardware validation. The owner has now tested selected components
on one SM-J415F using run 13 (image revision `0fdfd5569c9a32f34c7a426bf2b554d7790dcb64`):

- Read/write pmOS rootfs, OpenRC, RNDIS and blank-password root SSH work.
- A temporary RGB-bar program produced visible framebuffer output and backlight.
  No desktop or GPU acceleration has been demonstrated. The MDSS driver powers
  the panel down on the last framebuffer close; keep a display consumer open.
  Real brightness control is `/sys/class/leds/lcd-backlight/brightness`, not the
  Samsung stub `/sys/class/backlight/panel`.
- Wi-Fi scanned, associated and obtained an IPv4 address after stock-firmware
  provisioning, disabling NM management of `p2p0`, and selecting a permanent MAC
  for the connection. A wlan0-bound Internet ping returned 3/3 replies, DNS worked,
  and the system clock was correct. The two compatibility changes were tested
  together: neither one alone has been proven to be the cause/fix.
- Touchscreen `sec_ts`/I2C clock-timeout flooding has been observed and remains
  unresolved. Audio, cellular service, GPU acceleration and general stability are
  not validated. Wi-Fi cold-boot automation below still requires an on-device test.

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

**Recovery confirmed: OrangeFox.** No automatic flashing script is supplied. The
owner reported BOOT `/dev/mmcblk0p23` (32M in lsblk), SYSTEM `/dev/mmcblk0p47`,
APNHLOS `/dev/mmcblk0p45` and VENDOR `/dev/mmcblk0p48` on this phone; still verify
labels and byte capacities on the actual target. Back up the current BOOT and SYSTEM
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
rules cannot be installed. No private Wi-Fi profile or stock firmware is included
in the CI image. The optional live-device setup below does not broaden SSH/telnet
access to Wi-Fi. This access is intentionally unauthenticated; use a trusted,
directly connected computer and remove the debug services before normal,
untrusted-use deployment.

With `boot-debug.img`, connect by telnet to inspect the initramfs. Use
`cat /pmOS_init.log`, `dmesg`, and `ip address`. Run `pmos_continue_boot` if you want
to continue booting. That image is specifically for failures before OpenRC starts.

## Replace the aboot splash on a running phone (no reflash)

The kernel hands the bootloader splash over through continuous splash, has no
framebuffer console, and **powers the panel down when the last `/dev/fb0` handle
closes**. With no boot-time fb consumer, the aboot image simply stays on screen.
`ci/j4primelte/fb-splash.py` is the opt-in live-device fix: it keeps fb0 open,
draws a white-on-dark `POSTMARKETOS` splash (channel-order-safe), drives the real
`/sys/class/leds/lcd-backlight/brightness` LED, commits with `FBIOPAN_DISPLAY`,
and heartbeats an uptime line every 5 s. Run `--test 30` on the phone first, then
`--install` to add a supervised OpenRC service in the **boot** runlevel.

The service intentionally owns fb0. **Stop/disable `j4-fb-splash` before installing
a desktop compositor or framebuffer console later.** Stopping it blanks the screen
by driver design. It changes no partitions, firmware, network or SSH settings.

## Install the touch-oriented LXQt desktop (live, no reflash)

`ci/j4primelte/lxqt-setup.py` apk-installs **postmarketos-ui-lxqt**, which is
pmOS's tablet-tuned LXQt profile (touch-sized panel, onboard on-screen keyboard
autostart, cursor auto-hide), plus `onboard`, `unclutter-xfixes` and
`network-manager-applet`. It stops/disables `j4-fb-splash` first so X can own
fb0, writes an autostart that disables X blanking/DPMS, and enables `tinydm`.
Requires ~1 GiB free and a working `sec_touchscreen` input node (both verified
on the owner's phone). Wi-Fi profiles, firmware staging, USB debug and all
partitions stay untouched. **Software rendering only** (kernel has no DRM):
usable, not GPU-accelerated. Run `--check`, then `--install`, ON THE PHONE.

## Persist the live-tested Wi-Fi setup (no reflash)

`ci/j4primelte/wifi-setup.py` is an **opt-in live-device helper**, not a flashable
image. It requires the tested ARM32/kernel/deviceinfo, the real firmware files
already copied into `/lib/firmware`, and an active, working wlan0 profile with a
saved PSK. It refuses to overwrite pre-existing files not owned by the helper.
Run it ON THE PHONE while USB SSH and Wi-Fi are working:

```sh
python3 /tmp/j4-wifi-setup.py --check
```

```sh
python3 /tmp/j4-wifi-setup.py --install
```

It installs:

- `/usr/local/sbin/j4-wifi`: guarded WCNSS initialization with no automatic retry
  after a failed attempt, and no reinitialization if wlan0 already exists.
- `/etc/init.d/j4-wifi`, enabled in **default**, AFTER NetworkManager and
  wpa_supplicant. This deliberately reproduces the successful late/manual
  sequence and avoids wpa_supplicant selecting p2p0 at early boot.
- `/etc/NetworkManager/conf.d/90-j4-legacy-wifi.conf`: ignore only p2p0 and use a
  permanent connection MAC by default only on wlan0. Global scan randomization,
  other interfaces, WPA settings, USB addresses and SSH services are unchanged.
- Saves the current working profile by UUID, with permanent MAC, autoconnect
  enabled and priority 100. It does not ask for or print the password, start a
  new connection, reload/restart NM, or rerun firmware initialization at install.

Private backups (including the existing Wi-Fi keyfile) are stored root-only in
`/var/lib/j4-wifi/backup-*`. **Never upload that directory or its contents.** It is
outside `j4-collect-logs`' archive path. Firmware is not mounted, fetched or copied
by the helper, and stock/calibration partitions are not modified.

Require `CHECK_OK`, then `INSTALL_OK`. If it stops, keep the current connection
and report the error without sharing keyfiles. A successful installation only
schedules the boot path: **cold-boot/reconnection still needs to be verified**.
On the next boot, inspect `rc-service j4-wifi status` and
`/var/log/j4/wifi-start.log`. If the host loses its manual USB address while the
phone reboots, restore host `172.16.42.2/24` on the actual USB interface before
reconnecting to the phone at `172.16.42.1`.

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
