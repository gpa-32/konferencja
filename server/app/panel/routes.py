"""Panel WWW: klienci, sale, tablety, rezerwacje, zatwierdzenia, użytkownicy, ustawienia."""
import base64
import io
import json
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, available_timezones

import qrcode
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import config
from ..api.device import screenshot_path
from ..db import get_db
from ..models import (AuditLog, Booking, Device, EnrollmentToken, NfcCard, Room,
                      Site, Tenant, User, utcnow)
from ..security import hash_secret, new_enrollment_code, verify_secret
from ..services import bookings as svc
from ..services.hub import hub
from ..worker import request_sync
from .auth import Ctx, csrf_token, form, get_ctx, host_tenant_slug, require_admin, require_super

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))
templates.env.globals["csrf_token"] = csrf_token

STATUS_PL = {"confirmed": "potwierdzona", "pending_approval": "czeka na zatwierdzenie", "rejected": "odrzucona",
             "cancelled": "anulowana", "released": "zwolniona (brak check-in)"}
templates.env.globals["STATUS_PL"] = STATUS_PL
ROLES_PL = {"superadmin": "superadmin ePartners", "admin": "administrator", "caretaker": "opiekun sali",
            "user": "użytkownik"}
templates.env.globals["ROLES_PL"] = ROLES_PL


def render(request: Request, name: str, ctx: Ctx | None = None, **kw) -> HTMLResponse:
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, {"ctx": ctx, "flash": flash, **kw})


def back(request: Request, url: str, msg: str | None = None, error: bool = False) -> RedirectResponse:
    if msg:
        request.session["flash"] = {"msg": msg, "error": error}
    return RedirectResponse(url, status_code=303)


def local(dt: datetime | None, tz: str) -> str:
    return "" if dt is None else f"{svc.to_local(dt, tz):%d.%m.%Y %H:%M}"


templates.env.filters["local"] = local


def device_online(d: Device) -> bool:
    return hub.is_online(d.id) or bool(
        d.last_seen and d.last_seen > utcnow() - timedelta(seconds=config.DEVICE_OFFLINE_AFTER))


templates.env.globals["device_online"] = device_online


def _own(db: Session, model, obj_id: int, ctx: Ctx):
    obj = db.get(model, obj_id)
    if obj is None or getattr(obj, "tenant_id", None) != ctx.tid:
        raise HTTPException(404)
    return obj


def _room_of_booking_ok(b: Booking, ctx: Ctx) -> bool:
    return ctx.is_admin or ctx.user.id in (b.room.caretaker_id, b.room.deputy_id)


# ------------------------------------------------------------------ logowanie

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, e: str = ""):
    err = {"inactive": "Konto klienta jest nieaktywne."}.get(e)
    return render(request, "login.html", error=err, slug=host_tenant_slug(request))


@router.post("/login")
def login(request: Request, f: dict = Depends(form), db: Session = Depends(get_db)):
    email = str(f.get("email", "")).strip().lower()
    user = db.scalars(select(User).where(func.lower(User.email) == email)).first()
    if user is None or not user.active or not verify_secret(str(f.get("password", "")), user.password_hash):
        return render(request, "login.html", error="Nieprawidłowy e-mail lub hasło.")
    slug = host_tenant_slug(request)
    if slug and user.role != "superadmin":
        t = db.get(Tenant, user.tenant_id)
        if t is None or t.slug != slug:
            return render(request, "login.html", error="To konto nie należy do tego klienta.")
    request.session.clear()
    request.session["uid"] = user.id
    if user.role == "superadmin" and slug:
        t = db.scalars(select(Tenant).where(Tenant.slug == slug)).first()
        if t:
            request.session["tenant_id"] = t.id
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------ pulpit

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    if ctx.tenant is None:
        return RedirectResponse("/tenants", status_code=303)
    now = utcnow()
    rooms = db.scalars(select(Room).where(Room.tenant_id == ctx.tid).order_by(Room.name)).all()
    rows = []
    for r in rooms:
        cur = svc.current_booking(db, r.id, now)
        nxt = svc.next_booking(db, r.id, now)
        devs = db.scalars(select(Device).where(Device.room_id == r.id)).all()
        rows.append({"room": r, "current": cur, "next": nxt, "devices": devs,
                     "people": svc.invited_count(cur) if cur else 0})
    pending = db.scalar(select(func.count()).select_from(Booking).where(
        Booking.tenant_id == ctx.tid, Booking.status == "pending_approval"))
    return render(request, "dashboard.html", ctx, rows=rows, pending=pending, tz=ctx.tenant.timezone)


