# LK2ND-first bring-up for the J4+

The Geckyn LK2ND tree explicitly contains an MSM8917 secondary target with a
J4+ Rev06 device tree matching `J415*` and board ID `<8 6>`. The existing
manual workflow, `.github/workflows/j4primelte-k510.yml`, has an independent
`Build standalone J4+ LK2ND bootloader` job. Dispatch it on this branch with
`selftest_only=true` to avoid the long Linux kernel build; the job builds
pinned source commit `7bc82f56d1339b1d081c48ad3c664ec6119257af` and checks the
Android boot header, QCDT payload, page size, and BOOT-partition size. The build
applies two small, explicit compatibility fixes to the pinned
legacy source: it makes the stack-canary and PMIC display-name globals single
definitions and converts header-level anonymous enum objects (SCM, SDHCI, BLSP,
GPIO) to tagged enum types. A successful CI build only establishes that the
image was built and checked; it does not prove it boots on this phone.

## Two-stage test plan

1. Download artifact `lk2nd-j4primelte-<run-id>` from the successful Actions
   run. It contains `lk2nd-j4primelte.img` and `lk2nd-j4primelte-odin.tar`
   (the same image packaged as `boot.img` for Odin's AP slot), plus SHA-256
   files. Verify the selected file with its matching `.sha256` file.
2. Test LK2ND alone, flashing only the BOOT partition. Keep the existing
   OrangeFox/Odin recovery route available. Do not write SYSTEM or other
   partitions.
3. If LK2ND reaches its screen, enter its Fastboot mode with Volume Down during
   LK2ND startup (wait until the stock loader has handed off). Connect to the
   host and confirm `fastboot devices` sees the phone. This is the Fastboot
   interface provided by LK2ND; it is not a feature of the stock bootloader.
4. Only after that check, use LK2ND Fastboot to boot or flash the separately
   identified Linux 7.1.3 Android boot image. LK2ND reserves the first 1 MiB of
   BOOT for itself; its Fastboot implementation places the real Android boot
   image after that offset. Do not write an image directly over LK2ND's slot.

LK2ND addresses the bootloader/Android boot-image handoff and supplies a
Fastboot interface. It does **not** make the in-repository mainline J4 DTS/DTB
known-good. The Linux 7.1.3 candidate and its J4 DTB remain unproven and must be
identified and checked separately before testing. Do not combine the rejected
Riva 4.19 kernel with the Medusa 3.18 DTB.
