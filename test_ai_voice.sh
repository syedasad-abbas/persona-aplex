#!/bin/bash

# =========================================================
# PersonaPlex Loopback Media Test
# =========================================================
# Records both loopback legs. This validates media attachment
# and relay playback, but it does not capture your terminal
# microphone. For real caller speech, use SIP/DID/softphone.
# =========================================================

set -u

FS_CONTAINER="persona-aplex-freeswitch-1"
AI_SERVICE="personaplex"
ESL_HOST="127.0.0.1"
ESL_PORT="8021"
ESL_PASSWORD="FS!Secure2026"

RECORD_FILE_A="/tmp/personaplex_test_a.wav"
RECORD_FILE_B="/tmp/personaplex_test_b.wav"
LOCAL_FILE_A="$HOME/personaplex_test_a.wav"
LOCAL_FILE_B="$HOME/personaplex_test_b.wav"
RECORD_SECONDS="${RECORD_SECONDS:-75}"
CALL_SECONDS=$((RECORD_SECONDS + 20))

fs_cli_cmd() {
  docker exec "$FS_CONTAINER" fs_cli \
    -H "$ESL_HOST" \
    -P "$ESL_PORT" \
    -p "$ESL_PASSWORD" \
    -x "$1"
}

echo "======================================="
echo " PersonaPlex Loopback Media Test"
echo "======================================="

echo
echo "[0/8] Waiting for PersonaPlex voice agent..."
READY=0
for _ in $(seq 1 120); do
  HEALTH=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' persona-aplex-personaplex-1 2>/dev/null || true)
  if [ "$HEALTH" = "healthy" ] || docker compose logs --tail=5000 "$AI_SERVICE" 2>/dev/null | grep -Eq "Voice agent ready"; then
    READY=1
    break
  fi
  sleep 5
done
if [ "$READY" -ne 1 ]; then
  echo "ERROR: PersonaPlex did not report readiness yet."
  echo "Recent logs:"
  docker compose logs --tail=120 "$AI_SERVICE" || true
  exit 1
fi

echo "Waiting for prewarmed PersonaPlex audio session..."
PREWARM_READY=0
for _ in $(seq 1 160); do
  if docker compose logs --tail=5000 "$AI_SERVICE" 2>/dev/null | grep -Eq "background-prewarm: PersonaPlex ready session queued"; then
    PREWARM_READY=1
    break
  fi
  sleep 5
done
if [ "$PREWARM_READY" -ne 1 ]; then
  echo "ERROR: PersonaPlex audio session did not prewarm yet."
  echo "Recent logs:"
  docker compose logs --tail=160 "$AI_SERVICE" || true
  exit 1
fi

echo "PersonaPlex container FD usage:"
docker exec persona-aplex-personaplex-1 sh -c 'printf "open_fds="; ls /proc/1/fd | wc -l; printf "limit="; ulimit -n' 2>/dev/null || true

echo
echo "[1/8] Cleaning old calls/files..."
fs_cli_cmd "hupall" >/dev/null 2>&1 || true
docker exec "$FS_CONTAINER" sh -c "rm -f '$RECORD_FILE_A' '$RECORD_FILE_B'" >/dev/null 2>&1 || true
rm -f "$LOCAL_FILE_A" "$LOCAL_FILE_B"
sleep 2

echo
echo "[2/8] Starting fresh loopback call..."
fs_cli_cmd "originate {personaplex_agent_allowed=true}loopback/persona_agent/default &sleep($((CALL_SECONDS * 1000)))"
sleep 3

echo
echo "[3/8] Fetching loopback UUIDs..."
CHANNELS=$(fs_cli_cmd "show channels")
UUID_A=$(echo "$CHANNELS" | grep "loopback/persona_agent-a" | cut -d',' -f1 | tail -n1 | tr -d '\r')
UUID_B=$(echo "$CHANNELS" | grep "loopback/persona_agent-b" | cut -d',' -f1 | tail -n1 | tr -d '\r')

if [ -z "$UUID_A" ] && [ -z "$UUID_B" ]; then
  echo "ERROR: No loopback UUID found. Current channels:"
  echo "$CHANNELS"
  exit 1
fi

echo "UUID_A: ${UUID_A:-not found}"
echo "UUID_B: ${UUID_B:-not found}"