# ------------------------------------------------------------------ klienci (superadmin)

@router.get("/tenants", response_class=HTMLResponse)
def tenants(request: Request, ctx: Ctx = Depends(require_super), db: Session = Depends(get_db)):
    items = db.scalars(select(Tenant).order_by(Tenant.name)).all()
    stats = {t.id: (
        db.scalar(select(func.count()).select_from(Room).where(Room.tenant_id == t.id)),
        db.scalar(select(func.count()).select_from(Device).where(Device.tenant_id == t.id)),
    ) for t in items}
    apk = None
    meta_path = os.path.join(config.DATA_DIR, "apk", "latest.json")
    if os.path.exists(meta_path):
        with open(meta_path) as fh:
            apk = json.load(fh)
    return render(request, "tenants.html", ctx, items=items, stats=stats, base_domain=config.BASE_DOMAIN, apk=apk)


@router.post("/tenants")
def tenant_create(request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_super),
                  db: Session = Depends(get_db)):
    slug = str(f.get("slug", "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", slug) or slug in ("admin", "www", "api"):
        return back(request, "/tenants", "Identyfikator: małe litery, cyfry i myślniki (2–41 znaków).", True)
    password = str(f.get("admin_password", ""))
    if len(password) < 10:
        return back(request, "/tenants", "Hasło administratora klienta: min. 10 znaków.", True)
    t = Tenant(slug=slug, name=str(f.get("name", slug)).strip() or slug,
               max_rooms=int(f.get("max_rooms") or 10), max_devices=int(f.get("max_devices") or 10),
               calendar_provider=str(f.get("calendar_provider", "local")))
    db.add(t)
    try:
        db.flush()
        db.add(User(tenant_id=t.id, email=str(f.get("admin_email", "")).strip().lower(),
                    name=str(f.get("admin_name", "")), password_hash=hash_secret(password), role="admin"))
        svc.audit(db, t.id, ctx.user.email, "tenant.create", t.slug)
        db.commit()
    except IntegrityError:
        db.rollback()
        return back(request, "/tenants", "Identyfikator klienta lub e-mail administratora już istnieje.", True)
    return back(request, "/tenants", f"Utworzono klienta {t.name}.")


@router.post("/tenants/{tid}")
def tenant_update(tid: int, request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_super),
                  db: Session = Depends(get_db)):
    t = db.get(Tenant, tid)
    if t is None:
        raise HTTPException(404)
    t.name = str(f.get("name", t.name))
    t.max_rooms = int(f.get("max_rooms") or t.max_rooms)
    t.max_devices = int(f.get("max_devices") or t.max_devices)
    t.active = f.get("active") == "1"
    svc.audit(db, t.id, ctx.user.email, "tenant.update", f"{t.slug} active={t.active}")
    db.commit()
    return back(request, "/tenants", "Zapisano.")


