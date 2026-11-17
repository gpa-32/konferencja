import os
import re
import sys
import tempfile
from datetime import timedelta

_tmp = tempfile.mkdtemp()
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmp}/t.db", "DISABLE_WORKER": "1", "DATA_DIR": _tmp,
    "SUPERADMIN_EMAIL": "super@epartners.pl", "SUPERADMIN_PASSWORD": "superhaslo123",
})
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Booking, Room, Tenant, utcnow  # noqa: E402
from app.services import bookings as svc  # noqa: E402
from app.services.calendar import ExtAttendee, ExtEvent, Provider  # noqa: E402


def csrf(client, url):
    html = client.get(url).text
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)


def post(client, url, data, page="/"):
    data = dict(data)
    data["_csrf"] = csrf(client, page)
    return client.post(url, data=data, follow_redirects=False)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_full_flow(client):
    # superadmin tworzy klienta
    r = post(client, "/login", {"email": "super@epartners.pl", "password": "superhaslo123"}, "/login")
    assert r.status_code == 303
    r = post(client, "/tenants", {"name": "Firma A", "slug": "firma-a", "calendar_provider": "local",
                                  "max_rooms": 5, "max_devices": 5, "admin_email": "admin@firma-a.pl",
                                  "admin_password": "adminhaslo123"}, "/tenants")
    assert r.status_code == 303
    client.get("/logout")

    # admin klienta: użytkownik-opiekun i sala
    post(client, "/login", {"email": "admin@firma-a.pl", "password": "adminhaslo123"}, "/login")
    assert "Pulpit" in client.get("/").text
    post(client, "/users", {"name": "Opiekun", "email": "opiekun@firma-a.pl", "role": "caretaker",
                            "password": "opiekun12345", "pin": "1234"}, "/users")
    db = SessionLocal()
    from app.models import User
    caretaker = db.query(User).filter_by(email="opiekun@firma-a.pl").one()
    post(client, "/rooms", {"name": "Sala Mała", "capacity": 3, "caretaker_id": caretaker.id}, "/rooms")
    room = db.query(Room).filter_by(name="Sala Mała").one()

    # kod QR i rejestracja tabletu
    r = post(client, "/devices/enroll", {"room_id": room.id}, "/devices")
    assert r.status_code == 303
    page = client.get(r.headers["location"]).text
    code = re.search(r'font-size:18px">([A-Z0-9-]+)<', page).group(1)
    r = client.post("/api/device/enroll", json={"code": code, "model": "Qbic TD-1060", "has_led": True})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert client.post("/api/device/enroll", json={"code": code}).status_code == 400  # jednorazowy
    H = {"Authorization": f"Bearer {token}"}

    st = client.get("/api/device/state", headers=H).json()
    assert st["room"]["name"] == "Sala Mała" and st["status"] == "free"
    assert 30 in st["book_options"]

    # akcje z tabletu
    st = client.post("/api/device/action", headers=H, json={"action": "book_now", "minutes": 30}).json()
    assert st["status"] == "busy"
    bid = st["current"]["id"]
    st = client.post("/api/device/action", headers=H, json={"action": "extend", "booking_id": bid,
                                                             "minutes": 15}).json()
    assert st["current"]["end"] - st["current"]["start"] == 45 * 60 * 1000
    assert client.post("/api/device/action", headers=H, json={"action": "book_now", "minutes": 30}).status_code == 409
    st = client.post("/api/device/action", headers=H, json={"action": "end", "booking_id": bid}).json()
    assert st["status"] == "free"

    # rezerwacja w panelu ponad pojemność → oczekujący + zatwierdzenie
    start = svc.to_local(utcnow() + timedelta(hours=3), "Europe/Warsaw")
    r = post(client, "/bookings", {"room_id": room.id, "title": "Zarząd", "start": f"{start:%Y-%m-%dT%H:%M}",
                                   "duration": 60, "attendees": "a@x.pl, b@x.pl, c@x.pl, d@x.pl"}, "/bookings")
    assert r.status_code == 303
    db.expire_all()
    b = db.query(Booking).filter_by(title="Zarząd").one()
    assert b.status == "pending_approval"
    assert [a.status for a in b.attendees].count("waiting") == 2  # 5 osób, pojemność 3
    client.get("/logout")

    post(client, "/login", {"email": "opiekun@firma-a.pl", "password": "opiekun12345"}, "/login")
    assert "Zarząd" in client.get("/approvals").text
    r = post(client, f"/approvals/{b.id}/approve", {}, "/approvals")
    db.expire_all()
    b = db.get(Booking, b.id)
    assert b.status == "confirmed" and all(a.status != "waiting" for a in b.attendees)

    # tablet z wymaganym PIN-em
    t = db.query(Tenant).filter_by(slug="firma-a").one()
    t.require_auth_on_tablet = True
    db.commit()
    assert client.post("/api/device/action", headers=H, json={"action": "book_now", "minutes": 15}).status_code == 401
    assert client.post("/api/device/action", headers=H,
                       json={"action": "book_now", "minutes": 15, "pin": "9999"}).status_code == 403
    st = client.post("/api/device/action", headers=H, json={"action": "book_now", "minutes": 15, "pin": "1234"}).json()
    assert st["current"]["organizer"] == "Opiekun"
    db.close()


