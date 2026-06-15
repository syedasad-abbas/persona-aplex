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
import base64
import contextlib
import asyncio
import logging
import math
import time
import traceback
import json
import re
import struct
import subprocess
import tempfile
import threading
import wave
from typing import Optional, List

import numpy as np
import sphn
import aiohttp
from aiohttp import web

import db

log = logging.getLogger("agent.bridge")

MOSHI_HOST = os.getenv("MOSHI_HOST", "127.0.0.1")
MOSHI_CONNECT_HOST = "127.0.0.1" if MOSHI_HOST in ("0.0.0.0", "::") else MOSHI_HOST
MOSHI_PORT = int(os.getenv("MOSHI_PORT", "8998"))
MOSHI_SSL = os.getenv("MOSHI_SSL", "0").lower() in ("1", "true", "yes")
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "600"))
RELAY_HOST = os.getenv("RELAY_HOST", "127.0.0.1")
RELAY_BIND_HOST = os.getenv("RELAY_BIND_HOST", "0.0.0.0")
RELAY_PORT = int(os.getenv("RELAY_PORT", "9001"))
OUTBOUND_TEST_TONE = os.getenv("OUTBOUND_TEST_TONE", "0").lower() in ("1", "true", "yes")
OUTBOUND_TEST_TONE_DELAY = float(os.getenv("OUTBOUND_TEST_TONE_DELAY", "0"))
OUTBOUND_TEST_TONE_DURATION_MS = int(os.getenv("OUTBOUND_TEST_TONE_DURATION_MS", "2500"))
OUTBOUND_TEST_TONE_CALLER = os.getenv("OUTBOUND_TEST_TONE_CALLER", "")
MOSHI_HANDSHAKE_TIMEOUT = float(os.getenv("MOSHI_HANDSHAKE_TIMEOUT", "90"))
MOSHI_PREWARM = os.getenv("MOSHI_PREWARM", "1").lower() in ("1", "true", "yes")
MOSHI_PREWARM_TIMEOUT = float(os.getenv("MOSHI_PREWARM_TIMEOUT", "180"))
MOSHI_BUSY_WAIT_TIMEOUT = float(os.getenv("MOSHI_BUSY_WAIT_TIMEOUT", str(MOSHI_PREWARM_TIMEOUT)))
INBOUND_GAIN = float(os.getenv("INBOUND_GAIN", "1.0"))
OUTBOUND_GAIN = float(os.getenv("OUTBOUND_GAIN", "1.0"))
AUDIO_STATS_INTERVAL = float(os.getenv("AUDIO_STATS_INTERVAL", "5"))
FS_ESL_HOST = os.getenv("FS_ESL_HOST", "127.0.0.1")
FS_ESL_PORT = int(os.getenv("FS_ESL_PORT", "8021"))
FS_ESL_PASSWORD = os.getenv("FS_ESL_PASSWORD", "FS!Secure2026")
FS_TEMP_DIR = os.getenv("FS_TEMP_DIR", "/tmp")
FS_PLAYBACK_BROADCAST_FALLBACK = os.getenv("FS_PLAYBACK_BROADCAST_FALLBACK", "1").lower() in ("1", "true", "yes")
FS_PLAYBACK_BROADCAST_DELAY = float(os.getenv("FS_PLAYBACK_BROADCAST_DELAY", "0.25"))
AUDIBLE_ACTIVE_THRESHOLD = int(os.getenv("AUDIBLE_ACTIVE_THRESHOLD", "500"))
AUDIBLE_MIN_ACTIVE_MS = float(os.getenv("AUDIBLE_MIN_ACTIVE_MS", "250"))
EXPECTED_AI_PHRASE = os.getenv("EXPECTED_AI_PHRASE", "thank you").strip()
SYSTEM_PROMPTS_SKIPPED = os.getenv("MOSHI_SKIP_SYSTEM_PROMPTS", "0").lower() in ("1", "true", "yes")
VERIFIED_AI_RESPONSE_ENABLED = os.getenv("VERIFIED_AI_RESPONSE_ENABLED", "1").lower() in ("1", "true", "yes")
VERIFIED_AI_RESPONSE_MODE = os.getenv("VERIFIED_AI_RESPONSE_MODE", "assist").strip().lower()
VERIFIED_AI_RESPONSE_TEXT = os.getenv(
    "VERIFIED_AI_RESPONSE_TEXT",
    "Hello, this is Alex from HealthFirst Medical Center. "
    "I can help book your appointment. May I have your full name?",
).strip()
VERIFIED_AI_RESPONSE_VOICE = os.getenv("VERIFIED_AI_RESPONSE_VOICE", "slt").strip() or "slt"
VERIFIED_AI_RESPONSE_TRIGGER_ACTIVE_MS = float(os.getenv("VERIFIED_AI_RESPONSE_TRIGGER_ACTIVE_MS", "250"))
VERIFIED_AI_RESPONSE_TRIGGER_PEAK = int(os.getenv("VERIFIED_AI_RESPONSE_TRIGGER_PEAK", str(AUDIBLE_ACTIVE_THRESHOLD)))
VERIFIED_AI_RESPONSE_SUPPRESS_MOSHI_AFTER = os.getenv(
    "VERIFIED_AI_RESPONSE_SUPPRESS_MOSHI_AFTER", "1"
).lower() in ("1", "true", "yes")

# mod_audio_stream sends L16 at this rate
FS_SAMPLE_RATE = 16000
# Match the uuid_audio_stream sample rate unless explicitly overridden.
FS_PLAYBACK_SAMPLE_RATE = int(os.getenv("FS_PLAYBACK_SAMPLE_RATE", str(FS_SAMPLE_RATE)))
# PersonaPlex operates at 24kHz.
MOSHI_SAMPLE_RATE = 24000


class AudioProbe:
    """Compact audio evidence for a stream without keeping every sample in memory."""

    def __init__(self):
        self.frames = 0
        self.bytes = 0
        self.samples = 0
        self.duration_ms = 0.0
        self.active_ms = 0.0
        self.peak = 0
        self.sum_squares = 0.0
        self.clip_samples = 0

    def add_pcm_float(self, pcm, sample_rate: int, byte_count: int = 0):
        arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
        if arr.size == 0 or sample_rate <= 0:
            return
        clipped = np.clip(arr, -1.0, 1.0)
        abs_i16 = np.abs(clipped) * 32767.0
        self.frames += 1
        self.bytes += int(byte_count)
        self.samples += int(arr.size)
        self.duration_ms += (arr.size * 1000.0) / sample_rate
        self.active_ms += (int(np.count_nonzero(abs_i16 >= AUDIBLE_ACTIVE_THRESHOLD)) * 1000.0) / sample_rate
        self.peak = max(self.peak, int(np.max(abs_i16)))
        self.sum_squares += float(np.sum(np.square(clipped, dtype=np.float64)))
        self.clip_samples += int(np.count_nonzero(abs_i16 >= 32700))

    @property
    def rms(self) -> float:
        if self.samples <= 0:
            return 0.0
        return math.sqrt(self.sum_squares / self.samples) * 32767.0

    @property
    def rms_dbfs(self) -> float:
        rms = self.rms
        if rms <= 0:
            return -120.0
        return 20.0 * math.log10(min(rms / 32767.0, 1.0))

    @property
    def active_pct(self) -> float:
        if self.duration_ms <= 0:
            return 0.0
        return min(100.0, (self.active_ms / self.duration_ms) * 100.0)

    @property
    def audible(self) -> bool:
        return self.peak >= AUDIBLE_ACTIVE_THRESHOLD and self.active_ms >= AUDIBLE_MIN_ACTIVE_MS


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
        self.finalized = False
        self.fs_audio_frames = 0
        self.fs_audio_bytes = 0
        self.fs_audio_peak = 0
        self.fs_text_frames = 0
        self.fs_binary_frames = 0
        self.opus_to_moshi_frames = 0
        self.pending_opus_frames: List[bytes] = []
        self.moshi_audio_frames = 0
        self.moshi_text_frames = 0
        self.playback_frames = 0
        self.playback_bytes = 0
        self.last_audio_log = 0.0
        self.first_fs_audio_logged = False
        self.first_opus_logged = False
        self.first_buffered_opus_logged = False
        self.first_moshi_audio_logged = False
        self.first_playback_logged = False
        self.stream_audio_messages = 0
        self.stream_audio_bytes = 0
        self.fs_play_events = 0
        self.last_play_event_file = ""
        self.ai_pcm_chunks: List[bytes] = []
        self.caller_pcm_chunks: List[bytes] = []
        self.caller_probe = AudioProbe()
        self.ai_probe = AudioProbe()
        self.verified_response_triggered = False
        self.verified_response_sent = False
        self.verified_response_text = ""
        self.verified_response_source = ""
        self.verified_response_reason = ""
        self.verified_response_bytes = 0
        self.verified_response_frames = 0
        self.moshi_audio_suppressed = 0
        self.moshi_text_suppressed = 0

    @property
    def active(self):
        return self._active and (time.time() - self.started_at) < MAX_CALL_SECONDS

    def stop(self):
        self._active = False