@router.post("/apk")
async def apk_upload(request: Request, ctx: Ctx = Depends(require_super)):
    """Wgranie nowej wersji aplikacji tabletu (aktualizacja OTA dla wszystkich klientów)."""
    data = await request.form()
    import hmac
    if not hmac.compare_digest(str(data.get("_csrf", "")), request.session.get("csrf", "")):
        raise HTTPException(400, "Sesja formularza wygasła – odśwież stronę.")
    upload = data.get("apk")
    try:
        code = int(str(data.get("version_code", "")))
    except ValueError:
        return back(request, "/tenants", "Podaj numer wersji (versionCode).", True)
    if upload is None or not hasattr(upload, "read"):
        return back(request, "/tenants", "Wybierz plik APK.", True)
    content = await upload.read()
    if not content.startswith(b"PK"):
        return back(request, "/tenants", "To nie jest plik APK.", True)
    folder = os.path.join(config.DATA_DIR, "apk")
    os.makedirs(folder, exist_ok=True)
    name = f"sala-{code}.apk"
    with open(os.path.join(folder, name), "wb") as fh:
        fh.write(content)
    meta = {"version_code": code, "version_name": str(data.get("version_name") or f"1.0.{code}"), "file": name}
    with open(os.path.join(folder, "latest.json"), "w") as fh:
        json.dump(meta, fh)
    return back(request, "/tenants", f"Wgrano aplikację {meta['version_name']} – tablety zaktualizują się automatycznie.")


@router.post("/tenants/{tid}/enter")
def tenant_enter(tid: int, request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_super),
                 db: Session = Depends(get_db)):
    if db.get(Tenant, tid) is None:
        raise HTTPException(404)
    request.session["tenant_id"] = tid
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------------ sale

@router.get("/rooms", response_class=HTMLResponse)
def rooms(request: Request, ctx: Ctx = Depends(require_admin), db: Session = Depends(get_db)):
    items = db.scalars(select(Room).where(Room.tenant_id == ctx.tid).order_by(Room.name)).all()
    sites = db.scalars(select(Site).where(Site.tenant_id == ctx.tid).order_by(Site.name)).all()
    return render(request, "rooms.html", ctx, items=items, sites=sites, users=_tenant_users(db, ctx))


def _tenant_users(db: Session, ctx: Ctx) -> list[User]:
    return list(db.scalars(select(User).where(User.tenant_id == ctx.tid, User.active.is_(True)).order_by(User.name)))


def _room_from_form(r: Room, f: dict) -> None:
    r.name = str(f.get("name", "")).strip() or r.name or "Sala"
    r.capacity = max(1, int(f.get("capacity") or 1))
    r.calendar_id = str(f.get("calendar_id", "")).strip().lower() or None
    r.site_id = int(f["site_id"]) if f.get("site_id") else None
    r.caretaker_id = int(f["caretaker_id"]) if f.get("caretaker_id") else None
    r.deputy_id = int(f["deputy_id"]) if f.get("deputy_id") else None
    r.checkin_required = f.get("checkin_required") == "1"
    r.checkin_minutes = max(1, int(f.get("checkin_minutes") or 10))


@router.post("/rooms")
def room_create(request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_admin),
                db: Session = Depends(get_db)):
    n = db.scalar(select(func.count()).select_from(Room).where(Room.tenant_id == ctx.tid))
    if n >= ctx.tenant.max_rooms:
        return back(request, "/rooms", f"Limit sal w licencji: {ctx.tenant.max_rooms}.", True)
    r = Room(tenant_id=ctx.tid, name="")
    _room_from_form(r, f)
    db.add(r)
    svc.audit(db, ctx.tid, ctx.user.email, "room.create", r.name)
    db.commit()
    request_sync(r.id)
    return back(request, "/rooms", f"Dodano salę {r.name}.")


@router.get("/rooms/{rid}", response_class=HTMLResponse)
def room_edit(rid: int, request: Request, ctx: Ctx = Depends(require_admin), db: Session = Depends(get_db)):
    r = _own(db, Room, rid, ctx)
    sites = db.scalars(select(Site).where(Site.tenant_id == ctx.tid).order_by(Site.name)).all()
    return render(request, "room_edit.html", ctx, r=r, sites=sites, users=_tenant_users(db, ctx))


