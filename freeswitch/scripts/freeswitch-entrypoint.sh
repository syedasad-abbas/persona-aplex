#!/bin/sh
set -eu

log() {
  level="${1:-INFO}"
  shift || true
  printf '%s %s %s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$level" "freeswitch-entrypoint" "$*"
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

detect_host_ip() {
  ip -4 route get 1.1.1.1 2>/dev/null |
    awk '{
      for (i = 1; i <= NF; i++) {
        if ($i == "src" && (i + 1) <= NF) {
          print $(i + 1)
          exit
        }
      }
    }'
}


update_vars_file() {
  file="$1"
  ip="$2"
  sip_domain="$3"
  [ -f "$file" ] || return 0

  sed -i -E \
    -e "s#data=\"external_rtp_ip=[^\"]*\"#data=\"external_rtp_ip=${ip}\"#" \
    -e "s#data=\"external_sip_ip=[^\"]*\"#data=\"external_sip_ip=${ip}\"#" \
    -e "s#data=\"domain_name=[^\"]*\"#data=\"domain_name=${sip_domain}\"#" \
    -e "s#data=\"domain=[^\"]*\"#data=\"domain=${sip_domain}\"#" \
    "$file"
}

ip="$(detect_host_ip || true)"

if ! valid_ipv4 "$ip"; then
  log CRITICAL "Could not detect a valid host IPv4 address"
  exit 1
fi

sip_domain="$ip"

for vars_file in \
  /usr/local/freeswitch/vars.xml \
  /usr/local/freeswitch/conf/vars.xml; do
  update_vars_file "$vars_file" "$ip" "$sip_domain"
done

log INFO "Using external_sip_ip/external_rtp_ip=${ip}, domain=${sip_domain}"

exec "$@"
