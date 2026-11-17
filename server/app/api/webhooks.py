"""Powiadomienia Microsoft Graph o zmianach w kalendarzach sal."""
import hmac
import logging

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select

from .. import config
from ..db import SessionLocal
from ..models import Room
from ..worker import request_sync

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
log = logging.getLogger("webhooks")


@router.post("/graph")
async def graph(request: Request):
    # Walidacja przy tworzeniu subskrypcji: odesłać validationToken jako text/plain.
    token = request.query_params.get("validationToken")
    if token is not None:
        return PlainTextResponse(token)
    try:
        payload = await request.json()
    except ValueError:
        return Response(status_code=400)
    db = SessionLocal()
    try:
        for n in payload.get("value", []):
            if not hmac.compare_digest(str(n.get("clientState", "")), config.M365_WEBHOOK_SECRET):
                log.warning("Odrzucono powiadomienie Graph z nieprawidłowym clientState")
                continue
            room = db.scalars(select(Room).where(Room.m365_subscription_id == n.get("subscriptionId"))).first()
            if room is not None:
                request_sync(room.id)
    finally:
        db.close()
    return Response(status_code=202)
