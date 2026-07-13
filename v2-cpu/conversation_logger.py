import asyncio
import time

import db
from logging_config import get_logger

log = get_logger("agent.conversation")


class ConversationLogger:
    def __init__(self, call_id, call_uuid):
        self.call_id = call_id
        self.call_uuid = call_uuid
        self.turn_index = 0
        self.queue = asyncio.Queue()
        self.worker_task = None
        self.closed = False

    def start(self):
        if self.closed:
            return
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self._worker())

    async def add_turn(
        self,
        role,
        text,
        intent=None,
        is_off_topic=False,
        source_used=None,
        quality_label=None,
        start_ms=None,
        end_ms=None,
    ):
        """Queue a conversation turn for logging/persistence.

        start_ms / end_ms are milliseconds relative to the beginning of the
        call (i.e. comparable to session.started_at), NOT wall-clock epoch
        time - that keeps them directly comparable against timestamps in the
        recorded call WAV. Pass None when the caller doesn't have a
        meaningful span for this turn (e.g. a turn source that isn't
        timestamped yet); created_ms (wall-clock) is still always recorded
        as a fallback/audit timestamp.
        """
        text = (text or "").strip()
        if not text or self.closed:
            return

        start_ms = int(round(start_ms)) if start_ms is not None else None
        end_ms = int(round(end_ms)) if end_ms is not None else None

        await self.queue.put({
            "role": role,
            "text": text,
            "intent": intent,
            "is_off_topic": is_off_topic,
            "source_used": source_used,
            "quality_label": quality_label,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "created_ms": int(time.time() * 1000),
        })

    async def add_reasoning(
        self,
        step,
        source=None,
        decision=None,
        reason=None,
        metadata=None,
    ):
        """Log a short, observable decision-trace entry.

        This is NOT raw model chain-of-thought. It records the
        observable inputs/outputs of a decision point (what was seen,
        what was decided, why) so calls can be audited without ever
        exposing private hidden reasoning.
        """
        if self.closed:
            return

        log.info(
            "REASONING call_uuid=%s step=%s source=%s decision=%s reason=%s metadata=%s",
            self.call_uuid,
            step,
            source,
            decision,
            reason,
            metadata,
        )

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await self.queue.put(None)
        if self.worker_task:
            await self.worker_task

    async def _worker(self):
        while True:
            item = await self.queue.get()
            if item is None:
                break

            self.turn_index += 1

            log.info(
                "CALL_TURN call_uuid=%s turn=%d role=%s source=%s start_ms=%s end_ms=%s text=%r",
                self.call_uuid,
                self.turn_index,
                item["role"],
                item["source_used"],
                item["start_ms"],
                item["end_ms"],
                item["text"][:500],
            )

            if not self.call_id:
                log.info(
                    "CALL_TURN_DB_SKIPPED call_uuid=%s turn=%d role=%s reason=no_call_id",
                    self.call_uuid,
                    self.turn_index,
                    item["role"],
                )
                continue

            try:
                await asyncio.to_thread(
                    db.save_conversation_turn,
                    self.call_id,
                    self.turn_index,
                    item["role"],
                    item["text"],
                    item["intent"],
                    item["is_off_topic"],
                    item["source_used"],
                    item["quality_label"],
                    item["start_ms"],
                    item["end_ms"],
                )
            except Exception:
                log.warning(
                    "CALL_TURN_SAVE_FAILED call_uuid=%s turn=%d role=%s",
                    self.call_uuid,
                    self.turn_index,
                    item["role"],
                    exc_info=True,
                )
