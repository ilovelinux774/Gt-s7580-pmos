#!/usr/bin/env bash
# Assemble a small arm64 systemd initramfs from extracted Ubuntu arm64 packages.
# This is intentionally not a distribution rootfs: it contains only systemd,
# its runtime libraries, BusyBox, iproute2, and the debug gadget units/scripts.
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <arm64-deb-directory> <work-directory> <output-initramfs.cpio.xz>" >&2
  exit 2
fi

deb_dir=$(realpath "$1")
work_dir=$(realpath -m "$2")
out=$(realpath -m "$3")
root="$work_dir/root"

command -v dpkg-deb >/dev/null || { echo "dpkg-deb is required" >&2; exit 1; }
shopt -s nullglob
packages=("$deb_dir"/*.deb)
((${#packages[@]} > 0)) || { echo "no .deb packages in $deb_dir" >&2; exit 1; }

rm -rf "$root"
mkdir -p "$root"
arm64_count=0
for package in "${packages[@]}"; do
  arch=$(dpkg-deb -f "$package" Architecture)
  [[ $arch == arm64 || $arch == all ]] || continue
  [[ $arch != arm64 ]] || ((arm64_count += 1))
  dpkg-deb -x "$package" "$root"
done
((arm64_count > 0)) || { echo "no arm64 packages found in $deb_dir" >&2; exit 1; }

# Package payloads include documentation, translations, headers and caches
# intended for a full distro install. None is needed by this debug initramfs.
rm -rf "$root/usr/share" "$root/usr/local/share" "$root/usr/include" \
  "$root/usr/lib/pkgconfig" "$root/usr/lib/cmake" \
  "$root/var/cache" "$root/var/lib"

systemd_bin="$root/usr/lib/systemd/systemd"
[[ -x $systemd_bin ]] || systemd_bin="$root/lib/systemd/systemd"
[[ -x $systemd_bin ]] || { echo "systemd executable missing from arm64 packages" >&2; exit 1; }
[[ -x "$root/bin/busybox" ]] || [[ -x "$root/usr/bin/busybox" ]] || {
  echo "busybox is missing from the arm64 packages" >&2; exit 1;
}

mkdir -p "$root"/{dev,proc,sys,run,tmp,root,etc/systemd/system/emergency.target.d,etc/systemd/system/emergency.service.d,usr/libexec,usr/local/sbin}
mkdir -p "$root/etc/systemd/system"/{emergency.target.wants,initrd.target.wants}
# systemd detects this file and stays in initrd mode. There is deliberately no
# /etc/fstab, root= argument, or switch-root unit in this diagnostic ramdisk.
printf 'NAME=J4 systemd debug initramfs\nID=j4-systemd-debug\n' > "$root/etc/initrd-release"
printf 'NAME="J4 systemd debug initramfs"\nID=j4-systemd-debug\n' > "$root/usr/lib/os-release"
ln -sfn ../usr/lib/os-release "$root/etc/os-release"
: > "$root/etc/machine-id"
: > "$root/etc/hostname"
printf 'root:x:0:0:Debug root:/root:/bin/sh\n' > "$root/etc/passwd"
printf 'root:x:0:\n' > "$root/etc/group"
printf 'passwd: files\ngroup: files\nshadow: files\nhosts: files dns\n' > "$root/etc/nsswitch.conf"
printf 'root::0:0:99999:7:::\n' > "$root/etc/shadow"
chmod 0600 "$root/etc/shadow"

busybox="$root/bin/busybox"
[[ -x $busybox ]] || busybox="$root/usr/bin/busybox"
mkdir -p "$root/bin"
ln -sfn "${busybox#"$root"}" "$root/bin/sh"
ln -sfn "${busybox#"$root"}" "$root/bin/ash"
for applet in grep mkdir mount seq sleep umount; do
  ln -sfn "${busybox#"$root"}" "$root/bin/$applet"
done

cat > "$root/usr/libexec/j4-debug-screen" <<'EOF'
#!/bin/sh
# Write a high-contrast status page to the framebuffer VT, independently of
# pstore and USB. The kernel DT supplies a simple framebuffer on this board.
set -u
screen=/dev/tty1
[ -c "$screen" ] || screen=/dev/console
[ -c "$screen" ] || exit 0
{
  printf '\033[2J\033[H\033[1;36mJ4+ LINUX 4.19 DEBUG BOOT\033[0m\n\n'
  printf '\033[1;37m%s\033[0m\n' "$@"
} > "$screen" 2>/dev/null || true
if [ -w /dev/kmsg ]; then
  printf 'j4-debug-screen: %s\n' "$*" > /dev/kmsg 2>/dev/null || true
fi
EOF
chmod 0755 "$root/usr/libexec/j4-debug-screen"

systemd_exec=${systemd_bin#"$root"}
cat > "$root/init" <<EOF
#!/bin/sh
# PID 1 is systemd in initrd mode. No pmOS init, root search, or switch_root.
export PATH=/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin
/usr/libexec/j4-debug-screen 'KERNEL STARTED' 'INITRAMFS STARTED' 'STARTING SYSTEMD AS PID 1'
exec $systemd_exec --system
EOF
chmod 0755 "$root/init"

cat > "$root/usr/libexec/j4-debug-rndis" <<'EOF'
#!/bin/sh
set -eu
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
/usr/libexec/j4-debug-screen 'SYSTEMD IS RUNNING' 'PROBING USB CONTROLLER'

# systemd mounts sysfs/devtmpfs; mount configfs if the kernel exposes it.
mkdir -p /sys/kernel/config
if ! grep -q ' /sys/kernel/config configfs ' /proc/mounts; then
  mount -t configfs configfs /sys/kernel/config || {
    /usr/libexec/j4-debug-screen 'SYSTEMD IS RUNNING' 'CONFIGFS MOUNT FAILED'
    echo 'debug-rndis: configfs mount failed' >/dev/kmsg 2>/dev/null || true
    exit 1
  }
fi

udc=''
for attempt in $(seq 1 30); do
  for entry in /sys/class/udc/*; do
    [ -e "$entry" ] || continue
    udc=${entry##*/}
    break
  done
  [ -n "$udc" ] && break
  sleep 1
