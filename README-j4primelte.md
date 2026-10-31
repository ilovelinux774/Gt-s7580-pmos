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
- `boot-veto.img`: identical payload, with `,veto` substituted into the existing
  `fbcon=` argument (`fbcon=map:0,scrollback:256k,veto`). This is the **control
  group, not the fix**: it puts the vendor veto back, so the console is never bound
  (dark during boot, like run #33) and the panel is never touched from fbcon.
- `image-info.json`: actual sizes and boot-header validation results.

### Boot text: the veto, and why the bind has to move out of probe

With `CONFIG_FRAMEBUFFER_CONSOLE=y` the panel stays dark until the display server
starts, because `fbcon_startup()` asks the driver for permission via `fb_open()`
and Samsung's `mdss_fb_open()` (mdss_fb.c:2864) runs its unblank path whenever no
owner holds the framebuffer (`mfd->ref_cnt == 0`) -- exactly the case while
`mdss_fb_probe()` is still registering it. The refusal is final, not merely
annoying: the vendor `vt.c` consumes `con_startup()` inside
`do_register_con_driver()` and sets `CON_DRIVER_FLAG_INIT` there, so after one
refusal nothing in userspace can bind fbcon again (`/sys/class/vtconsole` keeps
only the dummy device, and `FBIOPUT_CON2FBMAP` from a shell then has no driver to
bind).

What the five attempts measured, all read out of `pstore` rather than guessed:

| run | what the kernel did | result |
|---|---|---|
| #33 | veto kept | boots, no boot text (the safe baseline) |
| #35, #36 | `fb_open()` called, error ignored | reboot; `console-ramoops` ends at `mdss_fb_probe: fb0: split_mode` |
| #37, #38 | same, plus `fbcon=late` to bind at `late_initcall()` | same death, same line, ~1.04 s -- the gating hunk was never in those images, so the notifier still bound during probe |
| #39 | intended "skip the call, suppress the notifier takeover" | same death at 1.073 s. It did neither: the call sits in the `if` *condition*, so guarding only the body still called it, and renaming the flag inverted the sense of `!flag`, so the default cmdline kept the registration-time takeover |

So "binding while `register_framebuffer()` is running" is the variable that kills
this board, in every shape tried, and the log also corrected two of my own
over-readings: `mdss_fb_open()` prints nothing on success, so an absent marker
line cannot prove the call hung; and `mdss_fb_probe()` prints that final line a
dozen statements before `mdss_fb_register()`, so it marks the region, not the
function. A silent end-of-log with no oops fits a deadlock (probe holds
`console_lock` across the notifier) far better than a fault.

The patch therefore defers the bind rather than negotiating with the veto: at
registration time `fb_open()` is not evaluated at all and the takeover is
suppressed, and the bind is done once from `late_initcall()`, after
`mdss_fb_probe()` has run `mdp.init_fnc()`. Deferred **by default** in the
compiled kernel, so `boot.img` carries the fix and there is no boot argument to
remember; `fbcon=veto` selects the vendor behaviour and is what `boot-veto.img`
ships, which makes the comparison a single-variable one (same kernel, same
ramdisk, same DTB, one token). The unproven part, which this run is designed to
answer: at `late_initcall()` nobody owns fb0 yet, so unlike the hand bind that
worked on-device (`ref_cnt != 0`, blank path skipped) the deferred bind will run
`fbcon_init()` -> `mdss_fb_set_par()` on a panel the boot loader handed over. If
that is fatal too, the conclusion is that the bind must wait for a userspace owner
and moves to a service ordered `after tinydm` -- not to another initcall.

Two measurement traps, both already paid for. `pstore` survives a warm reset, so
recovery can read the previous boot's `console-ramoops`; pull it **before**
reflashing, and ignore `dmesg-ramoops-0` (binary, no timestamps). And the s2mu005
PMIC watchdog is enabled ~2.25 s into a healthy boot with `sleep-inhibitor` the
only userspace service keeping it quiet, so trimming `/etc/runlevels/default`
while testing produces a silent reset tens of seconds later that looks exactly
like a kernel fault -- `tinydm`, `j4-x-sysfs` and the rest stay where they are.


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

## Mainline arm64 kernel (M1): experimental BOOT-only bring-up

This is an **experimental** port: the first M1 image halted at DTB handling on
the phone, and the revised candidate has not yet been tested on the phone. The
first target is a pmOS initramfs shell over USB (RNDIS/telnet, optionally ACM)
or the BLSP1 UART. A successful compile or format check does **not** establish
that Samsung LK boots this ARM64 kernel, that the phone's USB controller probes,
or that a shell will appear. Panel, touch, Wi-Fi, audio and GPU remain later
milestones.

The pipeline builds no rootfs and flashes nothing. Its donor is the published,
known-good `boot-37-KNOWN-GOOD-rollback.img` from release `j4-39`: the download,
SHA-256 and the installed p47 rootfs UUID are pinned in
`ci/j4primelte/mainline-sources.env`. The boot image's 32-bit pmOS initramfs is
copied byte for byte, and `CONFIG_COMPAT=y` is required in the ARM64 kernel.
The #37 image is also the BOOT-only rollback in OrangeFox:
<https://github.com/ilovelinux774/Gt-s7580-pmos/releases/tag/j4-39>.

**For the first test use only `boot-m1-debug.img`.** Its `pmos.debug-shell`
flag starts the donor initramfs debug shell before its normal rootfs mount and
resize steps. Do not use `boot-m1-rootfs.img` for this initial test: while it
still flashes only BOOT, normal init may check, resize, mount and write to the
existing filesystem. The observed BOOT partition is `/dev/mmcblk0p23` (32 MiB);
the installed rootfs is p47. Rollback means flashing the #37 boot image to
BOOT, not flashing a rootfs. The phone has OrangeFox; this procedure does not
use fastboot.

The current design tries to preserve the existing ARM32 rootfs through ARM64
compatibility; it has not been proven on the phone. If an ARM64 rootfs later
turns out to be necessary, replacing the current rootfs would erase the current
LXQt, Wi-Fi and audio installation and its configuration. Do not flash one
without a backup and an explicit decision to replace it.

The build checks more than the image extension:

- it verifies the pinned donor's full SHA-256, Android-v0 payload/size ID, page
  size and root UUID, and rejects a different donor;
- it retains the donor ramdisk, ramdisk/tags addresses and other metadata, sets
  the kernel load address using the ARM64 Image's 2 MiB alignment and text offset,
  and hashes the correct Android-v0 payload/size ID. The J4+ DTB starts exactly
  at gzip EOF, without a gap or an assumed alignment. The gzip header has no
  optional FNAME, matching the public TeamWin J4+ ARM64 prebuilt format;
- it independently reads each output, checks its boot ID, donor ramdisk and
  UUID, actual ARM64 kernel header, gzip stream, DTB and required bootargs;
- it checks the compiled DTB's root `qcom,msm-id`, `qcom,board-id` and
  `qcom,pmic-id` against the known-good donor's Rev06 identification. This is a
  compatibility candidate, not proof of Samsung LK's exact matching rules;
- it audits CONFIG_COMPAT, gzip initramfs, RPM/SMD, ChipIdea UDC, the actual
  `qcom,usb-hs-28nm-femtophy`, dummy extcon, RNDIS and ACM as built-ins;
- it checks that the compiled DTB connects the USB controller to its PHY and
  dummy extcon, and tests estimated ARM64 kernel ranges against the donor's
  tags and initramfs addresses. LK's actual placement is **not** established.

The generic arm64 defconfig normally enables many unrelated platforms and
modules; before applying the J4+ fragment, this build removes unrelated platform
menus and inherited `=m` drivers. Required J4+ drivers are then explicitly
built in. M1 deliberately omits USB host, PCI, Wi-Fi, Bluetooth, IPv6 and f2fs:
they are not needed for the initial USB debug shell. The existing installation
and its services are available again after a BOOT-only rollback. A kernel too
large for the 32 MiB BOOT partition or an estimated memory overlap fails
verification instead of being presented as safe.

The separate `.github/workflows/build-mainline.yml` is registered on `main`, but
must be dispatched on branch `pmos-j4primelte-20260927`. Set `panel_milestone`
only for the staged, untested M2 variant; it is not part of the M1 acceptance.
The job produces `boot-m1-debug.img` and `boot-m1-rootfs.img` as build artifacts;
a separately published GitHub Release is required for phone-side delivery.

## Running the mainline pipeline locally

Run the tests against the complete pinned tree, not a partial checkout:

```sh
J4_MAINLINE_KERNEL_TREE=$HOME/j4-mainline-kernel python3 ci/j4primelte/test-mainline.py
```

Build M1 with the exact pinned donor and the ARM64 cross compiler installed:

```sh
J4_MAINLINE_BASE_BOOT=$HOME/j4-mainline-donors/boot-37-KNOWN-GOOD-rollback.img bash ci/j4primelte/build-mainline.sh
```

Verify the completed artifacts independently:

```sh
python3 ci/j4primelte/verify-mainline.py artifacts/mainline $HOME/j4-mainline-donor/boot.img
```

`J4_MAINLINE_DTBS_ONLY=1` reuses a prior output only when its archived `.config`
still matches; the same ARM64 toolchain is required because Kconfig also probes
the compiler during a device-tree-only build.

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
