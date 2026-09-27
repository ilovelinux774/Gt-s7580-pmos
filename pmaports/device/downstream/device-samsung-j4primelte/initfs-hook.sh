#!/bin/sh
# Run after pmOS's normal gadget setup. Don't reconfigure a working gadget.
mkdir -p /sys/kernel/debug
mount -t debugfs debugfs /sys/kernel/debug 2>/dev/null || true
if [ -w /sys/kernel/debug/msm_otg/mode ]; then
    echo peripheral > /sys/kernel/debug/msm_otg/mode || true
fi
for iface in rndis0 usb0; do
    [ -d "/sys/class/net/$iface" ] || continue
    mac=$(cat "/sys/class/net/$iface/address")
    if [ "$mac" = '00:00:00:00:00:00' ]; then
        ifconfig "$iface" down
        ifconfig "$iface" hw ether 02:4a:34:00:00:01
    fi
    ifconfig "$iface" 172.16.42.1 netmask 255.255.255.0 up
    break
done
echo 'j4primelte: initramfs device hook reached'
uname -a
cat /proc/cmdline
ifconfig -a