done
[ -n "$udc" ] || {
  /usr/libexec/j4-debug-screen 'SYSTEMD IS RUNNING' 'NO USB DEVICE CONTROLLER FOUND'
  echo 'debug-rndis: no USB device controller appeared' >/dev/kmsg 2>/dev/null || true
  exit 1
}
/usr/libexec/j4-debug-screen 'USB CONTROLLER FOUND' "$udc" 'CONFIGURING RNDIS + ACM'

g=/sys/kernel/config/usb_gadget/j4debug
mkdir -p "$g"
echo 0x18d1 > "$g/idVendor"
echo 0x4ee7 > "$g/idProduct"
mkdir -p "$g/strings/0x409"
echo 'J4 debug' > "$g/strings/0x409/manufacturer"
echo 'Linux 4.19 systemd initramfs debug' > "$g/strings/0x409/product"
echo 'j4-systemd-debug' > "$g/strings/0x409/serialnumber"
mkdir -p "$g/configs/c.1/strings/0x409"
echo 'RNDIS and debug serial' > "$g/configs/c.1/strings/0x409/configuration"
echo 120 > "$g/configs/c.1/MaxPower"

# Provide RNDIS for host-side network enumeration and ACM serial for an
# interactive emergency shell, without a separate SSH daemon or rootfs.
mkdir -p "$g/functions/rndis.usb0"
echo 02:4a:34:12:00:01 > "$g/functions/rndis.usb0/dev_addr"
echo 02:4a:34:12:00:02 > "$g/functions/rndis.usb0/host_addr"
mkdir -p "$g/functions/acm.usb0"
ln -sfn "$g/functions/rndis.usb0" "$g/configs/c.1/rndis.usb0"
ln -sfn "$g/functions/acm.usb0" "$g/configs/c.1/acm.usb0"
if ! echo "$udc" > "$g/UDC"; then
  # Some UDCs cannot allocate endpoints for RNDIS and ACM simultaneously.
  # Retry with RNDIS alone so the networking test is not lost to the console.
  echo '' > "$g/UDC" 2>/dev/null || true
  rm -f "$g/configs/c.1/acm.usb0"
  rmdir "$g/functions/acm.usb0" 2>/dev/null || true
  echo "debug-rndis: ACM unavailable; retrying RNDIS-only on $udc" >/dev/kmsg 2>/dev/null || true
  echo "$udc" > "$g/UDC" || {
    /usr/libexec/j4-debug-screen 'USB CONTROLLER FOUND' "$udc" 'GADGET BIND FAILED'
    exit 1
  }
fi

/usr/libexec/j4-debug-screen 'USB GADGET BOUND' "$udc" 'WAITING FOR RNDIS NETWORK'

# Configfs RNDIS normally creates usb0. Assign a fixed device address; the
# host can use 172.16.42.2/24 for this no-DHCP diagnostic.
netdev=''
for attempt in $(seq 1 15); do
  if [ -d /sys/class/net/usb0 ]; then
    netdev=usb0
    break
  fi
  sleep 1
