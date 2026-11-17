"""Sesja panelu, role, kontekst klienta (tenant), CSRF."""
import hmac
import secrets

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import Tenant, User


class Redirect(Exception):
    def __init__(self, url: str) -> None:
        self.url = url


class Ctx:
    def __init__(self, user: User, tenant: Tenant | None) -> None:
        self.user = user
        self.tenant = tenant

    @property
    def is_super(self) -> bool:
        return self.user.role == "superadmin"

    @property
    def is_admin(self) -> bool:
        return self.user.role in ("superadmin", "admin")

    @property
    def tid(self) -> int:
        if self.tenant is None:
            raise Redirect("/tenants")
        return self.tenant.id


def host_tenant_slug(request: Request) -> str | None:
    """Subdomena klienta: <slug>.BASE_DOMAIN (admin.* = panel ePartners)."""
    if not config.BASE_DOMAIN:
        return None
    host = (request.headers.get("host") or "").split(":")[0].lower()
    suffix = "." + config.BASE_DOMAIN.lower()
    if host.endswith(suffix):
        slug = host[: -len(suffix)]
        return None if slug in ("admin", "www", "") else slug
    return None


def get_ctx(request: Request, db: Session = Depends(get_db)) -> Ctx:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is None or not user.active:
        raise Redirect("/login")
    if user.role == "superadmin":
        tid = request.session.get("tenant_id")
        tenant = db.get(Tenant, tid) if tid else None
    else:
        tenant = db.get(Tenant, user.tenant_id)
        if tenant is None or not tenant.active:
            request.session.clear()
            raise Redirect("/login?e=inactive")
    return Ctx(user, tenant)


def require_admin(ctx: Ctx = Depends(get_ctx)) -> Ctx:
    if not ctx.is_admin:
        raise HTTPException(403, "Brak uprawnień")
    return ctx


def require_super(ctx: Ctx = Depends(get_ctx)) -> Ctx:
    if not ctx.is_super:
        raise HTTPException(403, "Brak uprawnień")
    return ctx


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


async def form(request: Request) -> dict:
    """Formularz POST z weryfikacją CSRF."""
    data = await request.form()
    if not hmac.compare_digest(str(data.get("_csrf", "")), request.session.get("csrf", "")):
        raise HTTPException(400, "Sesja formularza wygasła – odśwież stronę.")
    return {k: v for k, v in data.multi_items()} | {"_lists": {k: data.getlist(k) for k in data.keys()}}
