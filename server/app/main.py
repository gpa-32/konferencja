"""Serwer systemu rezerwacji sal konferencyjnych."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from . import config, worker
from .api import device, webhooks
from .db import Base, SessionLocal, engine
from .models import User
from .panel import routes as panel
from .panel.auth import Redirect
from .security import hash_secret
from .services.hub import hub

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


def init_db() -> None:
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        has_super = db.scalars(select(User).where(User.role == "superadmin")).first()
        if not has_super and config.SUPERADMIN_PASSWORD:
            db.add(User(email=config.SUPERADMIN_EMAIL.lower(), name="ePartners", role="superadmin",
                        password_hash=hash_secret(config.SUPERADMIN_PASSWORD)))
            db.commit()
            log.info("Utworzono superadmina %s", config.SUPERADMIN_EMAIL)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    init_db()
    hub.loop = asyncio.get_running_loop()
    task = None
    if not config.DISABLE_WORKER:
        task = asyncio.create_task(worker.run())
    yield
    if task:
        task.cancel()


app = FastAPI(title="Sale konferencyjne – ePartners", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, session_cookie="sale_session",
                   same_site="lax", https_only=config.PUBLIC_URL.startswith("https://"), max_age=12 * 3600)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
app.include_router(device.router)
app.include_router(webhooks.router)
app.include_router(panel.router)


@app.exception_handler(Redirect)
async def _redirect(request: Request, exc: Redirect):
    return RedirectResponse(exc.url, status_code=303)


@app.get("/api/tls-check")
def tls_check(domain: str = ""):
    """Caddy on-demand TLS: certyfikat tylko dla domeny głównej, admin.* i subdomen istniejących klientów."""
    from fastapi import HTTPException
    from .models import Tenant
    host = domain.lower()
    base = config.BASE_DOMAIN.lower()
    public = config.PUBLIC_URL.split("://", 1)[-1].split("/")[0].split(":")[0].lower()
    if host in (base, public, f"admin.{base}"):
        return {"ok": True}
    if base and host.endswith("." + base):
        db = SessionLocal()
        try:
            if db.scalars(select(Tenant).where(Tenant.slug == host[: -len(base) - 1])).first():
                return {"ok": True}
        finally:
            db.close()
    raise HTTPException(404)


@app.get("/healthz")
def healthz():
    return {"ok": True}
