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

## GPU experiment (opt-in)

This kernel carries drivers/gpu/drm/msm with MDP5 KMS and the a3xx (Adreno 308)
GPU, but it can never bind as shipped: msm_drv.c's dt_match[] excludes
"qcom,mdss_mdp" because the framebuffer driver owns that node, and Samsung's
device tree has no "gpus" phandle list, so the GPU never becomes a component of
the DRM device. Freedreno is already installed in userspace (mesa-dri-gallium);
what it needs is /dev/dri/card0.

Run the workflow with **gpu_experiment** enabled to build that configuration.
kernel-gpu.py adds the dt_match entry and a fallback that registers the
qcom,kgsl-3d0 node as the GPU component. Trade-offs, deliberately:

- CONFIG_MSM_KGSL and CONFIG_FB_MSM_MDSS are disabled, so the framebuffer
  display is off until the panel works under DRM. Plan for a headless test.
- CONFIG_DRM_MSM=m and the module is blacklisted, so a failed probe cannot
  stop the boot. Load it by hand: `modprobe msm`, then read dmesg.
- Leaving the input off produces exactly the previous, display-working image.

## Root filesystem UUID

Every build of the rootfs is a fresh ext4 filesystem, which would normally get a
new UUID, while `boot.img` looks for its root by `pmos_root_uuid`. Mixing a
`boot.img` and a `rootfs.img` from different runs would then stop in the
initramfs debug shell with `ERROR: failed to mount subpartitions!`, which looks
like a bricked phone but is only a mismatch.

`ci/j4primelte/fix-root-uuid.py` pins one UUID for every build
(`86e7262b-b628-4b89-8a6e-e96ef9b1aab6`) and patches the boot header cmdline to
match, so images from different runs can be mixed safely. Only cmdline bytes in
the boot header change; the kernel and ramdisk payloads stay byte-identical.

## Image files

- `boot.img`: normal boot; attempts to start the installed OpenRC rootfs.
- `boot-debug.img`: same kernel and ramdisk, with `pmos.debug-shell` added to the
  v0 Android boot header. Stops in the initramfs for debugging; it is an alternative
  boot image, not something to flash to another partition.
- `rootfs.img.xz`: compressed **raw ext4** filesystem, without a partition table.
  Decompress with `xz -dk rootfs.img.xz` before using an image-flashing tool.
- `SHA256SUMS`: hashes for the downloaded files.
- `ROOTFS-UNCOMPRESSED.sha256`: hash after decompression.
- `boot-slabdebug.img`: identical payload, `slub_debug=FZPU panic_on_oops=0` added.
  Flash this instead to catch the slab corruption that panics the board; it
  boots normally and does not stop in the initramfs. It is only meaningful in a
  build where `CONFIG_SLUB_DEBUG=y` (asserted by `build-kernel.sh` from run #36
  on); in run #35 the argument is parsed away and nothing is checked.
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
It also installs the `j4-x-sysfs` boot service (runs before `tinydm`): this
kernel's MDSS framebuffer registers without a parent device, so the sysfs link
`/sys/class/graphics/fb0/device/subsystem` never exists and xorg-server 21.1.23
fbdevhw silently rejects the framebuffer ("No devices detected"); the service
bind-mounts a tiny fake sysfs tree to satisfy the probe. The session hook also
rewrites `/sys/class/leds/lcd-backlight/brightness` — the MDSS backlight
register only takes effect when written (it reads 256 while the panel stays
dark until a write lands).
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

## Mainline arm64 kernel (M1): BOOT only, rootfs untouched

`build-medusa.yml` builds the downstream 3.18 kernel. That kernel has no usable GPU
path (libhybris was dropped from postmarketOS in November 2024), so the mobile UI
needs the arm64 mainline kernel. `build-mainline.yml` is the separate job that gets
there one milestone at a time:

| milestone | content | status |
|---|---|---|
| M0 | device survey, partition map, boot header layout | done |
| M1 | boot to a telnet/simpledrm console over the *existing* rootfs | this job |
| M2 | panel + backlight (`J4_MAINLINE_PANEL=1`, panel `.config`) | staged, untested |
| M3 | capacitive touch (s6sy761) | not started |
| M4 | Wi-Fi/BT (wcn3620 over WCNSS SMD) | not started |
| M5 | freedreno GPU | not started |

The job never produces a rootfs. It downloads the newest successful `build-medusa`
run's `j4primelte-boot-only-*` artifact and uses its **boot.img as a donor**: the
initramfs ramdisk is copied **byte for byte**, only the kernel payload is replaced.
That is why the installed userspace survives — the boot image carries no rootfs and
SYSTEM (p47) is never written. Flashing the previous boot.img is a complete rollback.

