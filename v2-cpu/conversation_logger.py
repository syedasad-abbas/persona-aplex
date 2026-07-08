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
    ):
        text = (text or "").strip()
        if not text or self.closed:
            return

        await self.queue.put({
            "role": role,
            "text": text,
            "intent": intent,
            "is_off_topic": is_off_topic,
            "source_used": source_used,
            "quality_label": quality_label,
            "created_ms": int(time.time() * 1000),
        })

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
                "CALL_TURN call_uuid=%s turn=%d role=%s source=%s text=%r",
                self.call_uuid,
                self.turn_index,
                item["role"],
                item["source_used"],
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
                )
            except Exception:
                log.warning(
                    "CALL_TURN_SAVE_FAILED call_uuid=%s turn=%d role=%s",
                    self.call_uuid,
                    self.turn_index,
                    item["role"],
                    exc_info=True,
                )
