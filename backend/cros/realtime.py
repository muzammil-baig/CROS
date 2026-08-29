"""Realtime fan-out with a replay buffer so clients can recover missed updates."""
import asyncio
import json
from collections import deque
from typing import Any

from fastapi import WebSocket


class RealtimeManager:
    def __init__(self, buffer_size: int = 500):
        self._clients: dict[WebSocket, set[str]] = {}
        self._buffer: deque = deque(maxlen=buffer_size)
        self._seq = 0
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients[ws] = {"*"}

    def disconnect(self, ws: WebSocket):
        self._clients.pop(ws, None)

    def subscribe(self, ws: WebSocket, topics: list[str]):
        self._clients[ws] = set(topics) if topics else {"*"}

    def replay(self, since_seq: int, topics: set[str]) -> list[dict]:
        return [m for m in self._buffer
                if m["seq"] > since_seq and ("*" in topics or m["topic"] in topics)]

    @property
    def latest_seq(self) -> int:
        return self._seq

    async def broadcast(self, topic: str, data: Any):
        async with self._lock:
            self._seq += 1
            msg = {"seq": self._seq, "topic": topic, "data": data}
            self._buffer.append(msg)
        dead = []
        for ws, topics in list(self._clients.items()):
            if "*" in topics or topic in topics:
                try:
                    await ws.send_text(json.dumps(msg, default=str))
                except Exception:
                    dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def client_count(self) -> int:
        return len(self._clients)


manager = RealtimeManager()
