#!/bin/sh
set -eu

log() {
  printf '%s\n' "[freeswitch-entrypoint] $*"
}

valid_ipv4() {
  case "$1" in
    ''|*[!0-9.]*) return 1 ;;
  esac
  awk -v ip="$1" 'BEGIN {
    n = split(ip, a, ".");
    if (n != 4) exit 1;
    for (i = 1; i <= 4; i++) {
      if (a[i] == "" || a[i] < 0 || a[i] > 255) exit 1;
    }
  }'
}

private_ipv4() {
  case "$1" in
    10.*|192.168.*|127.*|169.254.*) return 0 ;;
    172.*)
      second="$(printf '%s' "$1" | awk -F. '{print $2}')"
      [ "$second" -ge 16 ] 2>/dev/null && [ "$second" -le 31 ] 2>/dev/null && return 0
      ;;
  esac
  return 1
}

detect_public_ip() {
  if [ -n "${FS_PUBLIC_IP:-}" ]; then
    printf '%s' "$FS_PUBLIC_IP"
    return 0
  fi

  for url in \
    "${FS_PUBLIC_IP_URL:-https://api.ipify.org}" \
    "http://checkip.amazonaws.com" \
    "http://icanhazip.com"; do
    ip="$(wget -qO- --timeout=5 "$url" 2>/dev/null | tr -d '[:space:]' || true)"
    if valid_ipv4 "$ip" && ! private_ipv4 "$ip"; then
      printf '%s' "$ip"
      return 0
    fi
  done

  return 1
}

update_vars_file() {
  file="$1"
  ip="$2"
  [ -f "$file" ] || return 0

  sed -i -E \
    -e "s#data=\"external_rtp_ip=[^\"]*\"#data=\"external_rtp_ip=${ip}\"#" \
    -e "s#data=\"external_sip_ip=[^\"]*\"#data=\"external_sip_ip=${ip}\"#" \
    "$file"
}

if ip="$(detect_public_ip)"; then
  if valid_ipv4 "$ip"; then
    for vars_file in \
      /usr/local/freeswitch/vars.xml \
      /usr/local/freeswitch/conf/vars.xml; do
      update_vars_file "$vars_file" "$ip"
    done
    log "Using external_sip_ip/external_rtp_ip=${ip}"
  else
    log "Detected invalid public IP '${ip}', leaving vars.xml unchanged"
  fi
else
  log "Could not detect public IP, leaving vars.xml unchanged"
fi

exec "$@"
