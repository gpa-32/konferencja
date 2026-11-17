"""API dla tabletów: rejestracja kodem QR, stan sali, akcje, WebSocket, zrzuty ekranu, aktualizacje."""
import json
import os
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import (APIRouter, Depends, Header, HTTPException, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import config
from ..db import SessionLocal, get_db
from ..models import (Booking, Device, EnrollmentToken, NfcCard, Room, Tenant,
                      User, utcnow)
from ..security import new_token, token_hash, verify_secret
from ..services import bookings as svc
from ..services.hub import hub

router = APIRouter(prefix="/api/device", tags=["device"])


# ------------------------------------------------------------------ auth

def _device_from_token(db: Session, token: str | None) -> Device:
    if not token:
        raise HTTPException(401, "Brak tokenu urządzenia")
    dev = db.scalars(select(Device).where(Device.token_hash == token_hash(token))).first()
    if dev is None:
        raise HTTPException(401, "Nieznane urządzenie – zarejestruj tablet ponownie")
    tenant = db.get(Tenant, dev.tenant_id)
    if not tenant or not tenant.active:
        raise HTTPException(403, "Konto klienta jest nieaktywne")
    return dev


def current_device(authorization: str | None = Header(None), db: Session = Depends(get_db)) -> Device:
    token = authorization.split(" ", 1)[1] if authorization and " " in authorization else authorization
    dev = _device_from_token(db, token)
    dev.last_seen = utcnow()
    db.commit()
    return dev


# ------------------------------------------------------------------ rejestracja

class EnrollIn(BaseModel):
    code: str
    model: str = ""
    android_version: str = ""
    app_version: str = ""
    has_led: bool = False
    has_nfc: bool = False
    is_root: bool = False
    is_device_owner: bool = False


@router.post("/enroll")
def enroll(body: EnrollIn, request: Request, db: Session = Depends(get_db)):
    code = body.code.strip().upper()
    et = db.scalars(select(EnrollmentToken).where(EnrollmentToken.code == code)).first()
    now = utcnow()
    if et is None or et.used_at is not None or et.expires_at < now:
        raise HTTPException(400, "Kod jest nieprawidłowy, wykorzystany lub wygasł. Wygeneruj nowy w panelu.")
    tenant = db.get(Tenant, et.tenant_id)
    count = db.scalar(select(func.count()).select_from(Device).where(Device.tenant_id == tenant.id))
    if count >= tenant.max_devices:
        raise HTTPException(403, f"Limit tabletów w licencji ({tenant.max_devices}) został wyczerpany.")
    token = new_token()
    room = db.get(Room, et.room_id) if et.room_id else None
    dev = Device(tenant_id=tenant.id, room_id=et.room_id, token_hash=token_hash(token),
                 name=(room.name if room else body.model) or "Tablet", model=body.model,
                 android_version=body.android_version, app_version=body.app_version,
                 has_led=body.has_led, has_nfc=body.has_nfc, is_root=body.is_root,
                 is_device_owner=body.is_device_owner, last_seen=now,
                 ip=request.client.host if request.client else None)
    db.add(dev)
    db.flush()
    et.used_at = now
    et.device_id = dev.id
    svc.audit(db, tenant.id, f"device:{dev.id}", "device.enroll", f"{body.model} → {room.name if room else '-'}")
    db.commit()
    return {"device_id": dev.id, "token": token, "tenant": tenant.name, "room": room.name if room else None}


# ------------------------------------------------------------------ stan sali

def _ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=ZoneInfo("UTC")).timestamp() * 1000)


def _booking_json(b: Booking, tenant: Tenant, room: Room, now: datetime) -> dict:
    tz = tenant.timezone
    needs_checkin = bool(room.checkin_required and b.checked_in_at is None and b.start <= now + timedelta(minutes=5)
                         and b.status == "confirmed")
    return {
        "id": b.id,
        "title": "Spotkanie prywatne" if b.is_private else (b.title or "Rezerwacja"),
        "organizer": b.organizer_name or b.organizer_email,
        "start": _ms(b.start), "end": _ms(b.end),
        "start_local": f"{svc.to_local(b.start, tz):%H:%M}",
        "end_local": f"{svc.to_local(b.end, tz):%H:%M}",
        "people": svc.invited_count(b),
        "requested": svc.counted_attendees(b, tenant),
        "capacity": room.capacity,
        "status": b.status,
        "checked_in": b.checked_in_at is not None,
        "needs_checkin": needs_checkin,
        "checkin_deadline": _ms(b.start + timedelta(minutes=room.checkin_minutes)) if needs_checkin else None,
    }


