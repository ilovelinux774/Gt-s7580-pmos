#!/bin/sh
# Configure Samsung's legacy android_usb RNDIS function and a point-to-point SSH link.
set -eu

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

log_file=/var/log/j4-rndis.log
android_usb=/sys/class/android_usb/android0
rndis_ip=172.16.42.1
host_ip=172.16.42.2
sshd_pidfile=/run/j4-rndis-sshd.pid
sshd_config=/etc/ssh/sshd_config.j4-rndis

log() {
  message="j4-rndis: $*"
  mkdir -p /var/log 2>/dev/null || true
  printf '%s\n' "$message" >> "$log_file" 2>/dev/null || true
  printf '%s\n' "$message" > /dev/kmsg 2>/dev/null || true
}

stop_access() {
  /usr/bin/start-stop-daemon --stop --quiet --pidfile "$sshd_pidfile" 2>/dev/null || true
  rm -f "$sshd_pidfile"
  if [ -w "$android_usb/enable" ]; then
    echo 0 > "$android_usb/enable" 2>/dev/null || true
  fi
  log 'stopped USB RNDIS and SSH'
}

start_access() {
  if [ ! -d "$android_usb" ]; then
    log "Android USB gadget sysfs node is missing: $android_usb"
    return 1
  fi
  if [ ! -w "$android_usb/functions" ] || [ ! -w "$android_usb/enable" ]; then
    log 'Android USB gadget functions/enable attributes are not writable'
    return 1
  fi

  # This kernel uses Samsung's legacy android_usb composite gadget, not configfs.
  echo 0 > "$android_usb/enable" 2>/dev/null || true
  if ! echo rndis > "$android_usb/functions"; then
    log 'kernel rejected functions=rndis'
    return 1
  fi
  if ! echo 1 > "$android_usb/enable"; then
    log 'kernel failed to enable the RNDIS gadget'
    return 1
  fi
  log 'enabled android_usb function rndis'

  netdev=''
  attempt=0
  while [ "$attempt" -lt 20 ]; do
    for candidate in rndis0 usb0; do
      if [ -d "/sys/class/net/$candidate" ]; then
        netdev=$candidate
        break
      fi
    done
    [ -n "$netdev" ] && break
    attempt=$((attempt + 1))
    sleep 1
  done
  if [ -z "$netdev" ]; then
    log 'RNDIS was enabled but no rndis0/usb0 network interface appeared'
    return 1
  fi

  if ! /usr/bin/ip address replace "$rndis_ip/24" dev "$netdev" ||
     ! /usr/bin/ip link set "$netdev" up; then
    log "could not configure $netdev at $rndis_ip/24"
    return 1
  fi
  log "RNDIS interface $netdev is up at $rndis_ip/24; host should use $host_ip/24"

  if [ ! -x /usr/bin/sshd ] || [ ! -x /usr/bin/ssh-keygen ]; then
    log 'RNDIS is up, but OpenSSH server/keygen are missing from the rootfs'
    return 1
  fi
  if ! grep -q '^alarm:' /etc/passwd; then
    log 'RNDIS is up, but the expected Arch Linux ARM alarm account is missing'
    return 1
  fi

  mkdir -p /run/sshd /var/empty
  /usr/bin/ssh-keygen -A >> "$log_file" 2>&1
  if ! /usr/bin/sshd -t -f "$sshd_config" >> "$log_file" 2>&1; then
    log 'sshd configuration validation failed'
    return 1
  fi
  /usr/bin/start-stop-daemon --start --quiet --background --make-pidfile \
    --pidfile "$sshd_pidfile" --exec /usr/bin/sshd -- \
    -D -f "$sshd_config"
  sleep 1
  if ! /usr/bin/start-stop-daemon --status --quiet --pidfile "$sshd_pidfile"; then
    log 'sshd did not remain running'
    return 1
  fi
  log "SSH is listening on $rndis_ip; use the existing alarm account"
}

case "${1:-start}" in
  start) start_access ;;
  stop) stop_access ;;
  restart)
    stop_access
    start_access
    ;;
  *)
    echo "usage: $0 {start|stop|restart}" >&2
    exit 2
    ;;
esac
