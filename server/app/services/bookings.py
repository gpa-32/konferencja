"""Logika rezerwacji: kolizje, pojemność, zatwierdzanie, akcje z tabletu, synchronizacja kalendarza."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..models import Attendee, AuditLog, Booking, Room, Tenant, User, utcnow
from .calendar import CalendarError, ExtEvent, Provider, safe_provider
from .hub import hub
from .notify import send_email

log = logging.getLogger("bookings")
ACTIVE = ("confirmed", "pending_approval")


class BookingError(Exception):
    pass


def audit(db: Session, tenant_id: int | None, actor: str, action: str, details: str = "") -> None:
    db.add(AuditLog(tenant_id=tenant_id, actor=actor, action=action, details=details))


def minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


# ---------------------------------------------------------------- zapytania

def overlapping(db: Session, room_id: int, start: datetime, end: datetime,
                exclude_id: int | None = None) -> list[Booking]:
    q = select(Booking).where(
        Booking.room_id == room_id, Booking.status.in_(ACTIVE),
        Booking.start < end, Booking.end > start)
    if exclude_id:
        q = q.where(Booking.id != exclude_id)
    return list(db.scalars(q))


def next_booking(db: Session, room_id: int, after: datetime, exclude_id: int | None = None) -> Booking | None:
    q = select(Booking).where(Booking.room_id == room_id, Booking.status.in_(ACTIVE),
                              Booking.start >= after).order_by(Booking.start)
    if exclude_id:
        q = q.where(Booking.id != exclude_id)
    return db.scalars(q.limit(1)).first()


def current_booking(db: Session, room_id: int, now: datetime) -> Booking | None:
    return db.scalars(select(Booking).where(
        Booking.room_id == room_id, Booking.status.in_(ACTIVE),
        Booking.start <= now, Booking.end > now).order_by(Booking.start).limit(1)).first()


def counted_attendees(booking: Booking, tenant: Tenant) -> int:
    """Liczba osób do porównania z pojemnością (z listą oczekujących)."""
    n = 0
    for a in booking.attendees:
        if a.status == "declined":
            continue
        if tenant.count_only_accepted and not a.is_organizer and a.status not in ("accepted", "tentative", "waiting"):
            continue
        n += 1
    return max(n, 1)


def invited_count(booking: Booking) -> int:
    """Osoby faktycznie zaproszone (bez listy oczekujących i odmów) – pokazywane na tablecie."""
    return max(sum(1 for a in booking.attendees if a.status not in ("declined", "waiting")), 1)


# ---------------------------------------------------------------- tworzenie

def _notify_caretaker(db: Session, booking: Booking, escalate: bool = False) -> None:
    room = booking.room
    who = room.deputy if escalate else (room.caretaker or room.deputy)
    if who is None:
        return
    local = to_local(booking.start, room.tenant.timezone)
    send_email(
        who.email,
        f"[Sale] Przekroczona pojemność: {room.name} {local:%d.%m %H:%M}",
        f"Rezerwacja „{booking.title}” w sali {room.name} ({local:%d.%m.%Y %H:%M}) ma "
        f"{len([a for a in booking.attendees if a.status != 'declined'])} osób przy pojemności {room.capacity}.\n"
        f"Organizator: {booking.organizer_name} <{booking.organizer_email}>\n\n"
        f"Zatwierdź lub odrzuć: {config.PUBLIC_URL}/approvals\n",
    )


def to_local(dt: datetime, tz: str) -> datetime:
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz))


def create_booking(db: Session, tenant: Tenant, room: Room, *, title: str, start: datetime, end: datetime,
                   organizer: tuple[str, str], attendees: list[str], source: str, actor: str,
                   provider: Provider | None = None) -> Booking:
    start, end = minute(start), minute(end)
    if end <= start:
        raise BookingError("Koniec spotkania musi być po jego początku.")
    if overlapping(db, room.id, start, end):
        raise BookingError("Sala jest w tym czasie zajęta.")

    org_email, org_name = organizer
    booking = Booking(tenant_id=tenant.id, room_id=room.id, title=title or "Rezerwacja", start=start, end=end,
                      organizer_email=org_email or "", organizer_name=org_name or "", source=source)
    booking.room = room
    seen: set[str] = set()
    people: list[tuple[str, str, bool]] = []
    if org_email or org_name:
        people.append((org_email.lower(), org_name, True))
        seen.add(org_email.lower())
    for e in attendees:
        e = e.strip().lower()
        if e and e not in seen:
            seen.add(e)
            people.append((e, "", False))

    over = len(people) > room.capacity
    for i, (email, name, is_org) in enumerate(people):
        status = "accepted" if is_org else "invited"
        if over and i >= room.capacity:
            status = "waiting"
        booking.attendees.append(Attendee(email=email, name=name, is_organizer=is_org, status=status))
    if over:
        booking.status = "pending_approval"
        booking.approval_requested_at = utcnow()

    if provider is None:
        provider, err = safe_provider(tenant)
        if err and tenant.calendar_provider != "local":
            raise BookingError(err)
    if provider.external:
        invited = [(a.email, a.name) for a in booking.attendees if a.status != "waiting"]
        try:
            booking.external_id = provider.create_event(
                room, booking.title, start, end, invited,
                body=f"Rezerwacja utworzona przez {actor} ({source}).")
        except CalendarError as exc:
            raise BookingError(f"Kalendarz odrzucił rezerwację: {exc}") from exc

    db.add(booking)
    audit(db, tenant.id, actor, "booking.create",
          f"{room.name} {start:%Y-%m-%d %H:%M}-{end:%H:%M} {booking.title} ({len(people)} os., {booking.status})")
    db.commit()
    if over:
        _notify_caretaker(db, booking)
    hub.notify_room(room.id)
    return booking


def book_now(db: Session, tenant: Tenant, room: Room, minutes: int, actor: str, title: str = "",
             organizer: tuple[str, str] = ("", "")) -> Booking:
    now = minute(utcnow())
    if current_booking(db, room.id, now):
        raise BookingError("Sala jest teraz zajęta.")
    end = now + timedelta(minutes=minutes)
    nxt = next_booking(db, room.id, now)
    if nxt and nxt.start < end:
        raise BookingError(f"Sala wolna tylko do {to_local(nxt.start, tenant.timezone):%H:%M}.")
    b = create_booking(db, tenant, room, title=title or "Rezerwacja z tabletu", start=now, end=end,
                       organizer=organizer, attendees=[], source="tablet", actor=actor)
    b.checked_in_at = now
    db.commit()
    return b


# ---------------------------------------------------------------- zmiany

def _update_times_in_calendar(tenant: Tenant, booking: Booking) -> None:
    if not booking.external_id:
        return
    provider, err = safe_provider(tenant)
    if err:
        raise BookingError(err)
    try:
        provider.update_times(booking.room, booking.external_id, booking.start, booking.end)
    except CalendarError as exc:
        raise BookingError(f"Nie udało się zmienić rezerwacji w kalendarzu: {exc}") from exc


def end_early(db: Session, tenant: Tenant, booking: Booking, actor: str, status: str = "confirmed") -> None:
    now = minute(utcnow())
    if booking.end <= now:
        return
    if booking.start >= now:
        # Spotkanie jeszcze się nie zaczęło – zwolnienie = anulowanie.
        cancel_booking(db, tenant, booking, actor, "Sala zwolniona z tabletu")
        return
    booking.end = now
    booking.status = status
    _update_times_in_calendar(tenant, booking)
    audit(db, tenant.id, actor, "booking.end", f"{booking.room.name} {booking.title} zakończone o {now:%H:%M} UTC")
    db.commit()
    hub.notify_room(booking.room_id)


def extend(db: Session, tenant: Tenant, booking: Booking, minutes: int, actor: str) -> None:
    new_end = booking.end + timedelta(minutes=minutes)
    if overlapping(db, booking.room_id, booking.end, new_end, exclude_id=booking.id):
        nxt = next_booking(db, booking.room_id, booking.end, exclude_id=booking.id)
        until = f" (wolna do {to_local(nxt.start, tenant.timezone):%H:%M})" if nxt else ""
        raise BookingError("Nie można przedłużyć – kolejna rezerwacja" + until + ".")
    booking.end = new_end
    _update_times_in_calendar(tenant, booking)
    audit(db, tenant.id, actor, "booking.extend", f"{booking.room.name} {booking.title} +{minutes} min")
    db.commit()
    hub.notify_room(booking.room_id)


def checkin(db: Session, tenant: Tenant, booking: Booking, actor: str) -> None:
    booking.checked_in_at = utcnow()
    audit(db, tenant.id, actor, "booking.checkin", f"{booking.room.name} {booking.title}")
    db.commit()
    hub.notify_room(booking.room_id)


def cancel_booking(db: Session, tenant: Tenant, booking: Booking, actor: str, comment: str = "") -> None:
    if booking.external_id:
        provider, err = safe_provider(tenant)
        if not err:
            try:
                if booking.source in ("tablet", "panel"):
                    provider.cancel(booking.room, booking.external_id, comment)
                else:
                    provider.respond(booking.room, booking.external_id, "decline", comment)
            except CalendarError as exc:
                raise BookingError(f"Kalendarz: {exc}") from exc
    booking.status = "cancelled"
    audit(db, tenant.id, actor, "booking.cancel", f"{booking.room.name} {booking.title} {comment}")
    db.commit()
    hub.notify_room(booking.room_id)


def approve(db: Session, tenant: Tenant, booking: Booking, user: User | None) -> None:
    if booking.status != "pending_approval":
        raise BookingError("Ta rezerwacja nie czeka na zatwierdzenie.")
    provider, err = safe_provider(tenant)
    if err and booking.external_id:
        raise BookingError(err)
    for a in booking.attendees:
        if a.status == "waiting":
            a.status = "invited"
    try:
        if booking.external_id and booking.source in ("tablet", "panel"):
            provider.set_attendees(booking.room, booking.external_id,
                                   [(a.email, a.name) for a in booking.attendees if a.status != "declined"])
        elif booking.external_id:
            provider.respond(booking.room, booking.external_id, "accept",
                             "Przekroczenie pojemności zatwierdzone przez opiekuna sali.")
    except CalendarError as exc:
        raise BookingError(f"Kalendarz: {exc}") from exc
    booking.status = "confirmed"
    booking.decided_by_id = user.id if user else None
    booking.decided_at = utcnow()
    audit(db, tenant.id, user.email if user else "system", "booking.approve", f"{booking.room.name} {booking.title}")
    db.commit()
    send_email(booking.organizer_email, f"[Sale] Zatwierdzono: {booking.title}",
               f"Opiekun sali {booking.room.name} zatwierdził spotkanie z liczbą osób ponad pojemność. "
               "Zaproszenia do wszystkich uczestników zostały wysłane.")
    hub.notify_room(booking.room_id)


def reject(db: Session, tenant: Tenant, booking: Booking, user: User | None, reason: str = "") -> None:
    """Rezerwacja własna: spotkanie zostaje, osoby z listy oczekujących nie są zapraszane.
    Rezerwacja z Outlooka/Google: sala odrzuca zaproszenie (nie można odwołać cudzych zaproszeń)."""
    if booking.status != "pending_approval":
        raise BookingError("Ta rezerwacja nie czeka na zatwierdzenie.")
    actor = user.email if user else "system"
    msg = "Przekroczona pojemność sali – opiekun nie zatwierdził dodatkowych osób." + (f" {reason}" if reason else "")
    if booking.source in ("tablet", "panel"):
        for a in booking.attendees:
            if a.status == "waiting":
                a.status = "declined"
        booking.status = "confirmed"
        mail = (f"Spotkanie „{booking.title}” w sali {booking.room.name} odbędzie się, ale bez osób ponad "
                f"pojemność ({booking.room.capacity}). {reason}")
    else:
        provider, err = safe_provider(tenant)
        if err:
            raise BookingError(err)
        try:
            provider.respond(booking.room, booking.external_id, "decline", msg)
        except CalendarError as exc:
            raise BookingError(f"Kalendarz: {exc}") from exc
        booking.status = "rejected"
        mail = f"Sala {booking.room.name} odrzuciła spotkanie „{booking.title}”: {msg}"
    booking.decided_by_id = user.id if user else None
    booking.decided_at = utcnow()
    audit(db, tenant.id, actor, "booking.reject", f"{booking.room.name} {booking.title} {reason}")
    db.commit()
    send_email(booking.organizer_email, f"[Sale] Odrzucono dodatkowe osoby: {booking.title}", mail)
    hub.notify_room(booking.room_id)


# ---------------------------------------------------------------- synchronizacja z kalendarzem

def _apply_attendees(booking: Booking, ev: ExtEvent) -> None:
    existing = {a.email: a for a in booking.attendees}
    seen = set()
    for ea in ev.attendees:
        if ea.is_resource:
            continue
        seen.add(ea.email)
        a = existing.get(ea.email)
        if a is None:
            booking.attendees.append(Attendee(email=ea.email, name=ea.name, status=ea.status,
                                              is_organizer=ea.is_organizer))
        elif a.status != "waiting":
            a.status, a.name, a.is_organizer = ea.status, ea.name or a.name, ea.is_organizer
    if booking.source not in ("tablet", "panel"):
        for email, a in existing.items():
            if email not in seen:
                booking.attendees.remove(a)


def sync_room(db: Session, tenant: Tenant, room: Room, provider: Provider) -> int:
    """Pobiera wydarzenia sali i uzgadnia z bazą. Zwraca liczbę zmian."""
    now = utcnow()
    start = minute(now - timedelta(hours=12))
    end = now + timedelta(days=config.SYNC_DAYS_AHEAD)
    events = provider.list_events(room, start, end)
    by_ext = {b.external_id: b for b in db.scalars(select(Booking).where(
        Booking.room_id == room.id, Booking.external_id.is_not(None),
        Booking.end > start, Booking.start < end))}
    changes = 0
    seen: set[str] = set()

    for ev in events:
        seen.add(ev.id)
        b = by_ext.get(ev.id)
        gone = ev.cancelled or ev.room_response == "declined"
        if b is None:
            if gone:
                continue
            b = Booking(tenant_id=tenant.id, room_id=room.id, external_id=ev.id, source=tenant.calendar_provider,
                        title=ev.title, organizer_name=ev.organizer_name, organizer_email=ev.organizer_email,
                        start=ev.start, end=ev.end, is_private=ev.is_private, status="confirmed")
            b.room = room
            _apply_attendees(b, ev)
            db.add(b)
            changes += 1
            _decide_capacity(db, tenant, room, b, ev, provider, new=True)
            continue

        if gone:
            if b.status in ACTIVE:
                b.status = "cancelled"
                changes += 1
            continue
        if (b.start, b.end, b.title) != (ev.start, ev.end, ev.title) and b.status != "released":
            b.start, b.end, b.title = ev.start, ev.end, ev.title
            changes += 1
        before = counted_attendees(b, tenant)
        _apply_attendees(b, ev)
        if b.source not in ("tablet", "panel") and b.status in ACTIVE:
            if counted_attendees(b, tenant) != before or ev.room_response in ("none",):
                _decide_capacity(db, tenant, room, b, ev, provider, new=False)
                changes += 1

    # Wydarzenia usunięte z kalendarza
    for ext_id, b in by_ext.items():
        if ext_id not in seen and b.status in ACTIVE and b.end > now:
            b.status = "cancelled"
            changes += 1

    room.last_sync_at = now
    room.last_sync_error = None
    db.commit()
    if changes:
        hub.notify_room(room.id)
    return changes


def _decide_capacity(db: Session, tenant: Tenant, room: Room, b: Booking, ev: ExtEvent, provider: Provider,
                     new: bool) -> None:
    """Rezerwacja z Outlooka/Google: sala akceptuje w limicie, ponad limit – wstępnie + opiekun."""
    count = counted_attendees(b, tenant)
    if b.decided_by_id or b.decided_at:
        return  # opiekun już zdecydował
    if count <= room.capacity:
        if b.status == "pending_approval":
            b.status = "confirmed"
        if ev.room_response in ("none", "tentative"):
            try:
                provider.respond(room, ev.id, "accept")
            except CalendarError as exc:
                log.warning("accept %s: %s", room.name, exc)
        return
    if b.status != "pending_approval":
        b.status = "pending_approval"
        b.approval_requested_at = utcnow()
        try:
            provider.respond(room, ev.id, "tentativelyAccept",
                             f"Liczba uczestników ({count}) przekracza pojemność sali ({room.capacity}). "
                             "Rezerwacja czeka na zatwierdzenie przez opiekuna sali.")
        except CalendarError as exc:
            log.warning("tentative %s: %s", room.name, exc)
        audit(db, tenant.id, "system", "booking.over_capacity", f"{room.name} {b.title} {count}/{room.capacity}")
        db.flush()
        _notify_caretaker(db, b)
        send_email(b.organizer_email, f"[Sale] Czeka na zatwierdzenie: {b.title}",
                   f"Twoje spotkanie ma {count} uczestników, a sala {room.name} mieści {room.capacity} osób. "
                   "Opiekun sali musi zatwierdzić rezerwację.")


# ---------------------------------------------------------------- porządki (worker)

def housekeeping(db: Session) -> None:
    now = utcnow()
    # 1) Brak check-in → zwolnienie sali
    q = select(Booking).join(Room).where(
        Booking.status == "confirmed", Room.checkin_required.is_(True),
        Booking.checked_in_at.is_(None), Booking.start <= now, Booking.end > now)
    for b in db.scalars(q).all():
        if b.start + timedelta(minutes=b.room.checkin_minutes) <= now:
            tenant = db.get(Tenant, b.tenant_id)
            try:
                end_early(db, tenant, b, "system", status="released")
                audit(db, tenant.id, "system", "booking.auto_release", f"{b.room.name} {b.title}")
                db.commit()
            except BookingError as exc:
                log.warning("auto-release %s: %s", b.id, exc)

    # 2) Zatwierdzenia: eskalacja do zastępcy i decyzja domyślna przed spotkaniem
    for b in db.scalars(select(Booking).where(Booking.status == "pending_approval")).all():
        tenant = db.get(Tenant, b.tenant_id)
        if (not b.approval_escalated and b.approval_requested_at
                and b.approval_requested_at + timedelta(hours=tenant.approval_escalate_hours) <= now):
            b.approval_escalated = True
            db.commit()
            _notify_caretaker(db, b, escalate=True)
        if b.start - timedelta(minutes=15) <= now:
            try:
                if tenant.approval_default == "allow":
                    approve(db, tenant, b, None)
                else:
                    reject(db, tenant, b, None, "Brak decyzji opiekuna przed spotkaniem.")
            except BookingError as exc:
                log.warning("approval default %s: %s", b.id, exc)