done
[ -n "$netdev" ] || {
  /usr/libexec/j4-debug-screen 'USB GADGET BOUND' "$udc" 'RNDIS NETWORK INTERFACE MISSING'
  echo 'debug-rndis: RNDIS function did not create usb0' >/dev/kmsg 2>/dev/null || true
  exit 1
}
ip link set "$netdev" up
ip addr add 172.16.42.1/24 dev "$netdev" 2>/dev/null || ip addr replace 172.16.42.1/24 dev "$netdev"

# Wait briefly for configfs ACM to create the tty before systemd starts its
# getty-style direct shell service.
for attempt in $(seq 1 10); do
  [ -c /dev/ttyGS0 ] && break
  sleep 1
done

/usr/libexec/j4-debug-screen 'RNDIS IS ACTIVE' 'DEVICE: 172.16.42.1' "CONTROLLER: $udc" 'USB ACM SHELL MAY BE AVAILABLE'
echo "debug-rndis: gadget active on UDC $udc; RNDIS usb0=172.16.42.1" >/dev/kmsg 2>/dev/null || true
EOF
chmod 0755 "$root/usr/libexec/j4-debug-rndis"

cat > "$root/usr/libexec/j4-debug-usb-shell" <<'EOF'
#!/bin/sh
set -eu
for attempt in $(seq 1 20); do
  [ -c /dev/ttyGS0 ] && exec /bin/sh -i
  sleep 1
done
echo 'USB ACM console did not appear; use the kernel console emergency shell.' >&2
exit 1
EOF
chmod 0755 "$root/usr/libexec/j4-debug-usb-shell"

cat > "$root/etc/systemd/system/debug-rndis.service" <<'EOF'
[Unit]
Description=Configure J4 RNDIS and USB ACM debug gadget
DefaultDependencies=no
After=systemd-remount-fs.service
Before=emergency.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/libexec/j4-debug-rndis

[Install]
WantedBy=emergency.target
EOF

cat > "$root/etc/systemd/system/debug-usb-shell.service" <<'EOF'
[Unit]
Description=Interactive debug shell on the USB ACM serial function
DefaultDependencies=no
After=debug-rndis.service

[Service]
Type=simple
ExecStart=/usr/libexec/j4-debug-usb-shell
StandardInput=tty
StandardOutput=tty
StandardError=tty
TTYPath=/dev/ttyGS0
TTYReset=yes
TTYVHangup=yes
Restart=no
EOF

cat > "$root/etc/systemd/system/emergency.service.d/debug-shell.conf" <<'EOF'
[Unit]
Wants=debug-rndis.service
After=debug-rndis.service

[Service]
ExecStart=
ExecStart=-/bin/sh
StandardInput=tty
StandardOutput=tty
StandardError=tty
TTYPath=/dev/console
TTYReset=yes
TTYVHangup=yes
EOF

cat > "$root/etc/systemd/system/emergency.target.d/debug-rndis.conf" <<'EOF'
[Unit]
Wants=debug-rndis.service debug-usb-shell.service
After=debug-rndis.service
EOF

ln -sfn ../debug-rndis.service "$root/etc/systemd/system/emergency.target.wants/debug-rndis.service"
ln -sfn ../debug-usb-shell.service "$root/etc/systemd/system/emergency.target.wants/debug-usb-shell.service"
# Defense in depth: even if a bootloader injects a root= token, systemd cannot
# mount it or switch away from this diagnostic initramfs.
ln -sfn /dev/null "$root/etc/systemd/system/root.mount"
ln -sfn /dev/null "$root/etc/systemd/system/initrd-root-fs.target"
ln -sfn /dev/null "$root/etc/systemd/system/initrd-switch-root.target"
emergency_target="$root/usr/lib/systemd/system/emergency.target"
[[ -e $emergency_target ]] || emergency_target="$root/lib/systemd/system/emergency.target"
ln -sfn "${emergency_target#"$root"}" "$root/etc/systemd/system/default.target"

# Reject accidental root-switch machinery or an absent systemd runtime.
[[ -x "$root/usr/sbin/ip" || -x "$root/sbin/ip" ]] || { echo "iproute2 ip binary missing" >&2; exit 1; }
[[ -e "$root/usr/lib/systemd/system/emergency.target" || -e "$root/lib/systemd/system/emergency.target" ]] || {
  echo "systemd emergency.target unit missing" >&2; exit 1;
}

mkdir -p "$(dirname "$out")"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python3 "$script_dir/mknewc.py" "$root" "$out"
test -s "$out"
printf 'contents: arm64 systemd PID 1, initrd emergency shell, RNDIS + USB ACM gadget; no installed rootfs\n'