@router.post("/rooms/{rid}")
def room_update(rid: int, request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_admin),
                db: Session = Depends(get_db)):
    r = _own(db, Room, rid, ctx)
    old_cal = r.calendar_id
    _room_from_form(r, f)
    if r.calendar_id != old_cal:
        r.m365_subscription_id = None
    svc.audit(db, ctx.tid, ctx.user.email, "room.update", r.name)
    db.commit()
    hub.notify_room(r.id)
    request_sync(r.id)
    return back(request, "/rooms", "Zapisano salę.")


@router.post("/rooms/{rid}/delete")
def room_delete(rid: int, request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_admin),
                db: Session = Depends(get_db)):
    r = _own(db, Room, rid, ctx)
    if db.scalar(select(func.count()).select_from(Booking).where(Booking.room_id == r.id)):
        return back(request, "/rooms", "Sala ma rezerwacje – nie można jej usunąć.", True)
    for d in db.scalars(select(Device).where(Device.room_id == r.id)):
        d.room_id = None
    for et in db.scalars(select(EnrollmentToken).where(EnrollmentToken.room_id == r.id)):
        db.delete(et)
    db.delete(r)
    svc.audit(db, ctx.tid, ctx.user.email, "room.delete", r.name)
    db.commit()
    return back(request, "/rooms", "Usunięto salę.")


@router.post("/rooms/{rid}/sync")
def room_sync(rid: int, request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_admin),
              db: Session = Depends(get_db)):
    _own(db, Room, rid, ctx)
    request_sync(rid)
    return back(request, "/rooms", "Zlecono synchronizację kalendarza.")


@router.post("/sites")
def site_create(request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_admin),
                db: Session = Depends(get_db)):
    name = str(f.get("name", "")).strip()
    if name:
        db.add(Site(tenant_id=ctx.tid, name=name))
        db.commit()
    return back(request, "/rooms", "Dodano lokalizację.")


# ------------------------------------------------------------------ tablety

@router.get("/devices", response_class=HTMLResponse)
def devices(request: Request, ctx: Ctx = Depends(require_admin), db: Session = Depends(get_db)):
    items = db.scalars(select(Device).where(Device.tenant_id == ctx.tid).order_by(Device.name)).all()
    rooms_ = db.scalars(select(Room).where(Room.tenant_id == ctx.tid).order_by(Room.name)).all()
    return render(request, "devices.html", ctx, items=items, rooms=rooms_)


@router.post("/devices/enroll")
def device_enroll(request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_admin),
                  db: Session = Depends(get_db)):
    n = db.scalar(select(func.count()).select_from(Device).where(Device.tenant_id == ctx.tid))
    if n >= ctx.tenant.max_devices:
        return back(request, "/devices", f"Limit tabletów w licencji: {ctx.tenant.max_devices}.", True)
    room_id = int(f["room_id"]) if f.get("room_id") else None
    if room_id:
        _own(db, Room, room_id, ctx)
    et = EnrollmentToken(tenant_id=ctx.tid, room_id=room_id, code=new_enrollment_code(),
                         expires_at=utcnow() + timedelta(hours=24))
    db.add(et)
    svc.audit(db, ctx.tid, ctx.user.email, "device.enroll_code", f"room={room_id}")
    db.commit()
    return RedirectResponse(f"/devices/enroll/{et.id}", status_code=303)


