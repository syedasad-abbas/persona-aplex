import os
import asyncio
import contextlib
import numpy as np
from logging_config import get_logger

log = get_logger("agent.stt")

STT_ENABLED = os.getenv("STT_ENABLED", "0").lower() in ("1", "true", "yes")
STT_PROVIDER = os.getenv("STT_PROVIDER", "faster_whisper").strip().lower()
WHISPER_MODEL = os.getenv("WHISPER_MODEL", os.getenv("STT_MODEL", "base"))
STT_DEVICE = os.getenv("STT_DEVICE", "cpu")
STT_COMPUTE_TYPE = os.getenv("STT_COMPUTE_TYPE", "int8")
STT_QUEUE_MAX_CHUNKS = int(os.getenv("STT_QUEUE_MAX_CHUNKS", "500"))

# Language hint passed to faster-whisper (e.g. "en"). Leave empty/unset to
# let the model auto-detect the spoken language on every utterance.
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "en")

# --- Local streaming/endpointing tuning -----------------------------------
# StreamingSTTSession no longer talks to a remote ASR websocket; instead it
# buffers incoming PCM, runs a lightweight energy-based VAD to find utterance
# boundaries, and transcribes each completed utterance locally with
# faster-whisper. These knobs control that segmentation behavior.
STT_VAD_FRAME_MS = int(os.getenv("STT_VAD_FRAME_MS", "30"))
STT_VAD_ENERGY_THRESHOLD = int(os.getenv("STT_VAD_ENERGY_THRESHOLD", "300"))
STT_ENDPOINTING_MS = int(os.getenv("STT_ENDPOINTING_MS", "600"))
STT_MIN_SPEECH_MS = int(os.getenv("STT_MIN_SPEECH_MS", "200"))
STT_MAX_UTTERANCE_MS = int(os.getenv("STT_MAX_UTTERANCE_MS", "15000"))
STT_PRE_ROLL_MS = int(os.getenv("STT_PRE_ROLL_MS", "150"))

_WHISPER_SAMPLE_RATE = 16000

# OPTIONAL LOCAL STT START
# faster-whisper is only needed when STT is actually enabled. Importing it
# unconditionally would force the dependency to be installed even when STT
# is disabled entirely, so it's imported conditionally based on STT_ENABLED.
if STT_ENABLED:
    from faster_whisper import WhisperModel
else:
    WhisperModel = None
# OPTIONAL LOCAL STT END

_model = None


def get_model():
    global _model
    if WhisperModel is None:
        raise RuntimeError(
            "faster-whisper is not available; set STT_ENABLED=1 and ensure "
            "the faster-whisper package is installed to use local STT."
        )
    if _model is None:
        _model = WhisperModel(
            WHISPER_MODEL,
            device=STT_DEVICE,
            compute_type=STT_COMPUTE_TYPE,
        )
    return _model


def preload_model():
    """Eagerly load the faster-whisper model once, synchronously.

    get_model() already lazy-loads and caches the model globally (once per
    process, not once per call), but the first caller to hit it still pays
    the load/initialize cost. Call this once during application startup
    (before any calls are accepted) so that cost never lands on a live
    caller. Safe to call from a sync startup hook; safe to call again later
    since get_model() short-circuits on the cached instance. No-ops if STT
    is disabled.
    """
    if not STT_ENABLED:
        return None
    return get_model()


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


