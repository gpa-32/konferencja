"""Dostawcy kalendarzy: lokalny (tylko baza), Microsoft 365 (Graph). Google – etap 2."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from .. import config
from ..models import Room, Tenant, utcnow

log = logging.getLogger("calendar")
GRAPH = "https://graph.microsoft.com/v1.0"


class CalendarError(Exception):
    pass


@dataclass
class ExtAttendee:
    email: str
    name: str = ""
    # accepted | tentative | declined | invited
    status: str = "invited"
    is_resource: bool = False
    is_organizer: bool = False


@dataclass
class ExtEvent:
    id: str
    title: str
    start: datetime
    end: datetime
    organizer_name: str = ""
    organizer_email: str = ""
    attendees: list[ExtAttendee] = field(default_factory=list)
    is_private: bool = False
    cancelled: bool = False
    # Odpowiedź sali: none | accepted | tentative | declined | organizer
    room_response: str = "none"


class Provider:
    """Interfejs dostawcy. Implementacja lokalna – wszystko w bazie, nic nie wychodzi na zewnątrz."""
    external = False

    def list_events(self, room: Room, start: datetime, end: datetime) -> list[ExtEvent]:
        return []

    def create_event(self, room: Room, title: str, start: datetime, end: datetime,
                     attendees: list[tuple[str, str]], body: str = "") -> str | None:
        return None

    def update_times(self, room: Room, event_id: str, start: datetime, end: datetime) -> None:
        return None

    def set_attendees(self, room: Room, event_id: str, attendees: list[tuple[str, str]]) -> None:
        return None

    def cancel(self, room: Room, event_id: str, comment: str = "") -> None:
        return None

    def respond(self, room: Room, event_id: str, response: str, comment: str = "") -> None:
        """response: accept | tentativelyAccept | decline"""
        return None


class LocalProvider(Provider):
    pass


def _graph_dt(value: dict) -> datetime:
    # Graph zwraca "2026-11-17T10:00:00.0000000" w strefie z nagłówka Prefer (UTC).
    raw = value["dateTime"].split(".")[0]
    return datetime.fromisoformat(raw)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


_RESP_MAP = {
    "accepted": "accepted",
    "tentativelyAccepted": "tentative",
    "declined": "declined",
    "organizer": "accepted",
    "none": "invited",
    "notResponded": "invited",
}


class M365Provider(Provider):
    """Microsoft Graph, uprawnienia aplikacyjne (client credentials) w dzierżawie klienta.

    Wymaga zgody administratora klienta na aplikację ePartners oraz ograniczenia dostępu
    tylko do skrzynek sal (RBAC for Applications / Application Access Policy).
    """
    external = True
    _tokens: dict[str, tuple[str, float]] = {}
    _lock = threading.Lock()

    def __init__(self, tenant: Tenant, client: httpx.Client | None = None) -> None:
        if not tenant.m365_tenant_id:
            raise CalendarError("Brak identyfikatora dzierżawy M365 – wykonaj zgodę administratora w ustawieniach.")
        if not (config.M365_CLIENT_ID and config.M365_CLIENT_SECRET):
            raise CalendarError("Serwer nie ma skonfigurowanych M365_CLIENT_ID / M365_CLIENT_SECRET.")
        self.tid = tenant.m365_tenant_id
        self.http = client or httpx.Client(timeout=20)

    # --- token ---
    def _token(self) -> str:
        with self._lock:
            cached = self._tokens.get(self.tid)
            if cached and cached[1] > time.time() + 60:
                return cached[0]
            r = self.http.post(
                f"https://login.microsoftonline.com/{self.tid}/oauth2/v2.0/token",
                data={
                    "client_id": config.M365_CLIENT_ID,
                    "client_secret": config.M365_CLIENT_SECRET,
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                },
            )
            if r.status_code != 200:
                raise CalendarError(f"Token M365: {r.status_code} {r.text[:300]}")
            data = r.json()
            self._tokens[self.tid] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
            return data["access_token"]

    def _req(self, method: str, url: str, **kw) -> httpx.Response:
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._token()}"
        headers.setdefault("Prefer", 'outlook.timezone="UTC"')
        if not url.startswith("http"):
            url = GRAPH + url
        r = self.http.request(method, url, headers=headers, **kw)
        if r.status_code == 429:
            time.sleep(min(int(r.headers.get("Retry-After", "2")), 10))
            r = self.http.request(method, url, headers=headers, **kw)
        if r.status_code >= 400:
            raise CalendarError(f"Graph {method} {url}: {r.status_code} {r.text[:300]}")
        return r

    @staticmethod
    def _mailbox(room: Room) -> str:
        if not room.calendar_id:
            raise CalendarError(f"Sala {room.name} nie ma przypisanej skrzynki M365.")
        return room.calendar_id

    # --- odczyt ---
    def list_events(self, room: Room, start: datetime, end: datetime) -> list[ExtEvent]:
        mb = self._mailbox(room)
        url = (f"/users/{mb}/calendarView?startDateTime={_iso(start)}Z&endDateTime={_iso(end)}Z"
               "&$top=100&$select=id,subject,organizer,start,end,attendees,isCancelled,sensitivity,responseStatus")
        events: list[ExtEvent] = []
        while url:
            data = self._req("GET", url).json()
            for ev in data.get("value", []):
                events.append(self._parse(ev, mb))
            url = data.get("@odata.nextLink")
        return events

    @staticmethod
    def _parse(ev: dict, mailbox: str) -> ExtEvent:
        org = (ev.get("organizer") or {}).get("emailAddress") or {}
        org_email = (org.get("address") or "").lower()
        atts = []
        for a in ev.get("attendees") or []:
            ea = a.get("emailAddress") or {}
            email = (ea.get("address") or "").lower()
            if email == mailbox.lower():
                continue  # sama sala
            atts.append(ExtAttendee(
                email=email,
                name=ea.get("name") or "",
                status=_RESP_MAP.get((a.get("status") or {}).get("response", "none"), "invited"),
                is_resource=a.get("type") == "resource",
            ))
        if org_email and org_email != mailbox.lower() and not any(a.email == org_email for a in atts):
            atts.insert(0, ExtAttendee(email=org_email, name=org.get("name") or "", status="accepted",
                                       is_organizer=True))
        else:
            for a in atts:
                if a.email == org_email:
                    a.is_organizer = True
        room_resp = (ev.get("responseStatus") or {}).get("response", "none")
        return ExtEvent(
            id=ev["id"],
            title=ev.get("subject") or "",
            start=_graph_dt(ev["start"]),
            end=_graph_dt(ev["end"]),
            organizer_name=org.get("name") or "",
            organizer_email=org_email,
            attendees=atts,
            is_private=ev.get("sensitivity") in ("private", "confidential"),
            cancelled=bool(ev.get("isCancelled")),
            room_response={"tentativelyAccepted": "tentative", "notResponded": "none"}.get(room_resp, room_resp),
        )

    # --- zapis ---
    def create_event(self, room, title, start, end, attendees, body=""):
        mb = self._mailbox(room)
        payload = {
            "subject": title,
            "start": {"dateTime": _iso(start), "timeZone": "UTC"},
            "end": {"dateTime": _iso(end), "timeZone": "UTC"},
            "attendees": [{"emailAddress": {"address": e, "name": n or e}, "type": "required"}
                          for e, n in attendees if e],
            "body": {"contentType": "text", "content": body},
        }
        return self._req("POST", f"/users/{mb}/events", json=payload).json()["id"]

    def update_times(self, room, event_id, start, end):
        mb = self._mailbox(room)
        self._req("PATCH", f"/users/{mb}/events/{event_id}", json={
            "start": {"dateTime": _iso(start), "timeZone": "UTC"},
            "end": {"dateTime": _iso(end), "timeZone": "UTC"},
        })

    def set_attendees(self, room, event_id, attendees):
        mb = self._mailbox(room)
        self._req("PATCH", f"/users/{mb}/events/{event_id}", json={
            "attendees": [{"emailAddress": {"address": e, "name": n or e}, "type": "required"}
                          for e, n in attendees if e],
        })

    def cancel(self, room, event_id, comment=""):
        mb = self._mailbox(room)
        self._req("POST", f"/users/{mb}/events/{event_id}/cancel", json={"comment": comment})

    def respond(self, room, event_id, response, comment=""):
        mb = self._mailbox(room)
        self._req("POST", f"/users/{mb}/events/{event_id}/{response}",
                  json={"comment": comment, "sendResponse": True})

    # --- subskrypcje (webhook) ---
    def subscribe(self, room: Room) -> tuple[str, datetime]:
        mb = self._mailbox(room)
        # Maksymalny czas życia subskrypcji na zdarzenia Outlooka to ok. 3 dni.
        expires = utcnow() + timedelta(days=2, hours=12)
        data = self._req("POST", "/subscriptions", json={
            "changeType": "created,updated,deleted",
            "notificationUrl": f"{config.PUBLIC_URL}/api/webhooks/graph",
            "resource": f"/users/{mb}/events",
            "expirationDateTime": _iso(expires) + "Z",
            "clientState": config.M365_WEBHOOK_SECRET,
        }).json()
        return data["id"], expires

    def renew(self, subscription_id: str) -> datetime:
        expires = utcnow() + timedelta(days=2, hours=12)
        self._req("PATCH", f"/subscriptions/{subscription_id}",
                  json={"expirationDateTime": _iso(expires) + "Z"})
        return expires


class GoogleProvider(Provider):
    """Etap 2: Google Workspace (konto usługi z delegacją w domenie, kalendarze zasobów)."""

    def __init__(self, tenant: Tenant) -> None:
        raise CalendarError("Integracja Google Workspace będzie dostępna w etapie 2.")


def get_provider(tenant: Tenant) -> Provider:
    if tenant.calendar_provider == "m365":
        return M365Provider(tenant)
    if tenant.calendar_provider == "google":
        return GoogleProvider(tenant)
    return LocalProvider()


def safe_provider(tenant: Tenant) -> tuple[Provider, str | None]:
    """Dostawca albo lokalny z komunikatem błędu (np. brak zgody M365)."""
    try:
        return get_provider(tenant), None
    except CalendarError as exc:
        return LocalProvider(), str(exc)