@router.get("/devices/enroll/{eid}", response_class=HTMLResponse)
def device_enroll_qr(eid: int, request: Request, ctx: Ctx = Depends(require_admin), db: Session = Depends(get_db)):
    et = _own(db, EnrollmentToken, eid, ctx)
    payload = json.dumps({"server": config.PUBLIC_URL, "code": et.code}, separators=(",", ":"))
    img = qrcode.make(payload, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    room = db.get(Room, et.room_id) if et.room_id else None
    return render(request, "enroll.html", ctx, et=et, room=room, server=config.PUBLIC_URL,
                  qr=base64.b64encode(buf.getvalue()).decode(), tz=ctx.tenant.timezone)


@router.get("/devices/{did}", response_class=HTMLResponse)
def device_detail(did: int, request: Request, ctx: Ctx = Depends(require_admin), db: Session = Depends(get_db)):
    d = _own(db, Device, did, ctx)
    rooms_ = db.scalars(select(Room).where(Room.tenant_id == ctx.tid).order_by(Room.name)).all()
    return render(request, "device.html", ctx, d=d, rooms=rooms_, tz=ctx.tenant.timezone,
                  online=device_online(d), ws=hub.is_online(d.id),
                  has_shot=os.path.exists(screenshot_path(d.id)))


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@router.post("/devices/{did}")
def device_update(did: int, request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_admin),
                  db: Session = Depends(get_db)):
    d = _own(db, Device, did, ctx)
    d.name = str(f.get("name", d.name)).strip() or d.name
    room_id = int(f["room_id"]) if f.get("room_id") else None
    if room_id:
        _own(db, Room, room_id, ctx)
    d.room_id = room_id
    d.brightness = min(100, max(5, int(f.get("brightness") or 80)))
    off_from, off_to = str(f.get("screen_off_from", "")).strip(), str(f.get("screen_off_to", "")).strip()
    d.screen_off_from = off_from if _HHMM.match(off_from) else None
    d.screen_off_to = off_to if _HHMM.match(off_to) else None
    led = str(f.get("led_config", "")).strip()
    if led:
        try:
            json.loads(led)
        except ValueError:
            return back(request, f"/devices/{did}", "Konfiguracja LED musi być poprawnym JSON.", True)
    d.led_config = led or None
    svc.audit(db, ctx.tid, ctx.user.email, "device.update", d.name)
    db.commit()
    hub.update_room(d.id, d.room_id)
    hub.send_command(d.id, "reload")
    return back(request, f"/devices/{did}", "Zapisano. Tablet przeładuje ustawienia.")


@router.post("/devices/{did}/command")
def device_command(did: int, request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_admin),
                   db: Session = Depends(get_db)):
    d = _own(db, Device, did, ctx)
    cmd = str(f.get("command", ""))
    if cmd not in ("reload", "restart_app", "reboot", "screenshot", "update", "led_test", "exit_kiosk"):
        raise HTTPException(400)
    ok = hub.send_command(d.id, cmd)
    svc.audit(db, ctx.tid, ctx.user.email, f"device.{cmd}", d.name)
    db.commit()
    if not ok:
        return back(request, f"/devices/{did}", "Tablet nie jest połączony – polecenie niewysłane.", True)
    msg = {"screenshot": "Zlecono zrzut ekranu – odśwież stronę za kilka sekund."}.get(cmd, "Wysłano polecenie.")
    return back(request, f"/devices/{did}", msg)


@router.get("/devices/{did}/screenshot.png")
def device_screenshot(did: int, ctx: Ctx = Depends(require_admin), db: Session = Depends(get_db)):
    d = _own(db, Device, did, ctx)
    p = screenshot_path(d.id)
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/devices/{did}/delete")
def device_delete(did: int, request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_admin),
                  db: Session = Depends(get_db)):
    d = _own(db, Device, did, ctx)
    hub.send_command(d.id, "unenroll")
    for et in db.scalars(select(EnrollmentToken).where(EnrollmentToken.device_id == d.id)):
        et.device_id = None
    db.delete(d)
    svc.audit(db, ctx.tid, ctx.user.email, "device.delete", d.name)
    db.commit()
    return back(request, "/devices", "Usunięto tablet.")


# ------------------------------------------------------------------ rezerwacje

