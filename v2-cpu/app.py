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
import contextlib
import json

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
    stream_started = set()

    while True:
        try:
            esl = ESLClient(FS_ESL_HOST, FS_ESL_PORT, FS_ESL_PASSWORD)
            esl.connect()
            esl.subscribe("CHANNEL_CREATE CHANNEL_ANSWER CHANNEL_PARK CHANNEL_HANGUP CHANNEL_HANGUP_COMPLETE CUSTOM")
            log.info("ESL event loop running — watching for calls on FreeSWITCH %s:%d",
                     FS_ESL_HOST, FS_ESL_PORT)

            while esl.connected:
                for event in esl.read_events(timeout=1.0):
                    name = event.event_name
                    uuid = event.get("Unique-ID", "")

                    if name == "CUSTOM":
                        subclass = event.get("Event-Subclass", "")
                        if subclass.startswith("mod_audio_stream::"):
                            log.info("FreeSWITCH %s uuid=%s body=%s", subclass, uuid or "unknown", event.body[:500])

                            if subclass == "mod_audio_stream::play" and uuid:
                                file_path = event.get("file", "") or event.get("File", "")
                                payload = event.body or ""
                                start = payload.find("{")
                                end = payload.rfind("}")
                                if not file_path and start != -1 and end != -1 and end > start:
                                    try:
                                        play_data = json.loads(payload[start:end + 1])
                                        file_path = play_data.get("file", "")
                                    except json.JSONDecodeError:
                                        file_path = ""
                                if file_path:
                                        play_esl = ESLClient(FS_ESL_HOST, FS_ESL_PORT, FS_ESL_PASSWORD)
                                        try:
                                            play_esl.connect()
                                            play_result = play_esl.api("uuid_broadcast", f"{uuid} {file_path} aleg")
                                            log.info("Call %s: uuid_broadcast %s → %s", uuid, file_path, play_result.strip()[:200])
                                        finally:
                                            play_esl.close()
                        continue

                    if name in ("CHANNEL_ANSWER", "CHANNEL_PARK") and uuid and uuid not in stream_started:
                        dest_candidates = [
                            event.get("Caller-Destination-Number", ""),
                            event.get("variable_destination_number", ""),
                            event.get("variable_dialed_extension", ""),
                        ]
                        marker = event.get("variable_personaplex_agent", "").lower()
                        allowed = event.get("variable_personaplex_agent_allowed", "").lower()
                        is_agent_call = marker == "true" and allowed == "true"

                        if marker == "true" and not is_agent_call:
                            caller = event.get("Caller-Caller-ID-Number", "unknown")
                            called = next((dest for dest in dest_candidates if dest), "persona_agent")
                            log.warning(
                                "Call %s: Ignoring unapproved agent event=%s caller=%s called=%s marker=%s allowed=%s context=%s state=%s",
                                uuid,
                                name,
                                caller,
                                called,
                                marker,
                                allowed or "missing",
                                event.get("Caller-Context", ""),
                                event.get("Channel-Call-State", ""),
                            )

                        if is_agent_call:
                            stream_started.add(uuid)
                            caller = event.get("Caller-Caller-ID-Number", "unknown")
                            called = next((dest for dest in dest_candidates if dest), "persona_agent")
                            log.info(
                                "Call %s: Agent event=%s caller=%s called=%s marker=%s allowed=%s context=%s state=%s",
                                uuid,
                                name,
                                caller,
                                called,
                                marker,
                                allowed,
                                event.get("Caller-Context", ""),
                                event.get("Channel-Call-State", ""),
                            )

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

                            # Start audio streaming via mod_audio_stream.
                            # uuid_audio_stream <uuid> start <ws-url> mono 16000
                            stream_url = f"{relay_ws_url}?uuid={uuid}"
                            stream_args = f"{uuid} start {stream_url} mono 16000"
                            log.info("Call %s: Starting uuid_audio_stream args=%s", uuid, stream_args)
                            cmd_esl = ESLClient(FS_ESL_HOST, FS_ESL_PORT, FS_ESL_PASSWORD)
                            try:
                                cmd_esl.connect()
                                for var, value in (
                                    ("STREAM_MESSAGE_DEFLATE", "true"),
                                    ("STREAM_SUPPRESS_LOG", "false"),
                                ):
                                    set_result = cmd_esl.api("uuid_setvar", f"{uuid} {var} {value}")
                                    log.info("Call %s: uuid_setvar %s=%s -> %s", uuid, var, value, set_result.strip()[:120])
                                result = cmd_esl.api("uuid_audio_stream", stream_args)
                            finally:
                                cmd_esl.close()
                            result_text = result.strip()
                            if result_text.startswith("-ERR"):
                                log.error("Call %s: uuid_audio_stream failed: %s", uuid, result_text[:300])
                            else:
                                log.info("Call %s: uuid_audio_stream result: %s", uuid, result_text[:300])

                    elif name in ("CHANNEL_HANGUP", "CHANNEL_HANGUP_COMPLETE") and uuid:
                        stream_started.discard(uuid)
                        unregister_session(uuid)
                        log.info(
                            "Call %s: Hangup event=%s cause=%s disposition=%s",
                            uuid,
                            name,
                            event.get("Hangup-Cause", ""),
                            event.get("variable_endpoint_disposition", ""),
                        )

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
    relay_runner = {"runner": None}
    relay_ready = threading.Event()
    relay_error = {"error": None}
    relay_ready_timeout = int(os.getenv("RELAY_READY_TIMEOUT", "300"))

    async def _cleanup_relay():
        runner = relay_runner.get("runner")
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()
        tasks = [task for task in asyncio.all_tasks(relay_loop) if task is not asyncio.current_task(relay_loop)]
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)

    def _run_relay():
        asyncio.set_event_loop(relay_loop)
        try:
            relay_runner["runner"] = relay_loop.run_until_complete(start_relay_server(domain_config))
            relay_ready.set()
            relay_loop.run_forever()
        except Exception as e:
            relay_error["error"] = e
            relay_ready.set()
            log.error("Audio relay startup failed: %s", e)
        finally:
            relay_loop.run_until_complete(_cleanup_relay())
            relay_loop.run_until_complete(relay_loop.shutdown_asyncgens())
            relay_loop.close()

    relay_thread = threading.Thread(target=_run_relay, daemon=True)
    relay_thread.start()
    if not relay_ready.wait(timeout=relay_ready_timeout):
        log.error("Audio relay did not become ready within %ds", relay_ready_timeout)
        with contextlib.suppress(Exception):
            moshi_proc.terminate()
        sys.exit(1)
    if relay_error["error"] is not None:
        with contextlib.suppress(Exception):
            moshi_proc.terminate()
        sys.exit(1)

    # 3. Watch for moshi process death
    def _watch_moshi():
        moshi_proc.wait()
        if moshi_proc.returncode != 0:
            log.error("PersonaPlex exited with code %d", moshi_proc.returncode)
            os._exit(1)

    threading.Thread(target=_watch_moshi, daemon=True).start()

    def shutdown(sig, _frame):
        log.info("Shutting down (signal %s)...", sig)
        with contextlib.suppress(Exception):
            moshi_proc.terminate()
        if relay_loop.is_running():
            relay_loop.call_soon_threadsafe(relay_loop.stop)
            relay_thread.join(timeout=5)
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
