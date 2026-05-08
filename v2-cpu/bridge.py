"""
PersonaPlex ↔ FreeSWITCH audio bridge via mod_audio_stream.

Architecture:
  1. FreeSWITCH receives an inbound call and parks it (via dialplan)
  2. This bridge connects to FS via ESL inbound, detects the new call
  3. Starts PersonaPlex WebSocket session with domain text/voice prompts
  4. Runs `uuid_audio_stream` on FS to pipe the call's RTP audio
     directly to a local WebSocket relay
  5. The relay translates between FS raw audio and PersonaPlex's Opus protocol
  6. Agent text tokens are collected for transcript / post-call storage

mod_audio_stream sends raw L16 (PCM 16-bit signed, mono) over WebSocket.
PersonaPlex expects/sends Opus-encoded audio via its own WebSocket protocol.
The relay bridges these two WebSocket streams.
"""

import os
import asyncio
import logging
import time
import traceback
import json
import struct
import threading
from typing import Optional, List

import numpy as np
import sphn
import aiohttp
from aiohttp import web

import db

log = logging.getLogger("agent.bridge")

MOSHI_HOST = os.getenv("MOSHI_HOST", "127.0.0.1")
MOSHI_PORT = int(os.getenv("MOSHI_PORT", "8998"))
MOSHI_SSL = os.getenv("MOSHI_SSL", "0").lower() in ("1", "true", "yes")
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "600"))
RELAY_HOST = os.getenv("RELAY_HOST", "127.0.0.1")
RELAY_PORT = int(os.getenv("RELAY_PORT", "9001"))

# mod_audio_stream sends L16 at this rate
FS_SAMPLE_RATE = 16000
# PersonaPlex operates at 24kHz
MOSHI_SAMPLE_RATE = 24000


class CallSession:
    """Tracks one active call's bridge session."""

    def __init__(self, call_uuid: str, caller: str, called: str,
                 domain_config, call_id: Optional[int] = None):
        self.call_uuid = call_uuid
        self.caller = caller
        self.called = called
        self.domain_config = domain_config
        self.call_id = call_id
        self.transcript_tokens: List[str] = []
        self.started_at = time.time()
        self.moshi_ws = None
        self.opus_writer = sphn.OpusStreamWriter(MOSHI_SAMPLE_RATE)
        self.opus_reader = sphn.OpusStreamReader(MOSHI_SAMPLE_RATE)
        self._active = True

    @property
    def active(self):
        return self._active and (time.time() - self.started_at) < MAX_CALL_SECONDS

    def stop(self):
        self._active = False


# Active call sessions keyed by UUID
_sessions: dict[str, CallSession] = {}
_sessions_lock = threading.Lock()


async def start_relay_server(domain_config):
    """
    Start the local WebSocket relay server.

    mod_audio_stream connects here.  For each connection, we open a parallel
    WebSocket to PersonaPlex and shuttle audio between the two.

    Protocol from mod_audio_stream:
      - Binary frames: raw L16 PCM (16-bit signed LE, mono, at FS_SAMPLE_RATE)
      - Text frames: JSON metadata (connect, disconnect events)
      - We send back binary L16 PCM for playback into the call

    Protocol to PersonaPlex moshi.server:
      - Send:    0x01 + opus_bytes   (caller audio)
      - Receive: 0x01 + opus_bytes   (agent audio)
      - Receive: 0x02 + utf8_text    (agent text tokens)
      - Receive: 0x00                (handshake / ready)
    """
    app = web.Application()
    app.router.add_get("/audio", _handle_audio_ws)
    app["domain_config"] = domain_config

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, RELAY_HOST, RELAY_PORT)
    await site.start()
    log.info("Audio relay WebSocket server listening on ws://%s:%d/audio", RELAY_HOST, RELAY_PORT)
    return runner


async def _handle_audio_ws(request):
    """Handle one mod_audio_stream WebSocket connection (one per call)."""
    ws_fs = web.WebSocketResponse()
    await ws_fs.prepare(request)

    domain_config = request.app["domain_config"]
    session: Optional[CallSession] = None
    call_uuid = None

    try:
        # First message from mod_audio_stream is typically JSON metadata
        # with the call UUID.  Some versions send raw audio immediately.
        # We'll try to extract the UUID from query params or first text frame.
        call_uuid = request.query.get("uuid", "")

        # If UUID in query, look up the session
        if call_uuid:
            with _sessions_lock:
                session = _sessions.get(call_uuid)

        if not session:
            # Wait for a text frame with metadata
            async for msg in ws_fs:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        meta = json.loads(msg.data)
                        call_uuid = meta.get("uuid", meta.get("channelUUID", ""))
                    except (json.JSONDecodeError, AttributeError):
                        pass
                    if call_uuid:
                        with _sessions_lock:
                            session = _sessions.get(call_uuid)
                    break
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    # Raw audio arrived before metadata — use first registered session
                    with _sessions_lock:
                        if _sessions:
                            call_uuid, session = next(iter(_sessions.items()))
                    break
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    return ws_fs

        if not session:
            log.warning("Audio WS: no session found for uuid=%s, closing", call_uuid)
            await ws_fs.close()
            return ws_fs

        log.info("Call %s: Audio relay connected", call_uuid)

        # Open WebSocket to PersonaPlex
        text_prompt = session.domain_config.TEXT_PROMPT
        voice_prompt = os.getenv("VOICE_PROMPT", session.domain_config.DEFAULT_VOICE_PROMPT)
        protocol = "wss" if MOSHI_SSL else "ws"
        moshi_url = f"{protocol}://{MOSHI_HOST}:{MOSHI_PORT}/api/chat"
        params = {"text_prompt": text_prompt, "voice_prompt": voice_prompt}

        async with aiohttp.ClientSession() as http_session:
            async with http_session.ws_connect(moshi_url, params=params, timeout=30) as ws_moshi:
                log.info("Call %s: Connected to PersonaPlex", call_uuid)
                session.moshi_ws = ws_moshi

                # Wait for PersonaPlex handshake (0x00)
                async for msg in ws_moshi:
                    if msg.type == aiohttp.WSMsgType.BINARY and msg.data == b"\x00":
                        log.info("Call %s: PersonaPlex ready", call_uuid)
                        break
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        log.error("Call %s: PersonaPlex closed during handshake", call_uuid)
                        return ws_fs

                # Run bidirectional relay
                await asyncio.gather(
                    _relay_fs_to_moshi(ws_fs, ws_moshi, session),
                    _relay_moshi_to_fs(ws_moshi, ws_fs, session),
                )

    except Exception as e:
        log.error("Call %s: Relay error: %s\n%s", call_uuid, e, traceback.format_exc())
    finally:
        if session:
            session.stop()
            _finalize_call(session)
        if not ws_fs.closed:
            await ws_fs.close()
        log.info("Call %s: Audio relay disconnected", call_uuid)

    return ws_fs


