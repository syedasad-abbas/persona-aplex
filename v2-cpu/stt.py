import os
import json
import asyncio
import inspect
import contextlib
import websockets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from faster_whisper import WhisperModel
from logging_config import get_logger

log = get_logger("agent.stt")

STT_ENABLED = os.getenv("STT_ENABLED", "0").lower() in ("1", "true", "yes")
STT_PROVIDER = os.getenv("STT_PROVIDER", "deepgram").lower()
STT_MODEL = os.getenv("STT_MODEL", "base")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
STT_DEVICE = os.getenv("STT_DEVICE", "cpu")
STT_COMPUTE_TYPE = os.getenv("STT_COMPUTE_TYPE", "int8")
STT_QUEUE_MAX_CHUNKS = int(os.getenv("STT_QUEUE_MAX_CHUNKS", "500"))

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
DEEPGRAM_MODEL = os.getenv("DEEPGRAM_MODEL", STT_MODEL if STT_MODEL != "base" else "nova-3")
DEEPGRAM_LANGUAGE = os.getenv("DEEPGRAM_LANGUAGE", os.getenv("STT_LANGUAGE", "en-US"))
DEEPGRAM_ENDPOINTING_MS = os.getenv("DEEPGRAM_ENDPOINTING_MS", os.getenv("STT_ENDPOINTING_MS", "300"))
DEEPGRAM_URL = os.getenv("DEEPGRAM_URL", "wss://api.deepgram.com/v1/listen")

_model = None


def get_model():
    global _model
    if _model is None:
        _model = WhisperModel(
            WHISPER_MODEL,
            device=STT_DEVICE,
            compute_type=STT_COMPUTE_TYPE,
        )
    return _model


def transcribe_wav(path: str) -> str:
    if not STT_ENABLED:
        return ""

    if not path:
        return ""

    model = get_model()
    segments, info = model.transcribe(path, vad_filter=True)

    parts = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            parts.append(text)

    transcript = " ".join(parts).strip()
    log.info("STT transcript generated chars=%d language=%s", len(transcript), info.language)
    return transcript


class StreamingSTTSession:
    def __init__(self, call_uuid, sample_rate, on_final_text):
        self.call_uuid = call_uuid
        self.sample_rate = sample_rate
        self.on_final_text = on_final_text
        self.queue = asyncio.Queue(maxsize=STT_QUEUE_MAX_CHUNKS)
        self.task = None
        self.closed = False
        self.dropped_chunks = 0
        self.final_transcripts = 0

    def start(self):
        if STT_PROVIDER != "deepgram":
            log.error(
                "Call %s: Streaming STT provider=%s is not supported; use STT_PROVIDER=deepgram",
                self.call_uuid,
                STT_PROVIDER,
            )
            self.closed = True
            return False
        if not DEEPGRAM_API_KEY:
            log.error(
                "Call %s: Cannot start STT session, DEEPGRAM_API_KEY is not set",
                self.call_uuid,
            )
            self.closed = True
            return False
        if self.task is None or self.task.done():
            self.closed = False
            self.task = asyncio.create_task(self._worker())
            log.info(
                "Call %s: STT session starting provider=deepgram model=%s language=%s sample_rate=%s",
                self.call_uuid,
                DEEPGRAM_MODEL,
                DEEPGRAM_LANGUAGE or "auto",
                self.sample_rate,
            )
        return True

    async def send_audio(self, pcm_bytes):
        if self.closed or not pcm_bytes or self.task is None or self.task.done():
            return False

        try:
            self.queue.put_nowait(pcm_bytes)
            return True
        except asyncio.QueueFull:
            self.dropped_chunks += 1
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self.queue.put_nowait(pcm_bytes)
            except asyncio.QueueFull:
                return False

            if self.dropped_chunks <= 5 or self.dropped_chunks % 100 == 0:
                log.warning(
                    "Call %s: STT audio queue full, dropped_chunks=%d",
                    self.call_uuid,
                    self.dropped_chunks,
                )
            return True

    async def close(self):
        self.closed = True
        if self.task and not self.task.done():
            try:
                self.queue.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                with contextlib.suppress(asyncio.QueueFull):
                    self.queue.put_nowait(None)
        if self.task:
            await self.task

    def _build_deepgram_url(self):
        parts = urlsplit(DEEPGRAM_URL)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.setdefault("model", DEEPGRAM_MODEL)
        query.setdefault("encoding", "linear16")
        query.setdefault("channels", "1")
        query.setdefault("interim_results", "true")
        query.setdefault("endpointing", str(DEEPGRAM_ENDPOINTING_MS))
        query.setdefault("smart_format", "true")
        if DEEPGRAM_LANGUAGE:
            query.setdefault("language", DEEPGRAM_LANGUAGE)
        query["sample_rate"] = str(self.sample_rate)
        return urlunsplit((
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        ))

    async def _worker(self):
        url = self._build_deepgram_url()
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}

        try:
            header_arg = (
                "additional_headers"
                if "additional_headers" in inspect.signature(websockets.connect).parameters
                else "extra_headers"
            )
            connect_ctx = websockets.connect(url, **{header_arg: headers})

            async with connect_ctx as ws:
                log.info("Call %s: STT websocket connected", self.call_uuid)
                sender = asyncio.create_task(self._sender_loop(ws))
                receiver = asyncio.create_task(self._receiver_loop(ws))

                try:
                    await sender
                finally:
                    # sender finished (queue drained with sentinel) -> close stream cleanly
                    try:
                        await ws.send(json.dumps({"type": "CloseStream"}))
                    except Exception:
                        pass
                    await receiver
        except Exception as e:
            log.error(
                "STT worker failed call_uuid=%s error=%s",
                self.call_uuid, str(e),
            )
        finally:
            self.closed = True
            log.info(
                "Call %s: STT session closed finals=%d dropped_chunks=%d",
                self.call_uuid,
                self.final_transcripts,
                self.dropped_chunks,
            )

    async def _sender_loop(self, ws):
        while True:
            chunk = await self.queue.get()
            if chunk is None:
                break
            try:
                await ws.send(chunk)
            except Exception as e:
                log.error(
                    "STT sender failed call_uuid=%s error=%s",
                    self.call_uuid, str(e),
                )
                break

    async def _receiver_loop(self, ws):
        try:
            async for message in ws:
                try:
                    data = json.loads(message)
                except (TypeError, ValueError):
                    continue

                if (
                    data.get("type") == "Results"
                    and data.get("is_final") is True
                    and data.get("speech_final") is True
                ):
                    alternatives = data.get("channel", {}).get("alternatives", [])
                    if not alternatives:
                        continue

                    text = alternatives[0].get("transcript", "").strip()
                    confidence = alternatives[0].get("confidence", 0.0)

                    if text:
                        self.final_transcripts += 1
                        log.info(
                            "STT_FINAL call_uuid=%s confidence=%.3f text=%r",
                            self.call_uuid,
                            confidence,
                            text[:500],
                        )
                        try:
                            await self.on_final_text(text, confidence)
                        except Exception:
                            log.warning(
                                "STT final-text callback failed call_uuid=%s",
                                self.call_uuid,
                                exc_info=True,
                            )
        except websockets.exceptions.ConnectionClosed:
            log.info("STT receiver connection closed call_uuid=%s", self.call_uuid)
        except Exception as e:
            log.error(
                "STT receiver failed call_uuid=%s error=%s",
                self.call_uuid, str(e),
            )