class FakeProvider(Provider):
    external = True

    def __init__(self, events):
        self.events = events
        self.responses = []

    def list_events(self, room, start, end):
        return self.events

    def respond(self, room, event_id, response, comment=""):
        self.responses.append((event_id, response))


def test_sync_capacity(client):
    db = SessionLocal()
    t = db.query(Tenant).filter_by(slug="firma-a").one()
    room = db.query(Room).filter_by(name="Sala Mała").one()
    now = utcnow()
    small = ExtEvent(id="ev1", title="Mały", start=now + timedelta(days=1), end=now + timedelta(days=1, hours=1),
                     organizer_email="o@x.pl", attendees=[ExtAttendee("o@x.pl", is_organizer=True),
                                                          ExtAttendee("p@x.pl")])
    big = ExtEvent(id="ev2", title="Duży", start=now + timedelta(days=2), end=now + timedelta(days=2, hours=1),
                   organizer_email="o@x.pl",
                   attendees=[ExtAttendee(f"u{i}@x.pl") for i in range(5)] + [ExtAttendee("room2@x.pl", is_resource=True)])
    p = FakeProvider([small, big])
    svc.sync_room(db, t, room, p)
    assert ("ev1", "accept") in p.responses and ("ev2", "tentativelyAccept") in p.responses
    assert db.query(Booking).filter_by(external_id="ev2").one().status == "pending_approval"
    # zniknięcie wydarzenia → anulowanie
    p.events = [big]
    svc.sync_room(db, t, room, p)
    assert db.query(Booking).filter_by(external_id="ev1").one().status == "cancelled"
    # odrzucenie przez opiekuna → sala odrzuca
    b = db.query(Booking).filter_by(external_id="ev2").one()
    import app.services.bookings as bk
    orig = bk.safe_provider
    bk.safe_provider = lambda tenant: (p, None)
    try:
        svc.reject(db, t, b, None, "za mało miejsc")
    finally:
        bk.safe_provider = orig
    assert ("ev2", "decline") in p.responses and b.status == "rejected"
    db.close()


def test_pages_render(client):
    post(client, "/login", {"email": "admin@firma-a.pl", "password": "adminhaslo123"}, "/login")
    for url in ["/", "/rooms", "/devices", "/bookings", "/approvals", "/users", "/settings", "/audit"]:
        r = client.get(url)
        assert r.status_code == 200, url
    db = SessionLocal()
    from app.models import Device
    d = db.query(Device).first()
    assert client.get(f"/devices/{d.id}").status_code == 200
    db.close()


def test_websocket_refresh(client):
    from app.models import Device
    from app.security import new_token, token_hash
    db = SessionLocal()
    d = db.query(Device).first()
    tok = new_token()
    d.token_hash = token_hash(tok)
    t = db.query(Tenant).filter_by(slug="firma-a").one()
    t.require_auth_on_tablet = False
    db.commit()
    db.close()
    with client.websocket_connect(f"/api/device/ws?token={tok}") as ws:
        ws.send_text("ping")
        assert ws.receive_json()["type"] == "pong"
        st = client.get("/api/device/state", headers={"Authorization": f"Bearer {tok}"}).json()
        if st["current"]:
            client.post("/api/device/action", headers={"Authorization": f"Bearer {tok}"},
                        json={"action": "end", "booking_id": st["current"]["id"]})
        else:
            client.post("/api/device/action", headers={"Authorization": f"Bearer {tok}"},
                        json={"action": "book_now", "minutes": 15})
        assert ws.receive_json()["type"] == "refresh"


def test_m365_consent_callback(client):
    from app.panel.routes import _consent_signer
    db = SessionLocal()
    t = db.query(Tenant).filter_by(slug="firma-a").one()
    state = _consent_signer.dumps({"tid": t.id, "uid": 1})
    tid = "11111111-2222-3333-4444-555555555555"
    assert client.get(f"/m365/callback?tenant={tid}&admin_consent=True&state=zly").status_code == 400
    r = client.get(f"/m365/callback?tenant={tid}&admin_consent=True&state={state}", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    t = db.get(Tenant, t.id)
    assert t.m365_consented and t.m365_tenant_id == tid
    db.close()
