#!/bin/sh
set -eu

HOST="${1:-${SIP_HOST:-127.0.0.1}}"
PORT="${2:-${SIP_PORT:-5060}}"
USER="${3:-${SIP_USER:-1000}}"
PASS="${4:-${SIP_PASS:-FS!Secure2026}}"
DOMAIN="${5:-${SIP_DOMAIN:-$HOST}}"
TIMEOUT="${SIP_TIMEOUT:-3}"

if ! command -v nc >/dev/null 2>&1; then
  echo "nc is required for this test" >&2
  exit 2
fi

if ! command -v md5sum >/dev/null 2>&1; then
  echo "md5sum is required for digest auth" >&2
  exit 2
fi

request_uri="sip:${DOMAIN}"
if [ "$PORT" != "5060" ]; then
  request_uri="sip:${DOMAIN}:${PORT}"
fi

call_id="register-test-$$-$(date +%s)@persona-aplex"
from_tag="rt$$"
contact="sip:${USER}@127.0.0.1:5099"

send_register() {
  cseq="$1"
  auth_header="${2:-}"
  branch="z9hG4bK-rt-$$-${cseq}-$(date +%s)"

  {
    printf 'REGISTER %s SIP/2.0\r\n' "$request_uri"
    printf 'Via: SIP/2.0/UDP 127.0.0.1:5099;branch=%s;rport\r\n' "$branch"
    printf 'Max-Forwards: 70\r\n'
    printf 'From: <sip:%s@%s>;tag=%s\r\n' "$USER" "$DOMAIN" "$from_tag"
    printf 'To: <sip:%s@%s>\r\n' "$USER" "$DOMAIN"
    printf 'Call-ID: %s\r\n' "$call_id"
    printf 'CSeq: %s REGISTER\r\n' "$cseq"
    printf 'Contact: <%s>\r\n' "$contact"
    printf 'Expires: 120\r\n'
    printf 'User-Agent: persona-aplex-register-test\r\n'
    if [ -n "$auth_header" ]; then
      printf '%s\r\n' "$auth_header"
    fi
    printf 'Content-Length: 0\r\n'
    printf '\r\n'
  } | nc -u -w "$TIMEOUT" "$HOST" "$PORT" || true
}

md5() {
  printf '%s' "$1" | md5sum | awk '{print $1}'
}

field() {
  name="$1"
  printf '%s' "$2" | sed -n "s/.*${name}=\"\\([^\"]*\\)\".*/\\1/p"
}

first_response="$(send_register 1)"
first_status="$(printf '%s\n' "$first_response" | tr -d '\r' | awk 'NF {print; exit}')"

if [ -z "$first_status" ]; then
  echo "No SIP response from ${HOST}:${PORT} for REGISTER ${request_uri}" >&2
  exit 1
fi

echo "First response: ${first_status}"

case "$first_status" in
  *" 200 "*)
    echo "REGISTER accepted without challenge."
    exit 0
    ;;
  *" 401 "*|*" 407 "*)
    ;;
  *)
    printf '%s\n' "$first_response" | tr -d '\r' >&2
    exit 1
    ;;
esac

challenge="$(printf '%s\n' "$first_response" | tr -d '\r' | awk '/^(WWW-Authenticate|Proxy-Authenticate):/ {sub(/^[^:]+:[[:space:]]*/, ""); print; exit}')"
realm="$(field realm "$challenge")"
nonce="$(field nonce "$challenge")"
qop="$(field qop "$challenge" | awk -F, '{print $1}')"
opaque="$(field opaque "$challenge")"

if [ -z "$realm" ] || [ -z "$nonce" ]; then
  echo "Could not parse SIP digest challenge" >&2
  printf '%s\n' "$first_response" | tr -d '\r' >&2
  exit 1
fi

echo "Challenge realm: ${realm}"

ha1="$(md5 "${USER}:${realm}:${PASS}")"
ha2="$(md5 "REGISTER:${request_uri}")"
cnonce="$(md5 "$call_id:$nonce")"
nc_value="00000001"

if [ -n "$qop" ]; then
  digest_response="$(md5 "${ha1}:${nonce}:${nc_value}:${cnonce}:${qop}:${ha2}")"
  auth_header="Authorization: Digest username=\"${USER}\", realm=\"${realm}\", nonce=\"${nonce}\", uri=\"${request_uri}\", response=\"${digest_response}\", algorithm=MD5, qop=${qop}, nc=${nc_value}, cnonce=\"${cnonce}\""
else
  digest_response="$(md5 "${ha1}:${nonce}:${ha2}")"
  auth_header="Authorization: Digest username=\"${USER}\", realm=\"${realm}\", nonce=\"${nonce}\", uri=\"${request_uri}\", response=\"${digest_response}\", algorithm=MD5"
fi

if [ -n "$opaque" ]; then
  auth_header="${auth_header}, opaque=\"${opaque}\""
fi

second_response="$(send_register 2 "$auth_header")"
second_status="$(printf '%s\n' "$second_response" | tr -d '\r' | awk 'NF {print; exit}')"

if [ -z "$second_status" ]; then
  echo "No SIP response after authenticated REGISTER" >&2
  exit 1
fi

echo "Second response: ${second_status}"

case "$second_status" in
  *" 200 "*)
    echo "Authenticated REGISTER succeeded for ${USER}@${DOMAIN}."
    ;;
  *)
    printf '%s\n' "$second_response" | tr -d '\r' >&2
    exit 1
    ;;
esac