def _resample_to_16k(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Linearly resample a mono float32 array to the 16kHz faster-whisper expects."""
    if sample_rate == _WHISPER_SAMPLE_RATE or samples.size == 0:
        return samples
    duration = samples.size / float(sample_rate)
    n_out = max(1, int(round(duration * _WHISPER_SAMPLE_RATE)))
    src_idx = np.arange(samples.size)
    dst_idx = np.linspace(0, samples.size - 1, n_out)
    return np.interp(dst_idx, src_idx, samples).astype(np.float32)


class StreamingSTTSession:
    """Buffers PCM audio for one call and transcribes it locally with faster-whisper.

    Public interface is unchanged from the previous Deepgram-backed
    implementation so callers (e.g. bridge.py) do not need to change:
        start()
        await send_audio(pcm_bytes)
        await close()
    send_audio() only ever enqueues PCM; it never blocks on inference.

    Internally, audio is segmented into utterances with a simple energy-based
    VAD (see total_samples_received / utterance_start_sample / speech_active /
    consecutive_silence_samples / utterance_pcm_buffer below). Each completed
    utterance is transcribed via asyncio.to_thread so the CPU-bound model
    call never blocks the event loop or the live audio relay.

    on_final_text is invoked as on_final_text(text, start_ms, end_ms, quality):
        text      - transcribed text for the utterance
        start_ms  - absolute utterance start, ms from session start
        end_ms    - absolute utterance end, ms from session start
        quality   - faster-whisper's avg_logprob (a log-probability, <= 0;
                    not a confidence percentage), or None if unavailable
    """

    def __init__(self, call_uuid, sample_rate, on_final_text, timeline_offset_ms=0):
        self.call_uuid = call_uuid
        self.sample_rate = sample_rate
        self.on_final_text = on_final_text
        # Align sample-derived caller timestamps with CallSession.started_at,
        # which is also the clock used for PersonaPlex agent turns.
        self.timeline_offset_ms = float(timeline_offset_ms)
        self.queue = asyncio.Queue(maxsize=STT_QUEUE_MAX_CHUNKS)
        self.task = None
        self.closed = False
        self.dropped_chunks = 0
        self.dropped_samples = 0
        self.final_transcripts = 0
        self.total_samples_enqueued = 0

        # Bytes per VAD analysis frame (16-bit mono PCM = 2 bytes/sample).
        self._frame_bytes = max(2, int(self.sample_rate * (STT_VAD_FRAME_MS / 1000.0)) * 2)

        # Segmentation thresholds, converted from ms to sample counts up front
        # so the hot loop below only ever compares sample counters.
        self._silence_end_samples = int(self.sample_rate * STT_ENDPOINTING_MS / 1000)
        self._min_speech_samples = int(self.sample_rate * STT_MIN_SPEECH_MS / 1000)
        self._max_utterance_samples = int(self.sample_rate * STT_MAX_UTTERANCE_MS / 1000)
        self._pre_roll_samples = int(self.sample_rate * STT_PRE_ROLL_MS / 1000)
        self._pre_roll_bytes_cap = self._pre_roll_samples * 2

        # Per-call segmentation state (sample-accurate; see _worker/_timestamp_*).
        self.total_samples_received = 0
        self.utterance_start_sample = None
        self.speech_active = False
        self.consecutive_silence_samples = 0
        self.utterance_pcm_buffer = bytearray()

    def start(self):
        if not STT_ENABLED:
            log.error(
                "Call %s: Cannot start STT session, STT_ENABLED is false",
                self.call_uuid,
            )
            self.closed = True
            return False
        if STT_PROVIDER not in ("faster_whisper", "faster-whisper"):
            log.error(
                "Call %s: Unsupported local STT provider=%s; expected faster_whisper",
                self.call_uuid,
                STT_PROVIDER,
            )
            self.closed = True
            return False
        if WhisperModel is None:
            log.error(
                "Call %s: Cannot start STT session, faster-whisper is not installed",
                self.call_uuid,
            )
            self.closed = True
            return False
        if self.task is None or self.task.done():
            self.closed = False
            self.task = asyncio.create_task(self._worker())
            log.info(
                "Call %s: STT session starting provider=faster-whisper model=%s language=%s sample_rate=%s",
                self.call_uuid,
                WHISPER_MODEL,
                STT_LANGUAGE or "auto",
                self.sample_rate,
            )
        return True

    async def send_audio(self, pcm_bytes):
        """Enqueue PCM for the worker to process. Never runs transcription -
        must stay lightweight since this is called from the live audio relay
        loop; blocking here would stall the caller<->PersonaPlex bridge."""
        if self.closed or not pcm_bytes or self.task is None or self.task.done():
            return False

        sample_count = len(pcm_bytes) // 2
        chunk_start_sample = self.total_samples_enqueued
        self.total_samples_enqueued += sample_count
        item = (pcm_bytes, chunk_start_sample)

        try:
            self.queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            self.dropped_chunks += 1
            dropped_item = None
            try:
                dropped_item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            if dropped_item is not None:
                dropped_audio, _dropped_start = dropped_item
                self.dropped_samples += len(dropped_audio) // 2
            try:
                self.queue.put_nowait(item)
            except asyncio.QueueFull:
                return False

            if self.dropped_chunks <= 5 or self.dropped_chunks % 100 == 0:
                log.warning(
                    "Call %s: STT audio queue full, dropped_chunks=%d dropped_samples=%d",
                    self.call_uuid,
                    self.dropped_chunks,
                    self.dropped_samples,
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

    def _is_speech(self, frame: bytes) -> bool:
        samples = np.frombuffer(frame, dtype=np.int16)
        if samples.size == 0:
            return False
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        return rms >= STT_VAD_ENERGY_THRESHOLD

    def _timestamp_seconds(self, sample_position: int) -> float:
        return sample_position / float(self.sample_rate)

    def _timestamp_ms(self, sample_position: int) -> float:
        return self.timeline_offset_ms + sample_position * 1000.0 / float(self.sample_rate)

    def _reset_utterance_state(self):
        self.speech_active = False
        self.utterance_pcm_buffer = bytearray()
        self.utterance_start_sample = None
        self.consecutive_silence_samples = 0

    async def _worker(self):
        try:
            # Load (or reuse) the model off the event loop before consuming audio.
            await asyncio.to_thread(get_model)
        except Exception as e:
            log.error("Call %s: Failed to load Whisper model: %s", self.call_uuid, e)
            self.closed = True
            return

        # Rolling pre-speech buffer (raw bytes, capped at STT_PRE_ROLL_MS worth
        # of samples) so an utterance doesn't clip the first syllable.
        pre_roll_buffer = bytearray()
        incoming = bytearray()  # leftover partial-frame bytes between chunks

        try:
            while True:
                item = await self.queue.get()
                if item is None:
                    # Caller hung up / stream closed mid-utterance: flush what we have.
                    if self.speech_active:
                        speech_samples = (
                            len(self.utterance_pcm_buffer) // 2 - self.consecutive_silence_samples
                        )
                        if speech_samples >= self._min_speech_samples:
                            await self._finalize_utterance(
                                bytes(self.utterance_pcm_buffer), self.utterance_start_sample
                            )
                        self._reset_utterance_state()
                    break

                chunk, chunk_start_sample = item
                expected_start_sample = self.total_samples_received + len(incoming) // 2
                if chunk_start_sample > expected_start_sample:
                    gap_samples = chunk_start_sample - expected_start_sample
                    log.warning(
                        "Call %s: STT timeline gap samples=%d; resetting VAD state",
                        self.call_uuid,
                        gap_samples,
                    )
                    incoming.clear()
                    pre_roll_buffer.clear()
                    self._reset_utterance_state()
                    self.total_samples_received = chunk_start_sample
                incoming.extend(chunk)

                while len(incoming) >= self._frame_bytes:
                    frame = bytes(incoming[: self._frame_bytes])
                    del incoming[: self._frame_bytes]
                    frame_samples = len(frame) // 2
                    frame_start_sample = self.total_samples_received

                    if self._is_speech(frame):
                        if not self.speech_active:
                            # Speech just started: prepend the buffered pre-roll so
                            # we don't lose the very start of the word.
                            self.speech_active = True
                            pre_roll_samples = len(pre_roll_buffer) // 2
                            self.utterance_start_sample = frame_start_sample - pre_roll_samples
                            self.utterance_pcm_buffer = bytearray(pre_roll_buffer)
                            self.consecutive_silence_samples = 0
                        self.utterance_pcm_buffer.extend(frame)
                        self.consecutive_silence_samples = 0
                    else:
                        pre_roll_buffer.extend(frame)
                        if len(pre_roll_buffer) > self._pre_roll_bytes_cap:
                            del pre_roll_buffer[: len(pre_roll_buffer) - self._pre_roll_bytes_cap]

                        if self.speech_active:
                            self.utterance_pcm_buffer.extend(frame)
                            self.consecutive_silence_samples += frame_samples

                            # 600ms (STT_ENDPOINTING_MS) of trailing silence -> close the utterance.
                            if self.consecutive_silence_samples >= self._silence_end_samples:
                                speech_samples = (
                                    len(self.utterance_pcm_buffer) // 2
                                    - self.consecutive_silence_samples
                                )
                                if speech_samples >= self._min_speech_samples:
                                    await self._finalize_utterance(
                                        bytes(self.utterance_pcm_buffer),
                                        self.utterance_start_sample,
                                    )
                                self._reset_utterance_state()

                    self.total_samples_received += frame_samples

                    # Safety cap so a caller who never pauses still gets transcribed
                    # in bounded chunks instead of one unbounded buffer.
                    if (
                        self.speech_active
                        and len(self.utterance_pcm_buffer) // 2 >= self._max_utterance_samples
                    ):
                        await self._finalize_utterance(
                            bytes(self.utterance_pcm_buffer), self.utterance_start_sample
                        )
                        self._reset_utterance_state()
        except Exception as e:
            log.error(
                "STT worker failed call_uuid=%s error=%s",
                self.call_uuid, str(e),
            )
        finally:
            self.closed = True
            log.info(
                "Call %s: STT session closed finals=%d dropped_chunks=%d dropped_samples=%d total_samples=%d",
                self.call_uuid,
                self.final_transcripts,
                self.dropped_chunks,
                self.dropped_samples,
                self.total_samples_received,
            )

    async def _finalize_utterance(self, audio_bytes: bytes, start_sample):
        # Never transcribe inline in the audio loop: this is only ever called
        # from the STT worker (after an utterance boundary is detected), and
        # the actual model call below is offloaded to a worker thread via
        # asyncio.to_thread so it can't block the event loop or the live
        # caller<->PersonaPlex audio relay.
        if not audio_bytes or start_sample is None:
            return

        utterance_start_ms = self._timestamp_ms(start_sample)
        buffer_end_ms = self._timestamp_ms(self.total_samples_received)

        try:
            text, start_ms, end_ms, quality = await asyncio.to_thread(
                self._transcribe_pcm, audio_bytes, utterance_start_ms
            )
        except Exception:
            log.warning(
                "Call %s: STT transcription failed utterance=[%.0fms-%.0fms]",
                self.call_uuid,
                utterance_start_ms,
                buffer_end_ms,
                exc_info=True,
            )
            return

        if not text:
            return

        self.final_transcripts += 1
        log.info(
            "STT_FINAL call_uuid=%s start_ms=%.0f end_ms=%.0f avg_logprob=%s text=%r",
            self.call_uuid,
            start_ms,
            end_ms,
            f"{quality:.3f}" if quality is not None else "n/a",
            text[:500],
        )
        try:
            await self.on_final_text(text, start_ms, end_ms, quality)
        except Exception:
            log.warning(
                "STT final-text callback failed call_uuid=%s",
                self.call_uuid,
                exc_info=True,
            )

    def _transcribe_pcm(self, pcm_bytes, utterance_start_ms):
        """Runs synchronously in a worker thread via asyncio.to_thread - never
        call this directly from the event loop (e.g. from send_audio)."""
        model = get_model()

        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        samples = _resample_to_16k(samples, self.sample_rate)

        kwargs = {
            # beam_size=1 (greedy decoding) cuts CPU usage and latency, which
            # matters for a real-time call; accuracy tradeoff is acceptable
            # for tiny/base models on CPU.
            "beam_size": 1,
            # vad_filter mops up any residual silence our own energy-VAD left
            # inside the buffered utterance (e.g. quiet room tone).
            "vad_filter": True,
            # Without this, faster-whisper can carry words over from one
            # utterance's context into the next, causing repeated/bled-over
            # text between separate caller turns.
            "condition_on_previous_text": False,
        }
        if STT_LANGUAGE:
            kwargs["language"] = STT_LANGUAGE

        segments, info = model.transcribe(samples, **kwargs)

        parts = []
        logprobs = []
        first_start = None
        last_end = None
        for segment in segments:
            text = segment.text.strip()
            if text:
                parts.append(text)
                if segment.avg_logprob is not None:
                    logprobs.append(segment.avg_logprob)
                if first_start is None:
                    first_start = segment.start
                last_end = segment.end

        transcript = " ".join(parts).strip()

        if first_start is None or last_end is None:
            # No speech segments survived vad_filter; fall back to the full
            # buffered-utterance extents.
            start_ms = utterance_start_ms
            end_ms = utterance_start_ms + (len(pcm_bytes) / 2) * 1000.0 / self.sample_rate
        else:
            # faster-whisper timestamps are seconds relative to the buffer we
            # handed it; add the utterance's absolute offset in the call to
            # get an absolute position in the conversation.
            start_ms = utterance_start_ms + first_start * 1000.0
            end_ms = utterance_start_ms + last_end * 1000.0

        # avg_logprob is a log-probability (<= 0), not a confidence
        # percentage - surface it as-is for debugging/quality triage rather
        # than dressing it up as a calibrated confidence score.
        quality = sum(logprobs) / len(logprobs) if logprobs else None

        return transcript, start_ms, end_ms, quality
