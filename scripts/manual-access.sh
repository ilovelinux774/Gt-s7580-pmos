#!/bin/sh
# ---------------------------------------------------------------------------
# PASTE THIS INTO THE INITRAMFS TELNET / DEBUG SHELL
#
#   telnet 172.16.42.1            (debug shell, port 23)
#   telnet 172.16.42.1 24         (usb-shell hook, if you added it)
#
# It does three things:
#   1. Finishes the mount sequence by hand (pmos_continue_boot does NOT mount)
#   2. Rewrites the rootfs so root has no password and sshd lets you in
#   3. Continues the boot
#
# Steps 2's changes are written to DISK, so you only ever do this once.
# Afterwards every normal boot has sshd listening on 172.16.42.1.
# ---------------------------------------------------------------------------

set -x

. /init_functions.sh
. /etc/deviceinfo

# --- 1. finish mounting -----------------------------------------------------
# If any of these already ran (stowaway builds differ), they are safe to repeat.
find_root_partition
mount_root_partition /sysroot
mount --bind /sysroot/boot /boot
extract_initramfs_extra /boot/initramfs-extra

# --- 2. surgery on the rootfs ----------------------------------------------
cat > /sysroot/tmp/access.sh <<'INNER'
#!/bin/sh
set -x

# root: empty password (ssh asks, you press enter)
passwd -d root

# sshd: permit root + empty passwords
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/99-kylepro.conf <<'CONF'
PermitRootLogin yes
PermitEmptyPasswords yes
PasswordAuthentication yes
CONF
grep -q "sshd_config.d" /etc/ssh/sshd_config 2>/dev/null \
	|| printf '\nInclude /etc/ssh/sshd_config.d/*.conf\n' >> /etc/ssh/sshd_config

# re-assert the USB gadget address after switch_root
mkdir -p /etc/local.d
cat > /etc/local.d/10-usbnet.start <<'CONF'
#!/bin/sh
for i in rndis0 usb0 eth0; do
	ip link show "$i" >/dev/null 2>&1 || continue
	ip addr add 172.16.42.1/24 dev "$i" 2>/dev/null
	ip link set "$i" up 2>/dev/null
done
exit 0
CONF
chmod +x /etc/local.d/10-usbnet.start

# optional: paste YOUR pubkey on the line below for a truly prompt-free login
mkdir -p /root/.ssh && chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys
# echo "ssh-ed25519 AAAA... you@host" >> /root/.ssh/authorized_keys

rc-update add sshd default
rc-update add local default
INNER
chmod +x /sysroot/tmp/access.sh

mount -t proc proc /sysroot/proc 2>/dev/null
mount -t sysfs sysfs /sysroot/sys 2>/dev/null
mount -o bind /dev /sysroot/dev 2>/dev/null
chroot /sysroot /tmp/access.sh

# --- 3. continue booting ----------------------------------------------------
# Reminder: this ONLY kills the debug shell (pkill pmos_shell / pmos_loop_forever
# / telnetd). All the mounting had to happen above, first.
echo "root password cleared, sshd enabled. Running pmos_continue_boot."
pmos_continue_boot

# --- then, from your PC -----------------------------------------------------
#   sudo ip addr add 172.16.42.2/24 dev <usb-iface>
#   sudo ip link set <usb-iface> up
#   ssh -o StrictHostKeyChecking=no root@172.16.42.1
#   (password prompt -> just press Enter)