def room_state(db: Session, dev: Device) -> dict:
    tenant = db.get(Tenant, dev.tenant_id)
    now = utcnow()
    base = {
        "server_time": _ms(now),
        "timezone": tenant.timezone,
        "tenant": tenant.name,
        "device": {
            "id": dev.id, "name": dev.name, "brightness": dev.brightness,
            "screen_off_from": dev.screen_off_from, "screen_off_to": dev.screen_off_to,
            "led_config": json.loads(dev.led_config) if dev.led_config else None,
            "require_auth": tenant.require_auth_on_tablet,
        },
        "room": None, "status": "unassigned", "current": None, "next": [],
        "free_until": None, "free_until_local": None, "book_options": [],
    }
    room = db.get(Room, dev.room_id) if dev.room_id else None
    if room is None:
        return base
    local_now = svc.to_local(now, tenant.timezone)
    day_end_local = local_now.replace(hour=23, minute=59, second=59, microsecond=0)
    day_end = day_end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    items = db.scalars(select(Booking).where(
        Booking.room_id == room.id, Booking.status.in_(svc.ACTIVE),
        Booking.end > now, Booking.start < day_end).order_by(Booking.start)).all()
    current = next((b for b in items if b.start <= now < b.end), None)
    upcoming = [b for b in items if b.start > now]
    nxt = svc.next_booking(db, room.id, now)
    free_until = None if current else (nxt.start if nxt else None)
    options = []
    if current is None:
        for m in (15, 30, 60, 90):
            if free_until is None or now + timedelta(minutes=m) <= free_until:
                options.append(m)
    base.update({
        "room": {"id": room.id, "name": room.name, "capacity": room.capacity,
                 "checkin_required": room.checkin_required, "checkin_minutes": room.checkin_minutes},
        "status": ("pending" if current.status == "pending_approval" else "busy") if current else "free",
        "current": _booking_json(current, tenant, room, now) if current else None,
        "next": [_booking_json(b, tenant, room, now) for b in upcoming],
        "free_until": _ms(free_until) if free_until else None,
        "free_until_local": f"{svc.to_local(free_until, tenant.timezone):%H:%M}" if free_until else None,
        "book_options": options,
        "extend_options": _extend_options(db, room, current) if current else [],
    })
    return base


def _extend_options(db: Session, room: Room, current: Booking) -> list[int]:
    nxt = svc.next_booking(db, room.id, current.end, exclude_id=current.id)
    return [m for m in (15, 30, 60) if nxt is None or current.end + timedelta(minutes=m) <= nxt.start]


@router.get("/state")
def state(dev: Device = Depends(current_device), db: Session = Depends(get_db)):
    return room_state(db, dev)


class HeartbeatIn(BaseModel):
    battery: int | None = None
    ip: str | None = None
    app_version: str | None = None
    rustdesk_id: str | None = None


@router.post("/heartbeat")
def heartbeat(body: HeartbeatIn, request: Request, dev: Device = Depends(current_device),
              db: Session = Depends(get_db)):
    dev.battery = body.battery
    dev.ip = body.ip or (request.client.host if request.client else None)
    if body.app_version:
        dev.app_version = body.app_version
    if body.rustdesk_id:
        dev.rustdesk_id = body.rustdesk_id
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ akcje

class ActionIn(BaseModel):
    action: Literal["book_now", "end", "extend", "checkin"]
    booking_id: int | None = None
    minutes: int = 30
    pin: str | None = None
    nfc_uid: str | None = None


