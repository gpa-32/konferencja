"""Hasła, PIN-y i tokeny urządzeń."""
import base64
import hashlib
import hmac
import secrets

_N, _R, _P = 2 ** 14, 8, 1


def hash_secret(value: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(value.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_secret(value: str, stored: str | None) -> bool:
    if not stored or not value:
        return False
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except ValueError:
        return False
    dk = hashlib.scrypt(value.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=len(expected))
    return hmac.compare_digest(dk, expected)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    """Tokeny urządzeń przechowujemy jako SHA-256 (wysoka entropia, szybkie wyszukiwanie)."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_enrollment_code() -> str:
    # Krótki kod, który da się też przepisać ręcznie: 4 grupy po 4 znaki bez mylących liter.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))
