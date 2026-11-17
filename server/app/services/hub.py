"""Połączenia WebSocket z tabletami. Bezpieczne do wywołania z wątków (endpointy sync, worker)."""
import asyncio
import json
import logging
from dataclasses import dataclass

from fastapi import WebSocket

log = logging.getLogger("hub")


@dataclass
class _Conn:
    ws: WebSocket
    room_id: int | None


class Hub:
    def __init__(self) -> None:
        self._conns: dict[int, _Conn] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    def is_online(self, device_id: int) -> bool:
        return device_id in self._conns

    async def register(self, device_id: int, room_id: int | None, ws: WebSocket) -> None:
        old = self._conns.get(device_id)
        self._conns[device_id] = _Conn(ws, room_id)
        if old is not None:
            try:
                await old.ws.close()
            except Exception:  # noqa: BLE001
                pass

    def unregister(self, device_id: int, ws: WebSocket) -> None:
        conn = self._conns.get(device_id)
        if conn is not None and conn.ws is ws:
            del self._conns[device_id]

    def update_room(self, device_id: int, room_id: int | None) -> None:
        if device_id in self._conns:
            self._conns[device_id].room_id = room_id

    async def _send(self, device_id: int, payload: dict) -> bool:
        conn = self._conns.get(device_id)
        if conn is None:
            return False
        try:
            await conn.ws.send_text(json.dumps(payload))
            return True
        except Exception as exc:  # noqa: BLE001
            log.info("WS send to device %s failed: %s", device_id, exc)
            self.unregister(device_id, conn.ws)
            return False

    def _submit(self, coro) -> None:
        if self.loop is None or self.loop.is_closed():
            coro.close()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.loop.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    def notify_room(self, room_id: int) -> None:
        for device_id, conn in list(self._conns.items()):
            if conn.room_id == room_id:
                self._submit(self._send(device_id, {"type": "refresh"}))

    def send_command(self, device_id: int, command: str, **params) -> bool:
        if device_id not in self._conns:
            return False
        self._submit(self._send(device_id, {"type": "command", "command": command, **params}))
        return True


hub = Hub()
