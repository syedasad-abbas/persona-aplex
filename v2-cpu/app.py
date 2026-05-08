#!/usr/bin/env python3
"""
personaplex-7b-v1 Voice Agent — main entry point.

Launches:
  1. PersonaPlex moshi.server  — speech-to-speech model (WebSocket on :8998)
  2. Audio relay server         — bridges mod_audio_stream ↔ PersonaPlex (:9001)
  3. ESL inbound event loop     — connects to FreeSWITCH :8021, watches for calls,
                                  runs uuid_audio_stream to pipe call audio to relay
"""

import os
import sys
import signal
import logging
import subprocess
import time
import asyncio
import threading

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("agent")

MOSHI_DEVICE = os.getenv("MOSHI_DEVICE", "cpu")
MOSHI_HOST = os.getenv("MOSHI_HOST", "0.0.0.0")
MOSHI_PORT = int(os.getenv("MOSHI_PORT", "8998"))
FS_ESL_HOST = os.getenv("FS_ESL_HOST", "127.0.0.1")
FS_ESL_PORT = int(os.getenv("FS_ESL_PORT", "8021"))
FS_ESL_PASSWORD = os.getenv("FS_ESL_PASSWORD", "FS!Secure2026")
RELAY_HOST = os.getenv("RELAY_HOST", "127.0.0.1")
RELAY_PORT = int(os.getenv("RELAY_PORT", "9001"))
AGENT_DOMAIN = os.getenv("AGENT_DOMAIN", "appointment")

# Dialplan context/extension that parks calls for the agent
# Calls matching this destination are handled by the bridge
AGENT_DEST_PATTERN = os.getenv("AGENT_DEST_PATTERN", "^persona_agent$")


def _get_domain_config():
    if AGENT_DOMAIN == "appointment":
        from domains import appointment
        return appointment
    raise ValueError(f"Unknown domain: {AGENT_DOMAIN}")


# ---------------------------------------------------------------------------
# PersonaPlex subprocess
# ---------------------------------------------------------------------------

def _start_moshi_server():
    cmd = [
        sys.executable, "-m", "moshi.server",
        "--host", MOSHI_HOST,
        "--port", str(MOSHI_PORT),
        "--device", MOSHI_DEVICE,
        "--static", "none",
    ]
    if os.getenv("MOSHI_CPU_OFFLOAD", "").lower() in ("1", "true", "yes"):
        cmd.append("--cpu-offload")
    log.info("Starting PersonaPlex: %s", " ".join(cmd))
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)


def _stream_logs(proc):
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log.info("[moshi] %s", line)
    except Exception:
        pass


def _wait_for_moshi(timeout=600):
    import socket as _socket
    start = time.time()
    # When moshi listens on 0.0.0.0 we probe localhost
    probe_host = "127.0.0.1" if MOSHI_HOST in ("0.0.0.0", "::") else MOSHI_HOST
    while (time.time() - start) < timeout:
        try:
            s = _socket.create_connection((probe_host, MOSHI_PORT), timeout=2)
            s.close()
            return True
        except (ConnectionRefusedError, OSError, _socket.timeout):
            time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# ESL event loop — watches for calls and starts audio streaming
# ---------------------------------------------------------------------------