# Active call sessions keyed by UUID
_sessions: dict[str, CallSession] = {}
_sessions_lock = threading.Lock()
_moshi_session_lock: Optional[asyncio.Lock] = None


def _get_moshi_session_lock() -> asyncio.Lock:
    """PersonaPlex moshi.server serializes sessions; do not queue sockets forever."""
    global _moshi_session_lock
    if _moshi_session_lock is None:
        _moshi_session_lock = asyncio.Lock()
    return _moshi_session_lock


class PreparedMoshiSession:
    def __init__(self, http_session: aiohttp.ClientSession, ws_moshi, created_at: float):
        self.http_session = http_session
        self.ws_moshi = ws_moshi
        self.created_at = created_at

    async def close(self):
        with contextlib.suppress(Exception):
            await self.ws_moshi.close()
        with contextlib.suppress(Exception):
            await self.http_session.close()


def _stream_audio_message(raw_audio: bytes, sample_rate: int = FS_PLAYBACK_SAMPLE_RATE):
    return {
        "type": "streamAudio",
        "data": {
            "audioDataType": "raw",
            "sampleRate": sample_rate,
            "audioData": base64.b64encode(raw_audio).decode("ascii"),
        },
    }


def _raw_audio_ext(sample_rate: int) -> str:
    if sample_rate % 1000 == 0:
        return f".r{sample_rate // 1000}"
    return f".r{sample_rate}"


async def _broadcast_stream_audio_file_later(session: CallSession, stream_index: int):
    if not FS_PLAYBACK_BROADCAST_FALLBACK:
        return
    await asyncio.sleep(FS_PLAYBACK_BROADCAST_DELAY)
    file_path = f"{FS_TEMP_DIR.rstrip('/')}/{session.call_uuid}_{stream_index}.tmp{_raw_audio_ext(FS_PLAYBACK_SAMPLE_RATE)}"

    def _broadcast():
        from esl_client import ESLClient
        esl = ESLClient(FS_ESL_HOST, FS_ESL_PORT, FS_ESL_PASSWORD)
        try:
            esl.connect()
            return esl.api("uuid_broadcast", f"{session.call_uuid} {file_path} aleg")
        finally:
            esl.close()

    try:
        result = await asyncio.to_thread(_broadcast)
        result_text = result.strip()
        if result_text.startswith("+OK"):
            session.fs_play_events += 1
            session.last_play_event_file = file_path
        log.info(
            "Call %s: fallback uuid_broadcast streamAudio file index=%d path=%s -> %s playback_confirmed=%s",
            session.call_uuid,
            stream_index,
            file_path,
            result_text[:200],
            result_text.startswith("+OK"),
        )
    except Exception as e:
        log.warning("Call %s: fallback uuid_broadcast failed for %s: %s", session.call_uuid, file_path, e)


async def _send_stream_audio(ws_fs, session: CallSession, raw_audio: bytes):
    stream_index = session.stream_audio_messages
    session.stream_audio_messages += 1
    await ws_fs.send_str(json.dumps(_stream_audio_message(raw_audio)))
    session.stream_audio_bytes += len(raw_audio)
    if FS_PLAYBACK_BROADCAST_FALLBACK:
        asyncio.create_task(_broadcast_stream_audio_file_later(session, stream_index))


_verified_response_cache: dict[tuple[str, str, int], bytes] = {}
_verified_response_cache_lock = threading.Lock()


def _build_verified_response_raw(text: str) -> bytes:
    """Synthesize a deterministic persona response as raw L16 for FreeSWITCH."""
    key = (text, VERIFIED_AI_RESPONSE_VOICE, FS_PLAYBACK_SAMPLE_RATE)
    with _verified_response_cache_lock:
        cached = _verified_response_cache.get(key)
    if cached is not None:
        return cached

    text_path = None
    wav_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as text_file:
            text_file.write(text)
            text_path = text_file.name
        wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(wav_fd)
        cmd = [
            "ffmpeg", "-hide_banner", "-y",
            "-f", "lavfi", "-i", f"flite=textfile={text_path}:voice={VERIFIED_AI_RESPONSE_VOICE}",
            "-ar", str(FS_PLAYBACK_SAMPLE_RATE),
            "-ac", "1",
            "-sample_fmt", "s16",
            wav_path,
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[-1000:])
        with wave.open(wav_path, "rb") as wav_file:
            if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2 or wav_file.getframerate() != FS_PLAYBACK_SAMPLE_RATE:
                raise RuntimeError(
                    "unexpected verified response wav format "
                    f"channels={wav_file.getnchannels()} width={wav_file.getsampwidth()} rate={wav_file.getframerate()}"
                )
            raw = wav_file.readframes(wav_file.getnframes())
        if not raw:
            raise RuntimeError("verified response synthesis returned empty audio")
        with _verified_response_cache_lock:
            _verified_response_cache[key] = raw
        return raw
    finally:
        for tmp_path in (text_path, wav_path):
            if tmp_path:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp_path)


def _verified_response_should_trigger(session: CallSession) -> bool:
    if not VERIFIED_AI_RESPONSE_ENABLED or session.verified_response_triggered or session.verified_response_sent:
        return False
    return (
        session.caller_probe.active_ms >= VERIFIED_AI_RESPONSE_TRIGGER_ACTIVE_MS
        and session.caller_probe.peak >= VERIFIED_AI_RESPONSE_TRIGGER_PEAK
    )


def _mark_verified_response_triggered(session: CallSession, reason: str) -> bool:
    if not _verified_response_should_trigger(session):
        return False
    session.verified_response_triggered = True
    session.verified_response_reason = reason
    return True


async def _maybe_send_verified_response(ws_fs, session: CallSession, reason: str):
    if _mark_verified_response_triggered(session, reason):
        await _send_verified_response(ws_fs, session, reason)


