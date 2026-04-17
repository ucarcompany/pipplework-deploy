"""WebSocket connection manager for real-time pipeline events."""
from __future__ import annotations
import json
import asyncio
from datetime import datetime, timezone
from fastapi import WebSocket


class WSManager:
    def __init__(self):
        self._connections: list[WebSocket] = []
        self._event_history: list[dict] = []
        self._max_history = 500

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)
        # Send recent history so new clients catch up
        for evt in self._event_history[-50:]:
            try:
                await ws.send_json(evt)
            except Exception:
                break

    def disconnect(self, ws: WebSocket):
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, event: dict):
        if "timestamp" not in event:
            event["timestamp"] = datetime.now(timezone.utc).isoformat()
        self._event_history.append(event)
        if len(self._event_history) > self._max_history:
            self._event_history = self._event_history[-self._max_history:]
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def emit(self, event_type: str, stage: str, message: str, data: dict | None = None):
        await self.broadcast({
            "event_type": event_type,
            "stage": stage,
            "message": message,
            "data": data or {},
        })


ws_manager = WSManager()
