# J4+ Linux 4.9 + systemd 249 debug candidate

This is a separate, BOOT-only diagnostic path; it does not replace the existing
7.1.3 experiment or build/mount an installed root filesystem.

## Pinned kernel candidate

* Repository: `https://github.com/geckyn/kernel_msm8917-4.9.git`
* Branch: `LA.UM.8.6.2.r1/j4primelte`
* Commit: `5713b6504118bcee2ef2aa7823c3446142e2166b`
* Kernel: Linux 4.9.237
* Defconfig: `j4primelte-4.9_sea_defconfig`
* Board DTB: Samsung J4+ Rev06, `msm8917-sec-j4primelte-sea-open-r06.dtb`

This source contains the matching board DTS, Android gadget/configfs RNDIS and
ACM options, and MSM8917 USB controller support. The build disables the
stock `g_android`/Samsung composite gadget so the debug initramfs can bind its
own configfs gadget to the UDC. The upstream repository itself
warns that this branch is very unfinished, while saying it boots. CI success
will prove only that the kernel and image build; it cannot prove this exact
artifact boots on the phone.

## Userspace and cgroups

The initramfs uses Ubuntu 22.04 arm64 systemd 249, BusyBox, and iproute2. The
systemd 249 requirements allow Linux 3.13+ and legacy cgroups. The command line
therefore selects `systemd.unified_cgroup_hierarchy=0`; it must not inherit the
newer systemd-v262/cgroup-v2 requirement from the pmOS-rootfs work. The initramfs
runs systemd as PID 1, configures RNDIS, and keeps the emergency shell. It does
not search for, mount, or switch to the installed root filesystem.

The framebuffer status helper reports initramfs/systemd startup and USB gadget
stages on VT1 when the framebuffer console is available. Pstore is not required.

## Build/test boundary

The workflow pins and builds the source, checks systemd 249's required kernel
options, packages the Rev06 DTB in QCDT form, and refuses to upload an image over
the reported 33,554,432-byte BOOT partition limit. It does not flash the phone.
A hardware boot/USB test is still required, with a known-good recovery image
retained and only BOOT changed if the owner chooses to test it.