def _parse_local(value: str, tz: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


@router.get("/bookings", response_class=HTMLResponse)
def bookings(request: Request, room: int | None = None, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    tid = ctx.tid
    now = utcnow()
    q = select(Booking).where(Booking.tenant_id == tid, Booking.end > now - timedelta(hours=12)) \
        .order_by(Booking.start).limit(300)
    if room:
        q = q.where(Booking.room_id == room)
    items = db.scalars(q).all()
    rooms_ = db.scalars(select(Room).where(Room.tenant_id == tid).order_by(Room.name)).all()
    default_start = svc.to_local(svc.minute(now) + timedelta(minutes=15 - now.minute % 15), ctx.tenant.timezone)
    return render(request, "bookings.html", ctx, items=items, rooms=rooms_, room_filter=room,
                  tz=ctx.tenant.timezone, default_start=f"{default_start:%Y-%m-%dT%H:%M}")


@router.post("/bookings")
def booking_create(request: Request, f: dict = Depends(form), ctx: Ctx = Depends(get_ctx),
                   db: Session = Depends(get_db)):
    room = _own(db, Room, int(f.get("room_id") or 0), ctx)
    try:
        start = _parse_local(str(f.get("start")), ctx.tenant.timezone)
    except ValueError:
        return back(request, "/bookings", "Nieprawidłowa data.", True)
    end = start + timedelta(minutes=int(f.get("duration") or 60))
    emails = [e for e in re.split(r"[\s,;]+", str(f.get("attendees", ""))) if "@" in e]
    try:
        b = svc.create_booking(db, ctx.tenant, room, title=str(f.get("title", "")).strip(), start=start, end=end,
                               organizer=(ctx.user.email, ctx.user.name), attendees=emails, source="panel",
                               actor=ctx.user.email)
    except svc.BookingError as exc:
        db.rollback()
        return back(request, "/bookings", str(exc), True)
    if b.status == "pending_approval":
        waiting = sum(1 for a in b.attendees if a.status == "waiting")
        return back(request, "/bookings",
                    f"Zarezerwowano. Liczba osób przekracza pojemność sali ({room.capacity}) – {waiting} os. "
                    "czeka na zatwierdzenie przez opiekuna sali.")
    return back(request, "/bookings", "Zarezerwowano salę.")


@router.post("/bookings/{bid}/cancel")
def booking_cancel(bid: int, request: Request, f: dict = Depends(form), ctx: Ctx = Depends(get_ctx),
                   db: Session = Depends(get_db)):
    b = _own(db, Booking, bid, ctx)
    if not (ctx.is_admin or b.organizer_email == ctx.user.email or _room_of_booking_ok(b, ctx)):
        raise HTTPException(403)
    try:
        svc.cancel_booking(db, ctx.tenant, b, ctx.user.email, "Anulowano w panelu")
    except svc.BookingError as exc:
        db.rollback()
        return back(request, "/bookings", str(exc), True)
    return back(request, "/bookings", "Anulowano rezerwację.")


# ------------------------------------------------------------------ zatwierdzenia

@router.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    items = [b for b in db.scalars(select(Booking).where(
        Booking.tenant_id == ctx.tid, Booking.status == "pending_approval").order_by(Booking.start))
        if _room_of_booking_ok(b, ctx)]
    return render(request, "approvals.html", ctx, items=items, tz=ctx.tenant.timezone, tenant=ctx.tenant)


@router.post("/approvals/{bid}/{decision}")
def approval_decide(bid: int, decision: str, request: Request, f: dict = Depends(form),
                    ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    b = _own(db, Booking, bid, ctx)
    if not _room_of_booking_ok(b, ctx):
        raise HTTPException(403)
    try:
        if decision == "approve":
            svc.approve(db, ctx.tenant, b, ctx.user)
            msg = "Zatwierdzono."
        elif decision == "reject":
            svc.reject(db, ctx.tenant, b, ctx.user, str(f.get("reason", "")).strip())
            msg = "Odrzucono."
        else:
            raise HTTPException(400)
    except svc.BookingError as exc:
        db.rollback()
        return back(request, "/approvals", str(exc), True)
    return back(request, "/approvals", msg)


# ------------------------------------------------------------------ użytkownicy i karty NFC

@router.get("/users", response_class=HTMLResponse)
def users(request: Request, ctx: Ctx = Depends(require_admin), db: Session = Depends(get_db)):
    items = db.scalars(select(User).where(User.tenant_id == ctx.tid).order_by(User.name)).all()
    cards = db.scalars(select(NfcCard).where(NfcCard.tenant_id == ctx.tid)).all()
    unknown = db.scalars(select(AuditLog).where(AuditLog.tenant_id == ctx.tid, AuditLog.action == "nfc.unknown")
                         .order_by(AuditLog.id.desc()).limit(5)).all()
    return render(request, "users.html", ctx, items=items, cards=cards, unknown=unknown)


@router.post("/users")
def user_create(request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_admin),
                db: Session = Depends(get_db)):
    role = str(f.get("role", "user"))
    if role not in ("admin", "caretaker", "user"):
        raise HTTPException(400)
    pw = str(f.get("password", ""))
    u = User(tenant_id=ctx.tid, email=str(f.get("email", "")).strip().lower(), name=str(f.get("name", "")).strip(),
             role=role, password_hash=hash_secret(pw) if pw else "")
    pin = str(f.get("pin", "")).strip()
    if pin:
        if not pin.isdigit() or len(pin) < 4:
            return back(request, "/users", "PIN: min. 4 cyfry.", True)
        u.pin_hash = hash_secret(pin)
    db.add(u)
    try:
        svc.audit(db, ctx.tid, ctx.user.email, "user.create", u.email)
        db.commit()
    except IntegrityError:
        db.rollback()
        return back(request, "/users", "Użytkownik z tym e-mailem już istnieje.", True)
    return back(request, "/users", "Dodano użytkownika.")


@router.post("/users/{uid}")
def user_update(uid: int, request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_admin),
                db: Session = Depends(get_db)):
    u = _own(db, User, uid, ctx)
    role = str(f.get("role", u.role))
    if role in ("admin", "caretaker", "user"):
        u.role = role
    u.name = str(f.get("name", u.name)).strip()
    u.active = f.get("active") == "1"
    if f.get("password"):
        u.password_hash = hash_secret(str(f["password"]))
    pin = str(f.get("pin", "")).strip()
    if pin:
        if not pin.isdigit() or len(pin) < 4:
            return back(request, "/users", "PIN: min. 4 cyfry.", True)
        u.pin_hash = hash_secret(pin)
    svc.audit(db, ctx.tid, ctx.user.email, "user.update", u.email)
    db.commit()
    return back(request, "/users", "Zapisano.")


@router.post("/nfc")
def nfc_create(request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_admin),
               db: Session = Depends(get_db)):
    uid = re.sub(r"[^0-9A-Fa-f]", "", str(f.get("uid", ""))).upper()
    user = _own(db, User, int(f.get("user_id") or 0), ctx)
    if not uid:
        return back(request, "/users", "Podaj UID karty.", True)
    db.add(NfcCard(tenant_id=ctx.tid, uid=uid, user_id=user.id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return back(request, "/users", "Ta karta jest już przypisana.", True)
    return back(request, "/users", "Dodano kartę.")


@router.post("/nfc/{cid}/delete")
def nfc_delete(cid: int, request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_admin),
               db: Session = Depends(get_db)):
    db.delete(_own(db, NfcCard, cid, ctx))
    db.commit()
    return back(request, "/users", "Usunięto kartę.")


# ------------------------------------------------------------------ ustawienia klienta i M365

@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request, ctx: Ctx = Depends(require_admin)):
    tzs = sorted(z for z in available_timezones() if z.startswith("Europe/"))
    return render(request, "settings.html", ctx, t=ctx.tenant, tzs=tzs,
                  m365_ready=bool(config.M365_CLIENT_ID and config.M365_CLIENT_SECRET),
                  webhook_url=f"{config.PUBLIC_URL}/api/webhooks/graph")