The four images it publishes:

| image | behaviour |
|---|---|
| `boot-m1-rootfs.img` | mainline kernel, boots the installed rootfs |
| `boot-m1-debug.img` | same kernel, drops to the initramfs telnet shell at 172.16.42.1 |
| `boot-panel-rootfs.img` | only with `panel_milestone` checked: M2 device tree |
| `boot-panel-debug.img` | only with `panel_milestone` checked |

Decisions that are not obvious from the code:

- **Payload is `Image.gz` + our own DTB appended**, no QCDT/`second` image. Samsung
  LK only looks for a `second` dt image when `dt_size != 0`; a v0 header with
  `dt_size == 0` takes the appended DTB, which is what release LK2ND binaries
  support here. `verify-mainline.py` fails the build unless the appended tree's own
  `totalsize` accounts for exactly the bytes after the kernel, and the tree starts on
  an 8-byte boundary (zero padding after `Image.gz`), which is what the devicetree
  spec and LK's backwards scan for `d00dfeed` both require.
- **Boot header v0 legacy layout** (see `mainline-repack-boot.py` for the offsets):
  ten u32 sizes/addresses, `name[16]` at 48, `cmdline[512]` at 64, `id[8]` at 576,
  `extra_cmdline[1024]` at 608. The GKI 16-word struct is wrong for this phone and
  produced garbage like "unsupported boot header version 3".
- **Everything is built-in** (`CONFIG_MODULES=n`): the ramdisk and the rootfs are the
  downstream **armv7** ones, so there is nowhere to load an arm64 `.ko` from, and
  `CONFIG_COMPAT=y` must stay on or the 32-bit `/init` cannot execute.
- Bootargs are additive and idempotent: `clk_ignore_unused pd_ignore_unused
  regulator_ignore_unused rootwait` are appended to the donor cmdline (vendor
  `androidboot.*`, `lcd_id`, `vmalloc` arguments are inherited, never stripped), so
  `pmos_root_uuid` keeps pointing at the installed rootfs.
- Panel/touch/GPU are deliberately **off** in M1: the device tree keeps `&mdss`
  disabled and the `simple-framebuffer` node self-disables, so nothing can fight
  the bootloader over the display controller before M2.
- **Milestone switching is a second source file, not a `-D` flag.**
  `mainline/msm8917-samsung-j4primelte.dts` holds the shared tree with
  `#ifdef J4_PANEL` zones, and
  `mainline/msm8917-samsung-j4primelte-panel.dts` is a three-line wrapper that
  `#define`s `J4_PANEL` and includes it. Both `.dtb`s then come out of one
  `make dtbs`. `DTC_FLAGS=-DJ4_PANEL` was tried and cannot work: kbuild runs
  `cpp` before `dtc`, and this `dtc` has no `-D` at all — it aborted every other
  qcom/actions dtb in the tree with `invalid option -- 'D'`.

## Running the mainline pipeline locally

Everything the workflow does lives in `ci/j4primelte/build-mainline.sh`, and the
checks are worth running first because they are the ones that catch a stale device
tree or a header edit (builds go into `~/j4-mainline-kernel` and
`~/j4-mainline-out-*`; never build into `/tmp`, it is 992 MiB of tmpfs here, and
a directory literally named `build` is wiped by the workspace snapshotter):

```sh
TMPDIR=/home/user/tmp J4_MAINLINE_KERNEL_TREE=/home/user/j4-mainline-kernel python3 ci/j4primelte/test-mainline.py
```

The full build clones the pinned tree, then packs boot images from a donor
`boot.img`. `J4_MAINLINE_BASE_BOOT` is the donor gate: without it the kernel
payloads are still built and published, and the job says the boot images were
skipped instead of pretending otherwise:

```sh
J4_MAINLINE_BASE_BOOT=$HOME/j4-mainline-donor/boot.img J4_MAINLINE_PANEL=1 bash ci/j4primelte/build-mainline.sh
```

To redo only the device tree and the packing after a bootarg or `.dts` edit (the
compiled kernel is reused from `~/j4-mainline-out-*`):

```sh
J4_MAINLINE_DTBS_ONLY=1 J4_MAINLINE_BASE_BOOT=$HOME/j4-mainline-donor/boot.img bash ci/j4primelte/build-mainline.sh
```

An M1 image that boots gives a shell over RNDIS (`172.16.42.1`, telnet) and on the
BLSP1 UART at 115200 8N1 — `stdout-path = "serial0:115200n8"` with `&blsp1_uart1`
enabled, which is the first thing `verify-mainline.py` checks, because a DTB whose
console node is still `status = "disabled"` looks fine in source and is useless on
the wire.

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