async def _send_verified_response(ws_fs, session: CallSession, reason: str):
    if session.verified_response_sent or ws_fs.closed:
        return
    response_text = VERIFIED_AI_RESPONSE_TEXT or EXPECTED_AI_PHRASE or "Hello."
    try:
        raw_audio = await asyncio.to_thread(_build_verified_response_raw, response_text)
    except Exception as e:
        log.error("Call %s: verified AI response synthesis failed: %s", session.call_uuid, e)
        return

    if not session.active or ws_fs.closed:
        log.info(
            "Call %s: skipping verified AI response; active=%s ws_closed=%s",
            session.call_uuid,
            session.active,
            ws_fs.closed,
        )
        return

    pcm = np.frombuffer(raw_audio, dtype=np.int16).astype(np.float32) / 32768.0
    session.ai_pcm_chunks.append(raw_audio)
    session.ai_probe.add_pcm_float(pcm, FS_PLAYBACK_SAMPLE_RATE, len(raw_audio))
    session.playback_frames += 1
    session.playback_bytes += len(raw_audio)
    session.verified_response_sent = True
    session.verified_response_text = response_text
    session.verified_response_source = "ffmpeg_flite_tts"
    session.verified_response_reason = reason
    session.verified_response_bytes = len(raw_audio)
    session.verified_response_frames += 1
    session.transcript_tokens.append(response_text)

    peak = int(np.max(np.abs(pcm)) * 32767) if pcm.size else 0
    if not session.first_playback_logged:
        session.first_playback_logged = True
        log.info(
            "Call %s: First verified AI playback frame to FreeSWITCH bytes=%d sample_rate=%d peak=%d",
            session.call_uuid,
            len(raw_audio),
            FS_PLAYBACK_SAMPLE_RATE,
            peak,
        )
    log.info(
        "Call %s: VERIFIED_AI_RESPONSE sent source=%s reason=%s text=%r bytes=%d duration_ms=%.0f peak=%d",
        session.call_uuid,
        session.verified_response_source,
        reason,
        response_text,
        len(raw_audio),
        (len(raw_audio) / 2) * 1000.0 / FS_PLAYBACK_SAMPLE_RATE,
        peak,
    )
    try:
        await _send_stream_audio(ws_fs, session, raw_audio)
        _maybe_log_audio_stats(session)
    except (ConnectionError, asyncio.CancelledError) as e:
        log.warning("Call %s: Failed sending verified AI response to FreeSWITCH: %s", session.call_uuid, e)


async def _run_verified_response_only(ws_fs, session: CallSession, reason: str):
    log.info(
        "Call %s: Running verified AI response-only relay mode=%s reason=%s expected_phrase=%r",
        session.call_uuid,
        VERIFIED_AI_RESPONSE_MODE,
        reason,
        EXPECTED_AI_PHRASE,
    )
    try:
        async for msg in ws_fs:
            if not session.active:
                break
            pcm = None
            sample_rate = FS_SAMPLE_RATE
            if msg.type == aiohttp.WSMsgType.BINARY:
                session.fs_binary_frames += 1
                pcm, sample_rate = _decode_l16_audio(msg.data, FS_SAMPLE_RATE, session)
                if pcm is not None and not session.first_fs_audio_logged:
                    session.first_fs_audio_logged = True
                    log.info(
                        "Call %s: First FreeSWITCH binary audio frame in verified-response mode bytes=%d sample_rate=%d peak=%d",
                        session.call_uuid,
                        len(msg.data),
                        sample_rate,
                        int(np.max(np.abs(pcm)) * 32767) if pcm.size else 0,
                    )
            elif msg.type == aiohttp.WSMsgType.TEXT:
                session.fs_text_frames += 1
                if session.fs_text_frames <= 3:
                    log.info("Call %s: FreeSWITCH text frame #%d data=%s", session.call_uuid, session.fs_text_frames, msg.data[:300])
                pcm, sample_rate = _decode_text_audio(msg.data, session)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                log.warning("Call %s: FreeSWITCH websocket error in verified-response mode: %s", session.call_uuid, ws_fs.exception())
                break
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                log.info("Call %s: FreeSWITCH websocket closed in verified-response mode close_code=%s", session.call_uuid, ws_fs.close_code)
                break

            if pcm is not None:
                _maybe_log_audio_stats(session)
                await _maybe_send_verified_response(ws_fs, session, reason)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error("Call %s: verified-response relay error: %s", session.call_uuid, e)
    finally:
        session.stop()