def _authorize(db: Session, tenant: Tenant, body: ActionIn) -> User | None:
    user = None
    if body.nfc_uid:
        card = db.scalars(select(NfcCard).where(NfcCard.tenant_id == tenant.id,
                                                NfcCard.uid == body.nfc_uid.upper())).first()
        if card is None:
            svc.audit(db, tenant.id, "tablet", "nfc.unknown", body.nfc_uid.upper())
            db.commit()
            raise HTTPException(403, f"Nieznana karta ({body.nfc_uid.upper()}). Dodaj ją w panelu.")
        user = card.user
    elif body.pin:
        for u in db.scalars(select(User).where(User.tenant_id == tenant.id, User.active.is_(True),
                                               User.pin_hash.is_not(None))):
            if verify_secret(body.pin, u.pin_hash):
                user = u
                break
        if user is None:
            raise HTTPException(403, "Nieprawidłowy PIN")
    if tenant.require_auth_on_tablet and user is None:
        raise HTTPException(401, "Ta akcja wymaga PIN-u lub karty")
    return user


@router.post("/action")
def action(body: ActionIn, dev: Device = Depends(current_device), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, dev.tenant_id)
    room = db.get(Room, dev.room_id) if dev.room_id else None
    if room is None:
        raise HTTPException(400, "Tablet nie jest przypisany do sali")
    user = _authorize(db, tenant, body)
    actor = f"tablet:{dev.id}" + (f" ({user.email})" if user else "")
    try:
        if body.action == "book_now":
            if not 5 <= body.minutes <= 240:
                raise HTTPException(400, "Nieprawidłowy czas")
            svc.book_now(db, tenant, room, body.minutes, actor,
                         organizer=(user.email, user.name) if user else ("", ""))
        else:
            b = db.get(Booking, body.booking_id or 0)
            if b is None or b.room_id != room.id:
                raise HTTPException(404, "Nie znaleziono rezerwacji")
            if body.action == "end":
                svc.end_early(db, tenant, b, actor)
            elif body.action == "extend":
                svc.extend(db, tenant, b, body.minutes, actor)
            elif body.action == "checkin":
                svc.checkin(db, tenant, b, actor)
    except svc.BookingError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return room_state(db, dev)


# ------------------------------------------------------------------ zrzut ekranu

def screenshot_path(device_id: int) -> str:
    return os.path.join(config.DATA_DIR, "screenshots", f"{device_id}.png")


@router.post("/screenshot")
async def upload_screenshot(request: Request, authorization: str | None = Header(None)):
    data = await request.body()
    if len(data) > 8_000_000 or not data.startswith(b"\x89PNG"):
        raise HTTPException(400, "Oczekiwano obrazu PNG")
    db = SessionLocal()
    try:
        token = authorization.split(" ", 1)[1] if authorization and " " in authorization else authorization
        dev = _device_from_token(db, token)
        os.makedirs(os.path.dirname(screenshot_path(dev.id)), exist_ok=True)
        with open(screenshot_path(dev.id), "wb") as f:
            f.write(data)
        dev.screenshot_at = utcnow()
        db.commit()
    finally:
        db.close()
    return {"ok": True}


# ------------------------------------------------------------------ aktualizacje APK (OTA)

def _apk_meta() -> dict | None:
    p = os.path.join(config.DATA_DIR, "apk", "latest.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


@router.get("/update")
def update_info(dev: Device = Depends(current_device)):
    meta = _apk_meta()
    if not meta:
        return {"available": False}
    return {"available": True, "version_code": meta["version_code"], "version_name": meta["version_name"],
            "url": f"{config.PUBLIC_URL}/api/device/update/apk"}


@router.get("/update/apk")
def update_apk(dev: Device = Depends(current_device)):
    meta = _apk_meta()
    if not meta:
        raise HTTPException(404)
    return FileResponse(os.path.join(config.DATA_DIR, "apk", meta["file"]),
                        media_type="application/vnd.android.package-archive", filename=meta["file"])


# ------------------------------------------------------------------ WebSocket

@router.websocket("/ws")
async def ws(websocket: WebSocket, token: str = ""):
    db = SessionLocal()
    try:
        try:
            dev = _device_from_token(db, token)
        except HTTPException:
            await websocket.close(code=4401)
            return
        dev.last_seen = utcnow()
        db.commit()
        device_id, room_id = dev.id, dev.room_id
    finally:
        db.close()
    await websocket.accept()
    await hub.register(device_id, room_id, websocket)
    try:
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text('{"type":"pong"}')
                db = SessionLocal()
                try:
                    d = db.get(Device, device_id)
                    if d:
                        d.last_seen = utcnow()
                        db.commit()
                finally:
                    db.close()
    except WebSocketDisconnect:
        pass
    finally:
        hub.unregister(device_id, websocket)
