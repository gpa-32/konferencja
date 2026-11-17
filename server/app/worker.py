"""Worker w tle (w procesie API): synchronizacja kalendarzy, subskrypcje Graph, porządki."""
import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from . import config
from .db import SessionLocal
from .models import Room, Tenant, utcnow
from .services.bookings import housekeeping, sync_room
from .services.calendar import CalendarError, M365Provider, safe_provider

log = logging.getLogger("worker")

_pending_rooms: set[int] = set()
_wake: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None


def request_sync(room_id: int | None = None) -> None:
    """Wywoływane z webhooka lub panelu – synchronizacja poza kolejnością."""
    if room_id is not None:
        _pending_rooms.add(room_id)
    if _wake is not None and _loop is not None:
        _loop.call_soon_threadsafe(_wake.set)


def _ensure_subscription(db, provider: M365Provider, room: Room) -> None:
    if not config.PUBLIC_URL.startswith("https://"):
        return  # Graph wymaga publicznego HTTPS
    now = utcnow()
    try:
        if room.m365_subscription_id and room.m365_subscription_expires \
                and room.m365_subscription_expires > now + timedelta(hours=12):
            return
        if room.m365_subscription_id:
            try:
                room.m365_subscription_expires = provider.renew(room.m365_subscription_id)
                db.commit()
                return
            except CalendarError:
                room.m365_subscription_id = None
        room.m365_subscription_id, room.m365_subscription_expires = provider.subscribe(room)
        db.commit()
    except CalendarError as exc:
        log.warning("Subskrypcja Graph dla %s: %s", room.name, exc)


def sync_once(only_rooms: set[int] | None = None) -> None:
    db = SessionLocal()
    try:
        tenants = db.scalars(select(Tenant).where(Tenant.active.is_(True),
                                                  Tenant.calendar_provider != "local")).all()
        for tenant in tenants:
            provider, err = safe_provider(tenant)
            rooms = db.scalars(select(Room).where(Room.tenant_id == tenant.id, Room.calendar_id.is_not(None))).all()
            for room in rooms:
                if only_rooms is not None and room.id not in only_rooms:
                    continue
                if err:
                    room.last_sync_error = err
                    db.commit()
                    continue
                if isinstance(provider, M365Provider) and only_rooms is None:
                    _ensure_subscription(db, provider, room)
                try:
                    sync_room(db, tenant, room, provider)
                except CalendarError as exc:
                    db.rollback()
                    room = db.get(Room, room.id)
                    room.last_sync_error = str(exc)[:1000]
                    db.commit()
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    log.exception("Synchronizacja sali %s", room.id)
                    room = db.get(Room, room.id)
                    room.last_sync_error = f"Błąd: {exc}"[:1000]
                    db.commit()
        if only_rooms is None:
            housekeeping(db)
    finally:
        db.close()


async def run() -> None:
    global _wake, _loop
    _wake = asyncio.Event()
    _loop = asyncio.get_running_loop()
    while True:
        try:
            await asyncio.wait_for(_wake.wait(), timeout=config.SYNC_INTERVAL)
            _wake.clear()
            rooms = set(_pending_rooms)
            _pending_rooms.clear()
            await asyncio.sleep(1)  # zbierz serię powiadomień
            await asyncio.to_thread(sync_once, rooms or None)
        except asyncio.TimeoutError:
            await asyncio.to_thread(sync_once, None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Worker")
            await asyncio.sleep(5)
