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

## 2. Why Artix/Arch and not postmarketOS

Not aesthetics — the pmOS install that exists on this phone is ARMv7 and is the
state the owner does not want to lose. Switching to it means mounting and
resizing it, which is precisely what this project refuses to do. So the boot
image has to hand the phone to a *different* root filesystem on a *different*
partition.

The catch, and the reason the last attempt stalled where it did:

> Arch Linux ARM's glibc is built with a kernel floor newer than 3.18. A
> dynamically linked glibc binary aborts at startup with
> `Fatal: kernel too old` on this kernel. systemd, bash, pacman — everything
> Arch ships — is glibc, so none of it can be PID 1 here.

musl is configured at runtime, not against a kernel floor, and runs on 3.18.
That is why `build-rootfs.sh` puts an Alpine/musl tree at `/alpine` and points
`/sbin/init` and `/usr/bin/openrc-init` at it, while keeping Arch's packages at
`/` for tooling. `audit-rootfs.py` is the gate: if the boot path is glibc, the
build fails instead of producing another image that cannot work.

OpenRC (rather than systemd) is not a preference either: OpenRC 0.44+ from
Artix builds and runs on Alpine's musl, and Alpine ships it as its init.

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
```

Scripts (all runnable without the phone, and without a network):

| script | purpose |
|---|---|
| `j4boot.py` | read/verify/repack the Android v0 image; `inspect`, `extract`, `build`, `verify` |
| `extract_payload.py` | pull the pinned kernel payload and DTB out of the reference image |
| `uncpio.py` | extract the initramfs preserving symlinks |
| `mkinitfs_recipe.py` | the mkinitfs recipe, checked against the real initramfs |
| `make_rootswitch_initramfs.py` | build the ramdisk that hands over to the labelled root |
| `pack-boot.py` | assemble a BOOT image; refuses to change the kernel |
| `audit-rootfs.py` | the 3.18-compatibility gate for a root filesystem |
| `build-rootfs.sh` | ext4 rootfs image with a 3.18-safe feature set (CI, needs root) |

Tests: `test_j4boot.py`, `test_rootswitch.py`, `test_pipeline.py`.

## 4. What has and has not been proven

Proven on the phone, by the owner:

* this kernel and DTB boot, and the pmOS initramfs debug shell is reachable;
* RNDIS works via the legacy `android_usb` gadget, telnet and ssh on
  `172.16.42.1`;
* the pmOS rootfs on that install is usable (a *different* image).

Not proven, and not claimed here:

* that the kernel can run the Alpine userspace at all (no musl userspace has
  run on this kernel yet);
* that the root-switch hook finds the labelled filesystem on the real device;
* that a userspace init survives `switch_root` on this kernel;
* anything about the panel, GPU, Wi-Fi, audio, or touch in this configuration.

Every one of those is a step in §5 with a pass/fail criterion. A green CI run
means the artifacts are internally consistent, nothing more.

## 5. Next steps, in order

1. **Flash `boot-debug.img` first.** Same as `boot.img` but with
   `pmos.debug-shell` kept, so if the hook finds nothing you are at a shell
   over USB instead of a black screen. The hook logs every decision to
   `/dev/kmsg` and `/dev/console`.
2. Confirm the log says `found ARCHROOT on /dev/mmcblk0pNN` and
   `userspace init will be /usr/bin/openrc-init`.
3. Only then allow `switch_root`. The pass criterion is a login over the USB
   link from a *userspace* SSH, not from the initramfs.
4. `/var/log/j4/initramfs.log` in the new root holds the initramfs log for the
   boot that got you there — it is copied by the cleanup hook.
