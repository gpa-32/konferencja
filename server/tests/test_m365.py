import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("M365_CLIENT_ID", "cid")
os.environ.setdefault("M365_CLIENT_SECRET", "sec")

import httpx  # noqa: E402

from app import config  # noqa: E402
from app.models import Room, Tenant  # noqa: E402
from app.services.calendar import M365Provider  # noqa: E402


def test_m365_list_and_respond():
    config.M365_CLIENT_ID, config.M365_CLIENT_SECRET = "cid", "sec"
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, str(req.url)))
        if "oauth2" in str(req.url):
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        if "calendarView" in str(req.url):
            assert req.headers["Authorization"] == "Bearer T"
            return httpx.Response(200, json={"value": [{
                "id": "AAA", "subject": "Spotkanie", "isCancelled": False, "sensitivity": "normal",
                "start": {"dateTime": "2026-11-18T09:00:00.0000000", "timeZone": "UTC"},
                "end": {"dateTime": "2026-11-18T10:00:00.0000000", "timeZone": "UTC"},
                "organizer": {"emailAddress": {"name": "Jan", "address": "jan@f.pl"}},
                "responseStatus": {"response": "notResponded"},
                "attendees": [
                    {"type": "required", "status": {"response": "accepted"}, "emailAddress": {"address": "ala@f.pl"}},
                    {"type": "resource", "status": {"response": "none"}, "emailAddress": {"address": "sala@f.pl"}},
                ]}]})
        if req.url.path.endswith("/accept"):
            assert json.loads(req.content)["sendResponse"] is True
            return httpx.Response(202)
        return httpx.Response(404)

    t = Tenant(slug="x", name="x", m365_tenant_id="tid-1")
    room = Room(name="S", capacity=4, calendar_id="sala@f.pl")
    p = M365Provider(t, client=httpx.Client(transport=httpx.MockTransport(handler)))
    from datetime import datetime
    evs = p.list_events(room, datetime(2026, 11, 18), datetime(2026, 11, 19))
    assert len(evs) == 1
    ev = evs[0]
    assert ev.room_response == "none" and ev.start.hour == 9
    assert [a.email for a in ev.attendees] == ["jan@f.pl", "ala@f.pl"]  # organizator + ala, bez sali
    p.respond(room, "AAA", "accept")
    assert any(u.endswith("/users/sala@f.pl/events/AAA/accept") for _, u in calls)
