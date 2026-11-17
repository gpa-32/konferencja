"""Model danych. Wszystkie daty w UTC (naive). Każdy rekord klienta ma tenant_id."""
from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Tenant(Base):
    """Klient ePartners."""
    __tablename__ = "tenant"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    max_rooms: Mapped[int] = mapped_column(Integer, default=10)
    max_devices: Mapped[int] = mapped_column(Integer, default=10)
    # local | m365 | google
    calendar_provider: Mapped[str] = mapped_column(String(20), default="local")
    m365_tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    m365_consented: Mapped[bool] = mapped_column(Boolean, default=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Warsaw")
    # Akcje na tablecie wymagają PIN/NFC
    require_auth_on_tablet: Mapped[bool] = mapped_column(Boolean, default=False)
    # Liczyć tylko uczestników, którzy zaakceptowali/wstępnie (zamiast wszystkich zaproszonych)
    count_only_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    # Decyzja, gdy opiekun nie zdecyduje przed spotkaniem: reject | allow
    approval_default: Mapped[str] = mapped_column(String(10), default="reject")
    # Eskalacja do zastępcy po tylu godzinach
    approval_escalate_hours: Mapped[int] = mapped_column(Integer, default=4)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "user"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenant.id"), nullable=True)  # None = superadmin
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(300), default="")
    # superadmin | admin | caretaker | user
    role: Mapped[str] = mapped_column(String(20), default="user")
    pin_hash: Mapped[str | None] = mapped_column(String(300), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NfcCard(Base):
    __tablename__ = "nfc_card"
    __table_args__ = (UniqueConstraint("tenant_id", "uid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    uid: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    user: Mapped[User] = relationship()


class Site(Base):
    """Lokalizacja: budynek / piętro."""
    __tablename__ = "site"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    name: Mapped[str] = mapped_column(String(200))


class Room(Base):
    __tablename__ = "room"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    site_id: Mapped[int | None] = mapped_column(ForeignKey("site.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    capacity: Mapped[int] = mapped_column(Integer, default=8)
    # Adres skrzynki sali (M365) lub ID kalendarza zasobu (Google)
    calendar_id: Mapped[str | None] = mapped_column(String(254), nullable=True)
    caretaker_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    deputy_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    checkin_required: Mapped[bool] = mapped_column(Boolean, default=False)
    checkin_minutes: Mapped[int] = mapped_column(Integer, default=10)
    m365_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    m365_subscription_expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    tenant: Mapped[Tenant] = relationship()
    site: Mapped[Site | None] = relationship()
    caretaker: Mapped[User | None] = relationship(foreign_keys=[caretaker_id])
    deputy: Mapped[User | None] = relationship(foreign_keys=[deputy_id])


class Device(Base):
    """Tablet przy sali."""
    __tablename__ = "device"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    room_id: Mapped[int | None] = mapped_column(ForeignKey("room.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    model: Mapped[str] = mapped_column(String(200), default="")
    android_version: Mapped[str] = mapped_column(String(40), default="")
    app_version: Mapped[str] = mapped_column(String(40), default="")
    has_led: Mapped[bool] = mapped_column(Boolean, default=False)
    has_nfc: Mapped[bool] = mapped_column(Boolean, default=False)
    is_root: Mapped[bool] = mapped_column(Boolean, default=False)
    is_device_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    # Ustawienia zdalne
    brightness: Mapped[int] = mapped_column(Integer, default=80)  # %
    screen_off_from: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "20:00"
    screen_off_to: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "07:00"
    # Ścieżki sysfs listwy LED (gdy model nie jest znany): JSON {"red": "...", "green": "..."}
    led_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    battery: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    screenshot_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rustdesk_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    room: Mapped[Room | None] = relationship()


class EnrollmentToken(Base):
    """Jednorazowy kod QR do dodania tabletu."""
    __tablename__ = "enrollment_token"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    room_id: Mapped[int | None] = mapped_column(ForeignKey("room.id"), nullable=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("device.id"), nullable=True)


class Booking(Base):
    __tablename__ = "booking"
    __table_args__ = (UniqueConstraint("room_id", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"))
    room_id: Mapped[int] = mapped_column(ForeignKey("room.id"))
    external_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    organizer_name: Mapped[str] = mapped_column(String(200), default="")
    organizer_email: Mapped[str] = mapped_column(String(254), default="")
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    # tablet | panel | m365 | google
    source: Mapped[str] = mapped_column(String(20), default="panel")
    # confirmed | pending_approval | rejected | cancelled | released
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approval_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approval_escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    room: Mapped[Room] = relationship()
    attendees: Mapped[list["Attendee"]] = relationship(
        back_populates="booking", cascade="all, delete-orphan", order_by="Attendee.id")


class Attendee(Base):
    __tablename__ = "attendee"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("booking.id"))
    email: Mapped[str] = mapped_column(String(254), default="")
    name: Mapped[str] = mapped_column(String(200), default="")
    # invited | waiting | accepted | tentative | declined
    status: Mapped[str] = mapped_column(String(20), default="invited")
    is_organizer: Mapped[bool] = mapped_column(Boolean, default=False)
    booking: Mapped[Booking] = relationship(back_populates="attendees")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenant.id"), nullable=True)
    actor: Mapped[str] = mapped_column(String(254), default="system")
    action: Mapped[str] = mapped_column(String(100))
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