async def _relay_fs_to_moshi(ws_fs, ws_moshi, session: CallSession):
    """Forward audio from FreeSWITCH → PersonaPlex."""
    try:
        async for msg in ws_fs:
            if not session.active:
                break
            if msg.type == aiohttp.WSMsgType.BINARY:
                # L16 PCM from FreeSWITCH (16-bit signed LE, mono, FS_SAMPLE_RATE)
                pcm_16k = np.frombuffer(msg.data, dtype=np.int16).astype(np.float32) / 32768.0

                # Resample 16kHz → 24kHz (linear interpolation)
                if FS_SAMPLE_RATE != MOSHI_SAMPLE_RATE:
                    n_out = int(len(pcm_16k) * MOSHI_SAMPLE_RATE / FS_SAMPLE_RATE)
                    indices = np.linspace(0, len(pcm_16k) - 1, n_out)
                    pcm_24k = np.interp(indices, np.arange(len(pcm_16k)), pcm_16k).astype(np.float32)
                else:
                    pcm_24k = pcm_16k

                # Encode to Opus and send to PersonaPlex
                session.opus_writer.append_pcm(pcm_24k)
                opus_data = session.opus_writer.read_bytes()
                if opus_data:
                    try:
                        await ws_moshi.send_bytes(b"\x01" + opus_data)
                    except (ConnectionError, asyncio.CancelledError):
                        break
            elif msg.type == aiohttp.WSMsgType.TEXT:
                # Metadata from mod_audio_stream (ignore)
                pass
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                break
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error("Call %s: FS→Moshi relay error: %s", session.call_uuid, e)
    finally:
        session.stop()


async def _relay_moshi_to_fs(ws_moshi, ws_fs, session: CallSession):
    """Forward audio from PersonaPlex → FreeSWITCH, collect text tokens."""
    try:
        async for msg in ws_moshi:
            if not session.active:
                break
            if msg.type == aiohttp.WSMsgType.BINARY:
                kind = msg.data[0]
                payload = msg.data[1:]
                if kind == 1:  # Agent audio (Opus)
                    session.opus_reader.append_bytes(payload)
                    pcm_24k = session.opus_reader.read_pcm()
                    if pcm_24k.shape[-1] > 0:
                        # Resample 24kHz → FS_SAMPLE_RATE
                        if FS_SAMPLE_RATE != MOSHI_SAMPLE_RATE:
                            n_out = int(len(pcm_24k) * FS_SAMPLE_RATE / MOSHI_SAMPLE_RATE)
                            indices = np.linspace(0, len(pcm_24k) - 1, n_out)
                            pcm_fs = np.interp(indices, np.arange(len(pcm_24k)), pcm_24k)
                        else:
                            pcm_fs = pcm_24k
                        # Convert to L16 (16-bit signed LE) for mod_audio_stream
                        l16 = (np.clip(pcm_fs, -1.0, 1.0) * 32767).astype(np.int16)
                        try:
                            await ws_fs.send_bytes(l16.tobytes())
                        except (ConnectionError, asyncio.CancelledError):
                            break
                elif kind == 2:  # Agent text token
                    text = payload.decode("utf-8", "replace")
                    session.transcript_tokens.append(text)
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                break
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error("Call %s: Moshi→FS relay error: %s", session.call_uuid, e)
    finally:
        session.stop()


def register_session(call_uuid: str, caller: str, called: str,
                     domain_config, call_id: Optional[int] = None) -> CallSession:
    """Register a new call session (called from the ESL event handler)."""
    session = CallSession(call_uuid, caller, called, domain_config, call_id)
    with _sessions_lock:
        _sessions[call_uuid] = session
    return session


def unregister_session(call_uuid: str):
    with _sessions_lock:
        session = _sessions.pop(call_uuid, None)
    if session:
        session.stop()
        _finalize_call(session)


def _finalize_call(session: CallSession):
    """Post-call: save transcript to DB."""
    transcript = "".join(session.transcript_tokens).strip()
    if session.call_id:
        try:
            db.end_call(
                session.call_id, "completed",
                transcript=transcript,
                summary=f"domain={session.domain_config.DOMAIN_NAME} tokens={len(session.transcript_tokens)}",
            )
            log.info("Call %s: Saved transcript (%d chars)", session.call_uuid, len(transcript))
        except Exception as e:
            log.error("Call %s: DB end_call failed: %s", session.call_uuid, e)