echo
echo "[4/8] Starting recordings..."
if [ -n "$UUID_A" ]; then
  fs_cli_cmd "uuid_record $UUID_A start $RECORD_FILE_A"
fi
if [ -n "$UUID_B" ]; then
  fs_cli_cmd "uuid_record $UUID_B start $RECORD_FILE_B"
fi
sleep 2

echo
echo "[5/8] Waiting while relay runs"
echo "---------------------------------------"
echo "This loopback test does not capture your terminal microphone."
echo "With OUTBOUND_TEST_TONE=1, one recording should contain a short tone."
echo "Use a real SIP/DID/softphone call to test caller speech into the AI."
echo "---------------------------------------"
echo "Recording loopback legs for ${RECORD_SECONDS} seconds..."
sleep "$RECORD_SECONDS"

echo
echo "[6/8] Stopping recordings..."
if [ -n "$UUID_A" ]; then
  fs_cli_cmd "uuid_record $UUID_A stop $RECORD_FILE_A" || true
fi
if [ -n "$UUID_B" ]; then
  fs_cli_cmd "uuid_record $UUID_B stop $RECORD_FILE_B" || true
fi
sleep 2

echo
echo "[7/8] Copying WAV files..."
echo "Remote files:"
docker exec "$FS_CONTAINER" sh -c "ls -lh '$RECORD_FILE_A' '$RECORD_FILE_B' 2>/dev/null || true"

docker cp "$FS_CONTAINER:$RECORD_FILE_A" "$LOCAL_FILE_A" 2>/dev/null || true
docker cp "$FS_CONTAINER:$RECORD_FILE_B" "$LOCAL_FILE_B" 2>/dev/null || true

COPIED=0
if [ -f "$LOCAL_FILE_A" ]; then
  COPIED=1
  echo "Saved: $LOCAL_FILE_A"
  file "$LOCAL_FILE_A"
fi
if [ -f "$LOCAL_FILE_B" ]; then
  COPIED=1
  echo "Saved: $LOCAL_FILE_B"
  file "$LOCAL_FILE_B"
fi

if [ "$COPIED" -eq 0 ]; then
  echo "ERROR: Failed to copy WAV files. Check FreeSWITCH uuid_record output above."
  exit 1
fi

echo
echo "[8/8] Inspecting WAV audio levels..."
python3 - "$LOCAL_FILE_A" "$LOCAL_FILE_B" <<'PYWAV'
import audioop
import sys
import wave

for path in sys.argv[1:]:
    try:
        with wave.open(path, "rb") as wav:
            frames = wav.readframes(wav.getnframes())
            peak = audioop.max(frames, wav.getsampwidth()) if frames else 0
            rms = audioop.rms(frames, wav.getsampwidth()) if frames else 0
            duration = wav.getnframes() / float(wav.getframerate() or 1)
            print(f"{path}: duration={duration:.1f}s rate={wav.getframerate()}Hz peak={peak} rms={rms}")
            if peak == 0:
                print(f"{path}: SILENT")
            else:
                print(f"{path}: HAS AUDIO")
    except FileNotFoundError:
        print(f"{path}: missing")
PYWAV

echo
echo "Recent PersonaPlex media logs:"
docker compose logs --tail=300 "$AI_SERVICE" 2>/dev/null | \
  grep -E "uuid_audio_stream|Audio relay|Audio WS|PersonaPlex ready|Sent outbound|First FreeSWITCH|First encoded|First PersonaPlex|First playback|audio stats|final audio stats|mod_audio_stream|uuid_broadcast|Relay error|handshake|session busy|Too many open files|playback_bytes" || true

echo
echo "PersonaPlex container FD usage after test:"
docker exec persona-aplex-personaplex-1 sh -c 'printf "open_fds="; ls /proc/1/fd | wc -l; printf "limit="; ulimit -n' 2>/dev/null || true

echo
echo "======================================="
echo " Test Finished"
echo "======================================="
echo
echo "Expected for this loopback test:"
echo "  - At least one WAV should contain the outbound test tone."
echo "  - Terminal microphone speech will not be present."
echo
echo "For real two-way audio, place a SIP/DID/softphone call and inspect:"
echo "  docker compose logs -f personaplex"
echo "Look for fs_peak > 0 and playback_bytes > 0."