def _esl_event_loop(domain_config):
    """
    Connect to FreeSWITCH ESL, subscribe to call events.
    When a call lands on the agent extension, run uuid_audio_stream
    to pipe audio to the relay WebSocket.
    """
    from esl_client import ESLClient
    from bridge import register_session, unregister_session, FS_SAMPLE_RATE
    import db
    import re

    relay_ws_url = f"ws://{RELAY_HOST}:{RELAY_PORT}/audio"
    text_prompt = domain_config.TEXT_PROMPT
    voice_prompt = os.getenv("VOICE_PROMPT", domain_config.DEFAULT_VOICE_PROMPT)

    while True:
        try:
            esl = ESLClient(FS_ESL_HOST, FS_ESL_PORT, FS_ESL_PASSWORD)
            esl.connect()
            esl.subscribe("CHANNEL_CREATE CHANNEL_ANSWER CHANNEL_HANGUP CHANNEL_HANGUP_COMPLETE")
            log.info("ESL event loop running — watching for calls on FreeSWITCH %s:%d",
                     FS_ESL_HOST, FS_ESL_PORT)

            while esl.connected:
                for event in esl.read_events(timeout=1.0):
                    name = event.event_name
                    uuid = event.get("Unique-ID", "")

                    if name == "CHANNEL_ANSWER" and uuid:
                        # Check if this call is destined for our agent
                        dest = event.get("Caller-Destination-Number", "")
                        direction = event.get("Call-Direction", "")
                        context = event.get("Caller-Context", "")

                        # Match calls routed to our agent extension
                        if re.match(AGENT_DEST_PATTERN, dest):
                            caller = event.get("Caller-Caller-ID-Number", "unknown")
                            called = event.get("Caller-Destination-Number", "unknown")
                            log.info("Call %s: Incoming %s → %s — starting PersonaPlex session",
                                     uuid, caller, called)

                            # Register in DB
                            call_id = None
                            try:
                                call_id = db.create_call(
                                    uuid, caller, called,
                                    domain_config.DOMAIN_NAME, voice_prompt, text_prompt,
                                )
                            except Exception as e:
                                log.error("Call %s: DB create failed: %s", uuid, e)

                            # Register session for the relay
                            register_session(uuid, caller, called, domain_config, call_id)

                            # Start audio streaming via mod_audio_stream
                            # uuid_audio_stream <uuid> start <ws-url> both 16000
                            stream_url = f"{relay_ws_url}?uuid={uuid}"
                            result = esl.api(
                                "uuid_audio_stream",
                                f"{uuid} start {stream_url} both {FS_SAMPLE_RATE}",
                            )
                            log.info("Call %s: uuid_audio_stream → %s", uuid, result.strip()[:200])

                    elif name in ("CHANNEL_HANGUP", "CHANNEL_HANGUP_COMPLETE") and uuid:
                        unregister_session(uuid)
                        log.info("Call %s: Hangup", uuid)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error("ESL connection error: %s — reconnecting in 5s", e)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_server():
    domain_config = _get_domain_config()
    log.info("Domain: %s", domain_config.DOMAIN_NAME)
    log.info("PersonaPlex: device=%s, port=%d", MOSHI_DEVICE, MOSHI_PORT)
    log.info("FreeSWITCH ESL: %s:%d", FS_ESL_HOST, FS_ESL_PORT)
    log.info("Audio relay: ws://%s:%d/audio", RELAY_HOST, RELAY_PORT)

    # 1. Start PersonaPlex moshi.server
    moshi_proc = _start_moshi_server()
    threading.Thread(target=_stream_logs, args=(moshi_proc,), daemon=True).start()

    log.info("Waiting for PersonaPlex model to load (may take minutes on CPU)...")
    if not _wait_for_moshi(timeout=600):
        log.error("PersonaPlex failed to start")
        moshi_proc.terminate()
        sys.exit(1)
    log.info("PersonaPlex ready")

    # 2. Start audio relay server (async)
    from bridge import start_relay_server
    relay_loop = asyncio.new_event_loop()

    def _run_relay():
        asyncio.set_event_loop(relay_loop)
        relay_loop.run_until_complete(start_relay_server(domain_config))
        relay_loop.run_forever()

    relay_thread = threading.Thread(target=_run_relay, daemon=True)
    relay_thread.start()
    time.sleep(1)

    # 3. Watch for moshi process death
    def _watch_moshi():
        moshi_proc.wait()
        if moshi_proc.returncode != 0:
            log.error("PersonaPlex exited with code %d", moshi_proc.returncode)
            os._exit(1)

    threading.Thread(target=_watch_moshi, daemon=True).start()

    def shutdown(sig, _frame):
        log.info("Shutting down (signal %s)...", sig)
        moshi_proc.terminate()
        relay_loop.call_soon_threadsafe(relay_loop.stop)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("Voice agent ready — connect FreeSWITCH calls to 'persona_agent' extension")

    # 4. Run ESL event loop (blocks main thread)
    _esl_event_loop(domain_config)


def run_offline():
    """Offline test: process a WAV through PersonaPlex directly."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-wav", required=True)
    parser.add_argument("--output-wav", required=True)
    parser.add_argument("--output-text", default="output.json")
    args, remaining = parser.parse_known_args(sys.argv[2:])
    domain_config = _get_domain_config()
    voice = os.getenv("VOICE_PROMPT", domain_config.DEFAULT_VOICE_PROMPT)
    cmd = [
        sys.executable, "-m", "moshi.offline",
        "--input-wav", args.input_wav,
        "--output-wav", args.output_wav,
        "--output-text", args.output_text,
        "--voice-prompt", voice,
        "--text-prompt", domain_config.TEXT_PROMPT,
        "--device", MOSHI_DEVICE,
    ]
    if os.getenv("MOSHI_CPU_OFFLOAD", "").lower() in ("1", "true", "yes"):
        cmd.append("--cpu-offload")
    cmd.extend(remaining)
    log.info("Offline: %s", " ".join(cmd))
    os.execvp(sys.executable, cmd)


if __name__ == "__main__":
    if "--offline" in sys.argv:
        run_offline()
    else:
        run_server()
