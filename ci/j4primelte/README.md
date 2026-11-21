# Samsung Galaxy J4+ (SM-J415F) — bring-up, from the artifact that booted

This directory is a restart. It is organised around one idea: **everything must
be traceable to a file that booted on the phone.** The previous attempt
accumulated dozens of images whose only relationship to a booting system was
that CI had built them without error. This one starts from the image that
actually reached a shell, extracts what matters from it, and only ever changes
one variable at a time.

## 0. The anchor

`./boot.txt` (the owner's upload) is the Medusa ARM64 image whose postmarketOS
initramfs debug shell was reached on the handset over USB/RNDIS. It is
**17,510,400 bytes**, and the same file is published as a release asset on
`j4-medusa64-fbdev-parent-debug-1`. Its contents, as read by
`j4boot.py inspect`:

| field | value |
|---|---|
| boot header | Android v0, page size 2048 |
| load addresses | kernel `0x80080000`, ramdisk `0x83600000`, tags `0x83400000` |
| kernel payload | `sha256 65081abb…`, `Linux 3.18.120-j4-medusa64-debug+`, arm64 |
| appended DTB | `sha256 6fca7cf5…`, `Samsung J4PRIMELTE SEA OPEN PROJECT Rev06`, board-id `0x8/0x6` |
| ramdisk | plain gzip cpio, **32-bit ARM (armhf)**, pmOS initramfs 3.8.9-r0 |
| command line | `console=ttyMSM0 … fbcon=map:0,scrollback:256k consoleblank=0 pmos_root_uuid=86e7262b-… pmos.debug-shell` |

Those facts are pinned in `pins.env`. `test_j4boot.py` asserts them byte for
byte when `J4_REFERENCE_BOOT` points at the file.

Two things about this image are worth being explicit about, because both
shaped the earlier attempts:

* **The kernel has `CONFIG_COMPAT=y` but `# CONFIG_FRAMEBUFFER_CONSOLE is not
  set`.** There is no Linux text console on the panel. A boot either reaches
  the initramfs (which draws nothing) or gives no visible feedback at all. The
  only evidence channels are serial (`ttyMSM0`), pstore/RAMOOPS, and USB.
* **The kernel's display stack is what makes or breaks booting.** On this tree
  `mdss_fb_probe()` runs `mdss_fb_open()` while no owner holds the framebuffer
  and power-cycles a panel the bootloader is still driving. The earlier work
  established, from pstore logs, that binding the framebuffer console during
  probe kills the board at ~1.07 s. This kernel avoids the problem by not
  having a framebuffer console at all: the reference image is the one that
  survives.

## 1. What is actually in the image

`make_rootswitch_initramfs.py` rebuilds the ramdisk from the extracted pmOS
tree. The hook it adds (`hooks-extra/99-j4-root-switch.sh`) is the whole point:
it finds an ext4 filesystem by label, refuses to touch anything that looks like
a postmarketOS install, and records which userspace init should become PID 1.
`init_2nd.sh` is patched so the stock chain — which waits 30 s for a
`pmOS_root` partition, resizes it, and halts when it is absent — is skipped
when the hook has already mounted a root.

Nothing in the hook mounts anything read-write, resizes anything, or writes to
a partition. `test_rootswitch.py` runs the real hook under `/bin/sh` with
stubbed `blkid`/`mount` and asserts exactly that.

## 2. Why ARMtix (Artix), and why the glibc worry is over

Not aesthetics: the pmOS install that exists on this phone is ARMv7 and is the
state the owner does not want to lose. Switching to it means mounting and
resizing it, which is what this project refuses to do. So the boot image hands
the phone to a *different* root filesystem on a *different* partition — Artix,
because that is what the owner wants to run, not because anything else failed.

The previous attempt stalled on this claim:

> Arch Linux ARM's glibc is built with a kernel floor newer than 3.18 ...
> everything Arch ships is glibc, so none of it can be PID 1 here.

The mechanism is real — glibc refuses to start when `.note.ABI-tag` names a
kernel newer than `uname` says — but the number was wrong. glibc's aarch64
minimum is **3.7.0** (`arch_minimum_kernel` in
`sysdeps/unix/sysv/linux/aarch64/configure.ac`), and Arch Linux ARM's glibc
PKGBUILD passes no `--enable-kernel` that could raise it. ARMtix inherits that
glibc, so its binaries carry `ABI: 3.7.0`, which is below this phone's
**3.18.120**. Upstream Arch Linux's glibc, built with `--enable-kernel=4.4`,
*would* fail here — that is the case the check catches.

So the workaround the earlier attempt built (Alpine/musl at `/alpine`, Arch at
`/`, `/sbin/init` symlinked between them) is gone. There is one userspace now,
and it is ARMtix with OpenRC as PID 1.

`audit-rootfs.py` is the gate, and it no longer reasons from library names: it
reads the note out of every ELF in the tree and fails the build when any file —
on the boot path or not — demands a kernel newer than 3.18.120.
`README-rootfs.md` has the full analysis and the evidence.

## 2a. What the initramfs hook looks for

`/usr/bin/openrc-init`, then `/sbin/openrc-init`, then `/sbin/init`, then
`/usr/bin/init`, on an ext4 filesystem labelled `ARTIXROOT`. It refuses to touch
anything whose `/etc/os-release` says `postmarketos`, and it mounts the target
read-only first, remounting read-write only after the init has been chosen.
Nothing in it resizes, repairs or writes to a partition.

## 2b. The journal, and the one mount detail that matters

The hook mounts the target read-only first and remounts it read-write only
after the init has been chosen. The read-only mount is **not** `-o noload`: an
ext4 read-only mount does not replay the journal on its own, so nothing is
written anyway, but the read-write remount *does* recover it. `noload` would
keep the flag in effect across that remount and hand a filesystem with an
unreplayed journal to the kernel as read-write.

## 3. The pipeline

```
boot.txt ──j4boot.py──► kernel.payload + appended.dtb + ramdisk.gz
                              │                        │
                              │                        └──uncpio.py──► pmOS initramfs tree
                              │                                            │
                              │                        make_rootswitch_initramfs.py
                              │                                            │
                              │                                  rootswitch-initramfs.gz
                              │                                            │
                              └──────────────── pack-boot.py ─────────────┘
                                                     │
                                                 boot.img   (kernel byte-identical to boot.txt)

ARMtix tarball ──build-rootfs.sh──► audit-rootfs.py (3.18 gate) ──mke2fs -d──► rootfs.img
```

Two chains, one shared BOOT image. The rootfs chain never runs in the boot job:
it is the only part that needs a 1 GiB download, root and a few minutes.

Scripts (all runnable without the phone; only `build-rootfs.sh` and the
reference-image step need a network):

| script | purpose |
|---|---|
| `j4boot.py` | read/verify/repack the Android v0 image; `inspect`, `extract`, `build`, `verify` |
| `extract_payload.py` | pull the pinned kernel payload and DTB out of the reference image |
| `uncpio.py` | extract the initramfs preserving symlinks |
| `mkinitfs_recipe.py` | the mkinitfs recipe, checked against the real initramfs |
| `make_rootswitch_initramfs.py` | build the ramdisk that hands over to the labelled root |
| `pack-boot.py` | assemble a BOOT image; refuses to change the kernel |
| `elfnotes.py` | read an ELF's interpreter, architecture and `.note.ABI-tag` floor |
| `audit-rootfs.py` | the 3.18 gate: every ELF's floor vs 3.18.120 |
| `build-rootfs.sh` | ARMtix ext4 rootfs image, 3.18-safe features, no loop mount |

Tests: `test_j4boot.py`, `test_rootswitch.py`, `test_pipeline.py`,
`test_rootfs.py` (the last one also builds a real image when run as root).

## 4. What has and has not been proven

Proven on the phone, by the owner:

* this kernel and DTB boot, and the pmOS initramfs debug shell is reachable;
* RNDIS works via the legacy `android_usb` gadget, telnet and ssh on
  `172.16.42.1`;
* the pmOS rootfs on that install is usable (a *different* image).

Proven on the artifacts, without the phone:

* the BOOT images keep the kernel payload and DTB byte-identical to the image
  that booted, and fit the 32 MiB BOOT partition;
* the rootfs image uses only ext4 features 3.18 can mount, and carries the
  label the hook searches for;
* **no ELF in the rootfs refuses to start on 3.18.120.** Each binary's
  `.note.ABI-tag` is read out of the image and compared; ARMtix's glibc
  carries 3.7.0. So "Fatal: kernel too old" cannot be the reason a boot fails.

Not proven, and not claimed here:

* that this kernel runs this userspace at all (nothing but the initramfs has
  run on it yet);
* that the root-switch hook finds the labelled filesystem on the real device;
* that `openrc-init` survives `switch_root` on this kernel;
* anything about the panel, GPU, Wi-Fi, audio, or touch in this configuration.

The kernel has no framebuffer console, so a failed boot gives no visible
feedback. Every step below therefore has a pass/fail criterion read over USB or
serial, not looked at on the panel.

## 5. Next steps, in order

1. **Flash `boot-debug.img` first.** Same as `boot.img` but with
   `pmos.debug-shell` kept, so if the hook finds nothing you are at a shell
   over USB instead of a black screen. The hook logs every decision to
   `/dev/kmsg` and `/dev/console`.
2. Confirm the log says `found ARTIXROOT on /dev/mmcblk0pNN` and
   `userspace init will be /usr/bin/openrc-init`.
3. Only then allow `switch_root`. The pass criterion is a login over the USB
   link from a *userspace* SSH, not from the initramfs.
4. `/var/log/j4/initramfs.log` in the new root holds the initramfs log for the
   boot that got you there — it is copied by the cleanup hook.
