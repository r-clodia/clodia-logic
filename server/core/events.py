"""Simple in-process pub/sub event bus for SSE streaming."""
import asyncio
from typing import AsyncIterator

from .models import Event


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[Event]] = []

    async def publish(self, event: Event) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> AsyncIterator[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        try:
            while True:
                ev = await q.get()
                yield ev
        finally:
            self._subscribers.remove(q)


bus = EventBus()