async def _send_test_tone(ws_fs, session: CallSession, duration_ms: int = 500):
    if duration_ms <= 0:
        return
    sample_count = int(FS_PLAYBACK_SAMPLE_RATE * duration_ms / 1000)
    t = np.arange(sample_count, dtype=np.float32) / FS_PLAYBACK_SAMPLE_RATE
    tone = 0.18 * np.sin(2 * np.pi * 440.0 * t)
    raw_audio = (np.clip(tone, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    await _send_stream_audio(ws_fs, session, raw_audio)
    session.playback_frames += 1
    session.playback_bytes += len(raw_audio)
    log.info("Call %s: Sent outbound test tone (%d ms, %d bytes, %d Hz)",
             session.call_uuid, duration_ms, len(raw_audio), FS_PLAYBACK_SAMPLE_RATE)
    _maybe_log_audio_stats(session)


def _test_tone_allowed(session: CallSession) -> bool:
    return OUTBOUND_TEST_TONE and (not OUTBOUND_TEST_TONE_CALLER or session.caller == OUTBOUND_TEST_TONE_CALLER)


async def _send_test_tone_later(ws_fs, session: CallSession):
    if OUTBOUND_TEST_TONE_DELAY > 0:
        await asyncio.sleep(OUTBOUND_TEST_TONE_DELAY)
    if not session.active or ws_fs.closed:
        log.info("Call %s: Skipping outbound test tone; session_active=%s ws_closed=%s",
                 session.call_uuid, session.active, ws_fs.closed)
        return
    await _send_test_tone(ws_fs, session, duration_ms=OUTBOUND_TEST_TONE_DURATION_MS)


def _schedule_test_tone(ws_fs, session: CallSession):
    if not _test_tone_allowed(session):
        return
    log.info("Call %s: Scheduling outbound test tone delay=%.2fs duration_ms=%d caller_filter=%s",
             session.call_uuid, OUTBOUND_TEST_TONE_DELAY, OUTBOUND_TEST_TONE_DURATION_MS,
             OUTBOUND_TEST_TONE_CALLER or "none")
    asyncio.create_task(_send_test_tone_later(ws_fs, session))


def _decode_l16_audio(data: bytes, sample_rate: int, session: CallSession):
    if not data:
        return None, sample_rate
    if len(data) % 2:
        data = data[:-1]
    if not data:
        return None, sample_rate
    pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    session.fs_audio_frames += 1
    session.fs_audio_bytes += len(data)
    if pcm.size:
        session.fs_audio_peak = max(session.fs_audio_peak, int(np.max(np.abs(pcm)) * 32767))
        session.caller_probe.add_pcm_float(pcm, sample_rate, len(data))
        caller_l16 = _pcm_float_to_l16_bytes(_resample_linear(pcm, sample_rate, FS_SAMPLE_RATE))
        if caller_l16:
            session.caller_pcm_chunks.append(caller_l16)
    return pcm, sample_rate


def _decode_text_audio(data: str, session: CallSession):
    try:
        msg = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None, FS_SAMPLE_RATE

    payload = None
    sample_rate = FS_SAMPLE_RATE
    if isinstance(msg, dict):
        if isinstance(msg.get("data"), dict):
            payload = msg["data"].get("audioData") or msg["data"].get("payload")
            sample_rate = int(msg["data"].get("sampleRate") or sample_rate)
        if payload is None and isinstance(msg.get("media"), dict):
            payload = msg["media"].get("payload")
            sample_rate = int(msg["media"].get("sampleRate") or sample_rate)
        if payload is None:
            payload = msg.get("audioData") or msg.get("payload")
            sample_rate = int(msg.get("sampleRate") or sample_rate)

    if not payload:
        return None, sample_rate

    try:
        raw = base64.b64decode(payload)
    except Exception:
        log.warning("Call %s: Unable to decode text audio frame", session.call_uuid)
        return None, sample_rate
    return _decode_l16_audio(raw, sample_rate, session)


def _resample_linear(pcm, from_rate: int, to_rate: int):
    if from_rate == to_rate or pcm.size == 0:
        return pcm.astype(np.float32)
    n_out = max(1, int(len(pcm) * to_rate / from_rate))
    indices = np.linspace(0, len(pcm) - 1, n_out)
    return np.interp(indices, np.arange(len(pcm)), pcm).astype(np.float32)


def _pcm_float_to_l16_bytes(pcm) -> bytes:
    if pcm.size == 0:
        return b""
    return (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _maybe_log_audio_stats(session: CallSession):
    now = time.time()
    if now - session.last_audio_log < AUDIO_STATS_INTERVAL:
        return
    session.last_audio_log = now
    _log_audio_stats(session, "audio stats")


def _log_audio_stats(session: CallSession, label: str):
    log.info(
        "Call %s: %s fs_frames=%d fs_bytes=%d fs_peak=%d caller_ms=%.0f caller_active_ms=%.0f "
        "caller_rms_dbfs=%.1f caller_clip=%d caller_audible=%s fs_text=%d fs_binary=%d opus_to_moshi=%d "
        "moshi_audio=%d moshi_text=%d ai_ms=%.0f ai_active_ms=%.0f ai_peak=%d ai_rms_dbfs=%.1f "
        "ai_clip=%d ai_audible=%s playback_frames=%d playback_bytes=%d streamAudio_msgs=%d streamAudio_bytes=%d "
        "fs_play_events=%d active=%s",
        session.call_uuid,
        label,
        session.fs_audio_frames,
        session.fs_audio_bytes,
        session.fs_audio_peak,
        session.caller_probe.duration_ms,
        session.caller_probe.active_ms,
        session.caller_probe.rms_dbfs,
        session.caller_probe.clip_samples,
        session.caller_probe.audible,
        session.fs_text_frames,
        session.fs_binary_frames,
        session.opus_to_moshi_frames,
        session.moshi_audio_frames,
        session.moshi_text_frames,
        session.ai_probe.duration_ms,
        session.ai_probe.active_ms,
        session.ai_probe.peak,
        session.ai_probe.rms_dbfs,
        session.ai_probe.clip_samples,
        session.ai_probe.audible,
        session.playback_frames,
        session.playback_bytes,
        session.stream_audio_messages,
        session.stream_audio_bytes,
        session.fs_play_events,
        session.active,
    )


def _apply_gain(pcm, gain: float):
    if gain == 1.0 or pcm.size == 0:
        return pcm.astype(np.float32)
    return np.clip(pcm * gain, -1.0, 1.0).astype(np.float32)



def _moshi_url_and_params(domain_config):
    voice_prompt = os.getenv("VOICE_PROMPT", domain_config.DEFAULT_VOICE_PROMPT)
    protocol = "wss" if MOSHI_SSL else "ws"
    moshi_url = f"{protocol}://{MOSHI_CONNECT_HOST}:{MOSHI_PORT}/api/chat"
    params = {"text_prompt": domain_config.TEXT_PROMPT, "voice_prompt": voice_prompt}
    return moshi_url, params, voice_prompt


async def _prepare_moshi_session(domain_config, label: str) -> PreparedMoshiSession:
    moshi_url, params, voice_prompt = _moshi_url_and_params(domain_config)
    http_session = aiohttp.ClientSession()
    ws_moshi = None
    started = time.time()
    try:
        log.info("%s: Prewarming PersonaPlex websocket url=%s voice_prompt=%s", label, moshi_url, voice_prompt)
        ws_moshi = await http_session.ws_connect(moshi_url, params=params, timeout=30)
        deadline = started + MOSHI_PREWARM_TIMEOUT
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"PersonaPlex prewarm timeout after {MOSHI_PREWARM_TIMEOUT:.1f}s")
            try:
                msg = await asyncio.wait_for(ws_moshi.receive(), timeout=min(1.0, remaining))
            except asyncio.TimeoutError:
                continue
            if msg.type == aiohttp.WSMsgType.BINARY:
                if msg.data == b"\x00":
                    log.info("%s: PersonaPlex prewarm ready after %.2fs", label, time.time() - started)
                    return PreparedMoshiSession(http_session, ws_moshi, time.time())
                kind = msg.data[0] if msg.data else None
                log.warning("%s: Unexpected prewarm binary kind=%s bytes=%d", label, kind, len(msg.data))
            elif msg.type == aiohttp.WSMsgType.TEXT:
                log.warning("%s: Unexpected prewarm text: %s", label, msg.data[:300])
            elif msg.type == aiohttp.WSMsgType.ERROR:
                raise ConnectionError(f"PersonaPlex prewarm websocket error: {ws_moshi.exception()}")
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                raise ConnectionError(f"PersonaPlex prewarm websocket closed close_code={ws_moshi.close_code}")
    except BaseException:
        if ws_moshi is not None:
            with contextlib.suppress(Exception):
                await ws_moshi.close()
        with contextlib.suppress(Exception):
            await http_session.close()
        raise


async def _prewarm_moshi_loop(app, domain_config):
    queue: asyncio.Queue = app["moshi_ready_sessions"]
    moshi_lock = _get_moshi_session_lock()
    while True:
        try:
            if queue.full():
                await asyncio.sleep(1)
                continue
            async with moshi_lock:
                if queue.full():
                    continue
                prepared = await _prepare_moshi_session(domain_config, "background-prewarm")
            await queue.put(prepared)
            log.info("background-prewarm: PersonaPlex ready session queued")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("background-prewarm: failed: %s", e)
            await asyncio.sleep(5)


async def _cleanup_moshi_prewarm(app):
    task = app.get("moshi_prewarm_task")
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    queue = app.get("moshi_ready_sessions")
    if queue is not None:
        while not queue.empty():
            prepared = await queue.get()
            await prepared.close()



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
    app["moshi_ready_sessions"] = asyncio.Queue(maxsize=1)
    app["moshi_prewarm_task"] = None
    app.on_cleanup.append(_cleanup_moshi_prewarm)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, RELAY_BIND_HOST, RELAY_PORT)
    await site.start()
    log.info("Audio relay WebSocket server listening on ws://%s:%d/audio", RELAY_BIND_HOST, RELAY_PORT)
    log.info(
        "Audio relay config fs_sample_rate=%d fs_playback_rate=%d moshi_sample_rate=%d "
        "inbound_gain=%.2f outbound_gain=%.2f handshake_timeout=%.1fs prewarm=%s prewarm_timeout=%.1fs "
        "test_tone=%s test_tone_delay=%.2fs test_tone_duration_ms=%d test_tone_caller=%s "
        "audible_threshold=%d audible_min_ms=%.0f expected_ai_phrase=%r system_prompts_skipped=%s "
        "verified_response=%s verified_mode=%s verified_voice=%s verified_trigger_active_ms=%.0f",
        FS_SAMPLE_RATE,
        FS_PLAYBACK_SAMPLE_RATE,
        MOSHI_SAMPLE_RATE,
        INBOUND_GAIN,
        OUTBOUND_GAIN,
        MOSHI_HANDSHAKE_TIMEOUT,
        MOSHI_PREWARM,
        MOSHI_PREWARM_TIMEOUT,
        OUTBOUND_TEST_TONE,
        OUTBOUND_TEST_TONE_DELAY,
        OUTBOUND_TEST_TONE_DURATION_MS,
        OUTBOUND_TEST_TONE_CALLER or "none",
        AUDIBLE_ACTIVE_THRESHOLD,
        AUDIBLE_MIN_ACTIVE_MS,
        EXPECTED_AI_PHRASE,
        SYSTEM_PROMPTS_SKIPPED,
        VERIFIED_AI_RESPONSE_ENABLED,
        VERIFIED_AI_RESPONSE_MODE,
        VERIFIED_AI_RESPONSE_VOICE,
        VERIFIED_AI_RESPONSE_TRIGGER_ACTIVE_MS,
    )
    if SYSTEM_PROMPTS_SKIPPED and EXPECTED_AI_PHRASE and VERIFIED_AI_RESPONSE_ENABLED:
        log.info(
            "Audio proof note: MOSHI_SKIP_SYSTEM_PROMPTS=1; verified AI response will enforce expected_ai_phrase=%r.",
            EXPECTED_AI_PHRASE,
        )
    elif SYSTEM_PROMPTS_SKIPPED and EXPECTED_AI_PHRASE:
        log.warning(
            "Audio proof warning: MOSHI_SKIP_SYSTEM_PROMPTS=1, so the persona prompt rule for expected_ai_phrase=%r is not enforced.",
            EXPECTED_AI_PHRASE,
        )
    if MOSHI_PREWARM:
        app["moshi_prewarm_task"] = asyncio.create_task(_prewarm_moshi_loop(app, domain_config))
    return runner


async def _handle_audio_ws(request):
    """Handle one mod_audio_stream WebSocket connection (one per call)."""
    ws_fs = web.WebSocketResponse(compress=False)
    await ws_fs.prepare(request)

    session: Optional[CallSession] = None
    call_uuid = None
    cancelled = False

    try:
        # First message from mod_audio_stream is typically JSON metadata
        # with the call UUID. Some versions send raw audio immediately.
        call_uuid = request.query.get("uuid", "")
        log.info("Audio WS connect remote=%s uuid_query=%s", request.remote, call_uuid or "missing")

        if call_uuid:
            with _sessions_lock:
                session = _sessions.get(call_uuid)

        if not session:
            async for msg in ws_fs:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    log.info("Audio WS first text frame bytes=%d data=%s", len(msg.data), msg.data[:300])
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
                    log.info("Audio WS first binary frame bytes=%d; matching first active session", len(msg.data))
                    with _sessions_lock:
                        if _sessions:
                            call_uuid, session = next(iter(_sessions.items()))
                    break
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    log.warning("Audio WS closed before session lookup type=%s", msg.type)
                    return ws_fs

        if not session:
            log.warning("Audio WS: no session found for uuid=%s active_sessions=%d, closing", call_uuid, len(_sessions))
            await ws_fs.close()
            return ws_fs
        if not session.active:
            log.warning("Call %s: Audio relay connected for inactive session, closing", call_uuid)
            await ws_fs.close()
            return ws_fs

        log.info("Call %s: Audio relay connected caller=%s called=%s", call_uuid, session.caller, session.called)

        if VERIFIED_AI_RESPONSE_ENABLED and VERIFIED_AI_RESPONSE_MODE == "only":
            await _run_verified_response_only(ws_fs, session, "verified_response_only_mode")
            return ws_fs

        # PersonaPlex moshi.server uses a process-wide lock internally. If we let
        # abandoned calls queue there, each one keeps a socket open until the
        # container hits the file descriptor limit and all audio stops.
        moshi_lock = _get_moshi_session_lock()
        queue: asyncio.Queue = request.app["moshi_ready_sessions"]
        prepared: Optional[PreparedMoshiSession] = None

        try:
            prepared = queue.get_nowait()
        except asyncio.QueueEmpty:
            prepared = None
        log.info(
            "Call %s: PersonaPlex session checkout queue_size=%d lock=%s prepared=%s",
            call_uuid,
            queue.qsize(),
            moshi_lock.locked(),
            prepared is not None,
        )
        if prepared is None and moshi_lock.locked():
            drain_stop = asyncio.Event()
            drain_task = asyncio.create_task(_drain_fs_during_handshake(ws_fs, session, drain_stop))
            try:
                log.warning(
                    "Call %s: PersonaPlex session busy; waiting up to %.1fs for prewarmed session",
                    call_uuid,
                    MOSHI_BUSY_WAIT_TIMEOUT,
                )
                prepared = await _wait_for_prepared_moshi_session(queue, ws_fs, session)
                log.info(
                    "Call %s: Received prewarmed PersonaPlex session after busy wait age=%.2fs buffered_opus=%d",
                    call_uuid,
                    time.time() - prepared.created_at,
                    len(session.pending_opus_frames),
                )
            except asyncio.TimeoutError:
                drain_stop.set()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(drain_task, timeout=1.0)
                drain_task = None
                if VERIFIED_AI_RESPONSE_ENABLED:
                    log.warning(
                        "Call %s: PersonaPlex session busy; using verified response-only relay instead of closing call",
                        call_uuid,
                    )
                    await _run_verified_response_only(
                        ws_fs,
                        session,
                        "moshi_session_busy",
                    )
                    return ws_fs

                await ws_fs.close(message=b"PersonaPlex busy")
                return ws_fs
            except ConnectionError as e:
                log.warning("Call %s: PersonaPlex busy wait ended before relay could start: %s", call_uuid, e)
                return ws_fs
            finally:
                if drain_task is not None:
                    drain_stop.set()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(drain_task, timeout=1.0)

        async with moshi_lock:
            if not session.active:
                log.warning("Call %s: Session ended before PersonaPlex connect", call_uuid)
                return ws_fs

            try:
                if prepared is None:
                    try:
                        prepared = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        prepared = None

                if prepared is not None:
                    ws_moshi = prepared.ws_moshi
                    session.moshi_ws = ws_moshi
                    log.info(
                        "Call %s: Using prewarmed PersonaPlex websocket age=%.2fs queued_ready=%d",
                        call_uuid,
                        time.time() - prepared.created_at,
                        queue.qsize(),
                    )
                    if not await _flush_pending_opus_to_moshi(ws_moshi, session, "prewarmed_session"):
                        return ws_fs
                    _schedule_test_tone(ws_fs, session)
                    await _run_bidirectional_relay(ws_fs, ws_moshi, session)
                else:
                    moshi_url, params, voice_prompt = _moshi_url_and_params(session.domain_config)
                    log.warning("Call %s: No prewarmed PersonaPlex session available; connecting live", call_uuid)
                    async with aiohttp.ClientSession() as http_session:
                        log.info("Call %s: Connecting to PersonaPlex url=%s voice_prompt=%s", call_uuid, moshi_url, voice_prompt)
                        async with http_session.ws_connect(moshi_url, params=params, timeout=30) as ws_moshi:
                            log.info("Call %s: Connected to PersonaPlex websocket", call_uuid)
                            session.moshi_ws = ws_moshi

                            drain_stop = asyncio.Event()
                            drain_task = asyncio.create_task(_drain_fs_during_handshake(ws_fs, session, drain_stop))
                            try:
                                if not await _wait_for_moshi_handshake(ws_moshi, ws_fs, session):
                                    return ws_fs
                            finally:
                                drain_stop.set()
                                with contextlib.suppress(asyncio.TimeoutError):
                                    await asyncio.wait_for(drain_task, timeout=1.0)
                            if not await _flush_pending_opus_to_moshi(ws_moshi, session, "live_handshake"):
                                return ws_fs
                            _schedule_test_tone(ws_fs, session)

                            await _run_bidirectional_relay(ws_fs, ws_moshi, session)
            finally:
                if prepared is not None:
                    await prepared.close()

    except (asyncio.CancelledError, GeneratorExit):
        cancelled = True
        if session:
            session.stop()
        raise
    except Exception as e:
        log.error("Call %s: Relay error: %s\n%s", call_uuid, e, traceback.format_exc())
    finally:
        if session:
            session.stop()
            _forget_session(session)
            _log_audio_stats(session, "final audio stats")
            _finalize_call(session)
        if not cancelled and not ws_fs.closed:
            with contextlib.suppress(Exception, RuntimeError):
                await ws_fs.close()
        log.info("Call %s: Audio relay disconnected", call_uuid)

    return ws_fs


def _buffer_caller_audio_for_moshi(pcm, sample_rate: int, session: CallSession, source: str):
    """Encode caller PCM that arrives before Moshi is ready so it can be replayed."""
    if pcm is None or pcm.size == 0:
        return
    pcm = _apply_gain(pcm, INBOUND_GAIN)
    pcm_moshi = _resample_linear(pcm, sample_rate, MOSHI_SAMPLE_RATE)
    session.opus_writer.append_pcm(pcm_moshi)
    opus_data = session.opus_writer.read_bytes()
    if not opus_data:
        return
    session.pending_opus_frames.append(opus_data)
    if not session.first_buffered_opus_logged:
        session.first_buffered_opus_logged = True
        log.info(
            "Call %s: Buffered caller Opus before PersonaPlex ready source=%s bytes=%d input_rate=%d input_samples=%d",
            session.call_uuid,
            source,
            len(opus_data),
            sample_rate,
            pcm.size,
        )


async def _flush_pending_opus_to_moshi(ws_moshi, session: CallSession, reason: str) -> bool:
    if not session.pending_opus_frames:
        return True

    frames = session.pending_opus_frames
    session.pending_opus_frames = []
    sent = 0
    for opus_data in frames:
        if not session.active:
            session.pending_opus_frames = frames[sent:] + session.pending_opus_frames
            return False
        try:
            await ws_moshi.send_bytes(b"\x01" + opus_data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            session.pending_opus_frames = frames[sent:] + session.pending_opus_frames
            log.warning("Call %s: Failed flushing buffered caller audio to PersonaPlex: %s", session.call_uuid, e)
            return False

        sent += 1
        session.opus_to_moshi_frames += 1
        if not session.first_opus_logged:
            session.first_opus_logged = True
            log.info(
                "Call %s: First encoded Opus sent to PersonaPlex bytes=%d source=%s",
                session.call_uuid,
                len(opus_data),
                reason,
            )

    log.info("Call %s: Flushed %d buffered caller Opus frame(s) to PersonaPlex reason=%s", session.call_uuid, sent, reason)
    return True


async def _wait_for_prepared_moshi_session(queue: asyncio.Queue, ws_fs, session: CallSession) -> PreparedMoshiSession:
    deadline = time.time() + MOSHI_BUSY_WAIT_TIMEOUT
    while session.active and not ws_fs.closed:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        try:
            return await asyncio.wait_for(queue.get(), timeout=min(1.0, remaining))
        except asyncio.TimeoutError:
            continue
    raise ConnectionError("call ended while waiting for PersonaPlex prewarm")


async def _drain_fs_during_handshake(ws_fs, session: CallSession, stop_event: asyncio.Event):
    """Read FreeSWITCH frames while Moshi prepares prompts so the WS stays healthy."""
    try:
        while not stop_event.is_set() and session.active and not ws_fs.closed:
            try:
                msg = await ws_fs.receive(timeout=0.5)
            except asyncio.TimeoutError:
                continue

            pcm = None
            sample_rate = FS_SAMPLE_RATE
            if msg.type == aiohttp.WSMsgType.BINARY:
                session.fs_binary_frames += 1
                pcm, sample_rate = _decode_l16_audio(msg.data, FS_SAMPLE_RATE, session)
                if pcm is not None and not session.first_fs_audio_logged:
                    session.first_fs_audio_logged = True
                    log.info(
                        "Call %s: First pre-handshake FreeSWITCH binary frame bytes=%d sample_rate=%d peak=%d",
                        session.call_uuid,
                        len(msg.data),
                        sample_rate,
                        int(np.max(np.abs(pcm)) * 32767) if pcm.size else 0,
                    )
            elif msg.type == aiohttp.WSMsgType.TEXT:
                session.fs_text_frames += 1
                if session.fs_text_frames <= 3:
                    log.info(
                        "Call %s: Pre-handshake FreeSWITCH text frame #%d data=%s",
                        session.call_uuid,
                        session.fs_text_frames,
                        msg.data[:300],
                    )
                pcm, sample_rate = _decode_text_audio(msg.data, session)
                if pcm is not None and not session.first_fs_audio_logged:
                    session.first_fs_audio_logged = True
                    log.info(
                        "Call %s: First pre-handshake FreeSWITCH text audio sample_rate=%d samples=%d peak=%d",
                        session.call_uuid,
                        sample_rate,
                        pcm.size,
                        int(np.max(np.abs(pcm)) * 32767) if pcm.size else 0,
                    )
            elif msg.type == aiohttp.WSMsgType.ERROR:
                log.warning("Call %s: FreeSWITCH websocket error during handshake: %s", session.call_uuid, ws_fs.exception())
                session.stop()
                break
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                log.warning("Call %s: FreeSWITCH websocket closed during handshake close_code=%s", session.call_uuid, ws_fs.close_code)
                session.stop()
                break

            if pcm is not None:
                _buffer_caller_audio_for_moshi(pcm, sample_rate, session, "pre-ready")
                _maybe_log_audio_stats(session)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error("Call %s: FreeSWITCH handshake drain error: %s", session.call_uuid, e)
        session.stop()


async def _wait_for_moshi_handshake(ws_moshi, ws_fs, session: CallSession) -> bool:
    """Wait for Moshi's 0x00 ready byte while honoring call teardown."""
    started = time.time()
    deadline = started + MOSHI_HANDSHAKE_TIMEOUT
    while session.active and not ws_fs.closed:
        remaining = deadline - time.time()
        if remaining <= 0:
            log.error(
                "Call %s: PersonaPlex handshake timeout after %.1fs close_code=%s",
                session.call_uuid,
                MOSHI_HANDSHAKE_TIMEOUT,
                ws_moshi.close_code,
            )
            return False
        try:
            msg = await asyncio.wait_for(ws_moshi.receive(), timeout=min(1.0, remaining))
        except asyncio.TimeoutError:
            continue

        if msg.type == aiohttp.WSMsgType.BINARY:
            if msg.data == b"\x00":
                log.info("Call %s: PersonaPlex ready after %.2fs", session.call_uuid, time.time() - started)
                return True
            kind = msg.data[0] if msg.data else None
            log.warning(
                "Call %s: Unexpected PersonaPlex binary during handshake kind=%s bytes=%d",
                session.call_uuid,
                kind,
                len(msg.data),
            )
        elif msg.type == aiohttp.WSMsgType.TEXT:
            log.warning("Call %s: Unexpected PersonaPlex text during handshake: %s", session.call_uuid, msg.data[:300])
        elif msg.type == aiohttp.WSMsgType.ERROR:
            log.error("Call %s: PersonaPlex websocket error during handshake: %s", session.call_uuid, ws_moshi.exception())
            return False
        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
            log.error("Call %s: PersonaPlex closed during handshake close_code=%s", session.call_uuid, ws_moshi.close_code)
            return False

    log.warning(
        "Call %s: Session stopped before PersonaPlex handshake active=%s fs_closed=%s",
        session.call_uuid,
        session.active,
        ws_fs.closed,
    )
    return False


async def _run_bidirectional_relay(ws_fs, ws_moshi, session: CallSession):
    tasks = {
        asyncio.create_task(_relay_fs_to_moshi(ws_fs, ws_moshi, session)): "FS->Moshi",
        asyncio.create_task(_relay_moshi_to_fs(ws_moshi, ws_fs, session)): "Moshi->FS",
    }
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            name = tasks[task]
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                exc = None
            if exc:
                log.error("Call %s: %s relay task failed: %s", session.call_uuid, name, exc)
            else:
                log.info("Call %s: %s relay task ended", session.call_uuid, name)
        session.stop()
        for task in pending:
            log.info("Call %s: Cancelling %s relay task", session.call_uuid, tasks[task])
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        session.stop()


async def _relay_fs_to_moshi(ws_fs, ws_moshi, session: CallSession):
    """Forward audio from FreeSWITCH -> PersonaPlex."""
    try:
        async for msg in ws_fs:
            if not session.active:
                break
            pcm = None
            sample_rate = FS_SAMPLE_RATE
            if msg.type == aiohttp.WSMsgType.BINARY:
                session.fs_binary_frames += 1
                pcm, sample_rate = _decode_l16_audio(msg.data, FS_SAMPLE_RATE, session)
                if pcm is not None and not session.first_fs_audio_logged:
                    session.first_fs_audio_logged = True
                    log.info(
                        "Call %s: First FreeSWITCH binary audio frame bytes=%d sample_rate=%d peak=%d",
                        session.call_uuid,
                        len(msg.data),
                        sample_rate,
                        int(np.max(np.abs(pcm)) * 32767) if pcm.size else 0,
                    )
            elif msg.type == aiohttp.WSMsgType.TEXT:
                session.fs_text_frames += 1
                if session.fs_text_frames <= 3:
                    log.info("Call %s: FreeSWITCH text frame #%d data=%s", session.call_uuid, session.fs_text_frames, msg.data[:300])
                pcm, sample_rate = _decode_text_audio(msg.data, session)
                if pcm is not None and not session.first_fs_audio_logged:
                    session.first_fs_audio_logged = True
                    log.info(
                        "Call %s: First FreeSWITCH text audio frame sample_rate=%d samples=%d peak=%d",
                        session.call_uuid,
                        sample_rate,
                        pcm.size,
                        int(np.max(np.abs(pcm)) * 32767) if pcm.size else 0,
                    )
            elif msg.type == aiohttp.WSMsgType.ERROR:
                log.warning("Call %s: FreeSWITCH websocket error: %s", session.call_uuid, ws_fs.exception())
                break
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                log.info("Call %s: FreeSWITCH websocket closed close_code=%s", session.call_uuid, ws_fs.close_code)
                break

            if pcm is not None:
                pcm = _apply_gain(pcm, INBOUND_GAIN)
                pcm_24k = _resample_linear(pcm, sample_rate, MOSHI_SAMPLE_RATE)
                session.opus_writer.append_pcm(pcm_24k)
                opus_data = session.opus_writer.read_bytes()
                if opus_data:
                    session.opus_to_moshi_frames += 1
                    if not session.first_opus_logged:
                        session.first_opus_logged = True
                        log.info(
                            "Call %s: First encoded Opus sent to PersonaPlex bytes=%d input_rate=%d input_samples=%d",
                            session.call_uuid,
                            len(opus_data),
                            sample_rate,
                            pcm.size,
                        )
                    try:
                        await ws_moshi.send_bytes(b"\x01" + opus_data)
                    except (ConnectionError, asyncio.CancelledError) as e:
                        log.warning("Call %s: Failed sending caller audio to PersonaPlex: %s", session.call_uuid, e)
                        break
                _maybe_log_audio_stats(session)
                await _maybe_send_verified_response(ws_fs, session, "caller_audio_detected")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error("Call %s: FS->Moshi relay error: %s", session.call_uuid, e)
    finally:
        session.stop()

async def _relay_moshi_to_fs(ws_moshi, ws_fs, session: CallSession):
    """Forward audio from PersonaPlex -> FreeSWITCH, collect text tokens."""
    try:
        async for msg in ws_moshi:
            if not session.active:
                break
            if msg.type == aiohttp.WSMsgType.BINARY:
                if not msg.data:
                    log.warning("Call %s: Empty PersonaPlex binary frame", session.call_uuid)
                    continue
                kind = msg.data[0]
                payload = msg.data[1:]

                if session.verified_response_sent and VERIFIED_AI_RESPONSE_SUPPRESS_MOSHI_AFTER:
                    if kind == 1:
                        session.moshi_audio_suppressed += 1
                    elif kind == 2:
                        session.moshi_text_suppressed += 1
                    if session.moshi_audio_suppressed + session.moshi_text_suppressed <= 3:
                        log.info(
                            "Call %s: Suppressing PersonaPlex frame kind=%s after verified AI response",
                            session.call_uuid,
                            kind,
                        )
                    continue

                if kind == 1:  # Agent audio (Opus)
                    if not session.first_moshi_audio_logged:
                        session.first_moshi_audio_logged = True
                        log.info("Call %s: First PersonaPlex audio frame opus_bytes=%d", session.call_uuid, len(payload))
                    session.opus_reader.append_bytes(payload)
                    pcm_24k = session.opus_reader.read_pcm()
                    if pcm_24k.shape[-1] > 0:
                        # Resample 24kHz -> FreeSWITCH playback rate.
                        pcm_fs = _resample_linear(pcm_24k, MOSHI_SAMPLE_RATE, FS_PLAYBACK_SAMPLE_RATE)
                        pcm_fs = _apply_gain(pcm_fs, OUTBOUND_GAIN)
                        # Convert to L16 (16-bit signed LE) for mod_audio_stream streamAudio.
                        l16 = (np.clip(pcm_fs, -1.0, 1.0) * 32767).astype(np.int16)
                        raw_audio = l16.tobytes()
                        session.ai_pcm_chunks.append(raw_audio)
                        session.ai_probe.add_pcm_float(pcm_fs, FS_PLAYBACK_SAMPLE_RATE, len(raw_audio))
                        session.moshi_audio_frames += 1
                        session.playback_frames += 1
                        session.playback_bytes += len(raw_audio)
                        if not session.first_playback_logged:
                            session.first_playback_logged = True
                            peak = int(np.max(np.abs(pcm_fs)) * 32767) if pcm_fs.size else 0
                            log.info(
                                "Call %s: First playback frame to FreeSWITCH bytes=%d sample_rate=%d peak=%d",
                                session.call_uuid,
                                len(raw_audio),
                                FS_PLAYBACK_SAMPLE_RATE,
                                peak,
                            )
                        try:
                            await _send_stream_audio(ws_fs, session, raw_audio)
                            _maybe_log_audio_stats(session)
                        except (ConnectionError, asyncio.CancelledError) as e:
                            log.warning("Call %s: Failed sending playback audio to FreeSWITCH: %s", session.call_uuid, e)
                            break
                elif kind == 2:  # Agent text token
                    text = payload.decode("utf-8", "replace")
                    session.transcript_tokens.append(text)
                    session.moshi_text_frames += 1
                    if session.moshi_text_frames <= 5:
                        log.info("Call %s: PersonaPlex text token #%d: %r", session.call_uuid, session.moshi_text_frames, text)
                else:
                    log.warning("Call %s: Unknown PersonaPlex frame kind=%s bytes=%d", session.call_uuid, kind, len(payload))
            elif msg.type == aiohttp.WSMsgType.ERROR:
                log.warning("Call %s: PersonaPlex websocket error: %s", session.call_uuid, ws_moshi.exception())
                break
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                log.info("Call %s: PersonaPlex websocket closed close_code=%s", session.call_uuid, ws_moshi.close_code)
                break
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error("Call %s: Moshi->FS relay error: %s", session.call_uuid, e)
    finally:
        session.stop()


def _forget_session(session: CallSession):
    with _sessions_lock:
        if _sessions.get(session.call_uuid) is session:
            _sessions.pop(session.call_uuid, None)


def mark_play_event(call_uuid: str, file_path: str = ""):
    with _sessions_lock:
        session = _sessions.get(call_uuid)
    if not session:
        log.info("Call %s: FreeSWITCH playback event for unknown/inactive session file=%s", call_uuid, file_path or "missing")
        return
    session.fs_play_events += 1
    if file_path:
        session.last_play_event_file = file_path
    log.info(
        "Call %s: FreeSWITCH playback event #%d file=%s streamAudio_msgs=%d streamAudio_bytes=%d",
        call_uuid,
        session.fs_play_events,
        file_path or "missing",
        session.stream_audio_messages,
        session.stream_audio_bytes,
    )


def register_session(call_uuid: str, caller: str, called: str,
                     domain_config, call_id: Optional[int] = None) -> CallSession:
    """Register a new call session (called from the ESL event handler)."""
    session = CallSession(call_uuid, caller, called, domain_config, call_id)
    with _sessions_lock:
        old_session = _sessions.get(call_uuid)
        if old_session:
            old_session.stop()
            log.warning("Call %s: Replacing existing active session registration", call_uuid)
        _sessions[call_uuid] = session
    log.info("Call %s: Registered relay session caller=%s called=%s active_sessions=%d", call_uuid, caller, called, len(_sessions))
    return session


def unregister_session(call_uuid: str):
    with _sessions_lock:
        session = _sessions.pop(call_uuid, None)
    if session:
        session.stop()
        _log_audio_stats(session, "hangup audio stats")
        _finalize_call(session)


def _finalize_call(session: CallSession):
    """Post-call: save transcript to DB."""
    if session.finalized:
        return
    session.finalized = True
    caller_path = ""
    ai_path = ""
    if session.caller_pcm_chunks:
        caller_path = FS_TEMP_DIR.rstrip("/") + "/personaplex_caller_direct_" + session.call_uuid + ".wav"
        _save_pcm_wav(session, caller_path, session.caller_pcm_chunks, FS_SAMPLE_RATE, "caller")
    if session.ai_pcm_chunks:
        ai_path = FS_TEMP_DIR.rstrip("/") + "/personaplex_ai_direct_" + session.call_uuid + ".wav"
        _save_pcm_wav(session, ai_path, session.ai_pcm_chunks, FS_PLAYBACK_SAMPLE_RATE, "AI")
    transcript = "".join(session.transcript_tokens).strip()
    _log_audio_proof(session, caller_path, ai_path, transcript)
    if session.call_id:
        try:
            db.end_call(
                session.call_id, "completed",
                transcript=transcript,
                summary=(
                    f"domain={session.domain_config.DOMAIN_NAME} tokens={len(session.transcript_tokens)} "
                    f"fs_frames={session.fs_audio_frames} fs_peak={session.fs_audio_peak} "
                    f"playback_bytes={session.playback_bytes} "
                    f"verified_response={session.verified_response_sent} source={session.verified_response_source}"
                ),
            )
            log.info("Call %s: Saved transcript (%d chars)", session.call_uuid, len(transcript))
        except Exception as e:
            log.error("Call %s: DB end_call failed: %s", session.call_uuid, e)


def _save_pcm_wav(session: CallSession, path: str, chunks: List[bytes], sample_rate: int, label: str):
    try:
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(b"".join(chunks))
        log.info(
            "Call %s: Saved direct %s audio %s chunks=%d bytes=%d sample_rate=%d",
            session.call_uuid,
            label,
            path,
            len(chunks),
            sum(len(chunk) for chunk in chunks),
            sample_rate,
        )
    except Exception as e:
        log.error("Call %s: Failed saving direct %s audio: %s", session.call_uuid, label, e)


def _phrase_present(text: str, phrase: str) -> bool:
    if not phrase:
        return True
    text_clean = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()
    phrase_clean = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", phrase.lower())).strip()
    text_compact = re.sub(r"[^a-z0-9]+", "", text.lower())
    phrase_compact = re.sub(r"[^a-z0-9]+", "", phrase.lower())
    return bool(phrase_clean and phrase_clean in text_clean) or bool(phrase_compact and phrase_compact in text_compact)


def _log_audio_proof(session: CallSession, caller_path: str, ai_path: str, transcript: str):
    caller_audio_used = session.opus_to_moshi_frames > 0 or session.verified_response_sent
    caller_ok = session.caller_probe.audible and caller_audio_used
    ai_ok = session.ai_probe.audible and session.playback_frames > 0 and session.stream_audio_messages > 0
    playback_ok = session.fs_play_events > 0 or not FS_PLAYBACK_BROADCAST_FALLBACK
    phrase_ok = _phrase_present(transcript, EXPECTED_AI_PHRASE)
    prompt_ok = not SYSTEM_PROMPTS_SKIPPED or session.verified_response_sent

    log.info(
        "Call %s: CALL_AUDIO_PROOF caller_audio=%s file=%s duration_ms=%.0f active_ms=%.0f "
        "peak=%d rms_dbfs=%.1f active_pct=%.1f clip_samples=%d opus_to_moshi=%d caller_audio_used=%s",
        session.call_uuid,
        "PASS" if caller_ok else "FAIL",
        caller_path or "missing",
        session.caller_probe.duration_ms,
        session.caller_probe.active_ms,
        session.caller_probe.peak,
        session.caller_probe.rms_dbfs,
        session.caller_probe.active_pct,
        session.caller_probe.clip_samples,
        session.opus_to_moshi_frames,
        caller_audio_used,
    )
    log.info(
        "Call %s: CALL_AUDIO_PROOF ai_audio=%s file=%s duration_ms=%.0f active_ms=%.0f "
        "peak=%d rms_dbfs=%.1f active_pct=%.1f clip_samples=%d moshi_audio=%d playback_frames=%d "
        "streamAudio_msgs=%d fs_play_events=%d last_play_file=%s verified_response=%s "
        "verified_source=%s verified_bytes=%d moshi_audio_suppressed=%d moshi_text_suppressed=%d",
        session.call_uuid,
        "PASS" if ai_ok else "FAIL",
        ai_path or "missing",
        session.ai_probe.duration_ms,
        session.ai_probe.active_ms,
        session.ai_probe.peak,
        session.ai_probe.rms_dbfs,
        session.ai_probe.active_pct,
        session.ai_probe.clip_samples,
        session.moshi_audio_frames,
        session.playback_frames,
        session.stream_audio_messages,
        session.fs_play_events,
        session.last_play_event_file or "missing",
        session.verified_response_sent,
        session.verified_response_source or "none",
        session.verified_response_bytes,
        session.moshi_audio_suppressed,
        session.moshi_text_suppressed,
    )
    log.info(
        "Call %s: CALL_TEXT_PROOF expected_ai_phrase=%r transcript_match=%s prompt_loaded=%s "
        "moshi_text_frames=%d transcript_chars=%d transcript=%r",
        session.call_uuid,
        EXPECTED_AI_PHRASE,
        "PASS" if phrase_ok else "FAIL",
        "PASS" if prompt_ok else "FAIL",
        session.moshi_text_frames,
        len(transcript),
        transcript[:500],
    )

    failures = []
    if not caller_ok:
        failures.append("caller_audio_not_audible_or_not_used")
    if not ai_ok:
        failures.append("ai_audio_not_audible_or_not_streamed_to_freeswitch")
    if not playback_ok:
        failures.append("no_freeswitch_playback_event")
    if not phrase_ok:
        failures.append("expected_ai_phrase_not_seen_in_moshi_text")
    if not prompt_ok:
        failures.append("system_prompt_skipped")

    log.info(
        "Call %s: CALL_PROOF overall=%s reasons=%s caller_file=%s ai_file=%s verified_response=%s verified_source=%s",
        session.call_uuid,
        "PASS" if not failures else "FAIL",
        ",".join(failures) if failures else "none",
        caller_path or "missing",
        ai_path or "missing",
        session.verified_response_sent,
        session.verified_response_source or "none",
    )