@router.post("/settings")
def settings_save(request: Request, f: dict = Depends(form), ctx: Ctx = Depends(require_admin),
                  db: Session = Depends(get_db)):
    t = db.get(Tenant, ctx.tid)
    if f.get("timezone") in available_timezones():
        t.timezone = str(f["timezone"])
    if f.get("calendar_provider") in ("local", "m365", "google"):
        t.calendar_provider = str(f["calendar_provider"])
    t.require_auth_on_tablet = f.get("require_auth_on_tablet") == "1"
    t.count_only_accepted = f.get("count_only_accepted") == "1"
    t.approval_default = "allow" if f.get("approval_default") == "allow" else "reject"
    t.approval_escalate_hours = max(1, int(f.get("approval_escalate_hours") or 4))
    svc.audit(db, t.id, ctx.user.email, "tenant.settings", "")
    db.commit()
    request_sync()
    return back(request, "/settings", "Zapisano ustawienia.")


_consent_signer = URLSafeTimedSerializer(config.SECRET_KEY, salt="m365-consent")


def _tenant_url(t: Tenant, path: str) -> str:
    if config.BASE_DOMAIN:
        return f"https://{t.slug}.{config.BASE_DOMAIN}{path}"
    return path


@router.get("/m365/consent")
def m365_consent(request: Request, ctx: Ctx = Depends(require_admin)):
    # Stan podpisany kluczem serwera – powrót trafia na PUBLIC_URL, niezależnie od subdomeny klienta.
    state = _consent_signer.dumps({"tid": ctx.tid, "uid": ctx.user.id})
    q = urlencode({"client_id": config.M365_CLIENT_ID, "redirect_uri": f"{config.PUBLIC_URL}/m365/callback",
                   "state": state, "scope": "https://graph.microsoft.com/.default"})
    return RedirectResponse(f"https://login.microsoftonline.com/organizations/v2.0/adminconsent?{q}")


@router.get("/m365/callback", response_class=HTMLResponse)
def m365_callback(request: Request, tenant: str = "", admin_consent: str = "", state: str = "",
                  error_description: str = "", db: Session = Depends(get_db)):
    try:
        data = _consent_signer.loads(state, max_age=3600)
    except (BadSignature, SignatureExpired):
        return HTMLResponse("Nieprawidłowy lub wygasły link zgody – spróbuj ponownie z panelu.", status_code=400)
    t = db.get(Tenant, data["tid"])
    if t is None:
        raise HTTPException(404)
    if admin_consent.lower() != "true" or not re.fullmatch(r"[0-9a-fA-F-]{36}", tenant or ""):
        return HTMLResponse(f"Zgoda nie została udzielona. {error_description}<br>"
                            f'<a href="{_tenant_url(t, "/settings")}">Wróć do ustawień</a>', status_code=400)
    t.m365_tenant_id = tenant
    t.m365_consented = True
    t.calendar_provider = "m365"
    svc.audit(db, t.id, f"user:{data['uid']}", "m365.consent", tenant)
    db.commit()
    request_sync()
    return RedirectResponse(_tenant_url(t, "/settings"), status_code=303)


# ------------------------------------------------------------------ dziennik

@router.get("/audit", response_class=HTMLResponse)
def audit_log(request: Request, ctx: Ctx = Depends(require_admin), db: Session = Depends(get_db)):
    items = db.scalars(select(AuditLog).where(AuditLog.tenant_id == ctx.tid)
                       .order_by(AuditLog.id.desc()).limit(300)).all()
    return render(request, "audit.html", ctx, items=items, tz=ctx.tenant.timezone)
