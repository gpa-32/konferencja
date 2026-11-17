"""Konfiguracja z zmiennych środowiskowych."""
import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DATABASE_URL = _env("DATABASE_URL", "sqlite:///./konferencja.db")
SECRET_KEY = _env("SECRET_KEY", "dev-secret-change-me")
# Publiczny adres serwera (https://sale.epartners.pl) – używany w kodach QR, webhookach Graph i linkach zgody M365.
PUBLIC_URL = _env("PUBLIC_URL", "http://localhost:8000").rstrip("/")
# Domena bazowa dla subdomen klientów: <slug>.BASE_DOMAIN
BASE_DOMAIN = _env("BASE_DOMAIN", "")

# Pierwszy superadmin ePartners tworzony przy starcie, jeśli nie istnieje.
SUPERADMIN_EMAIL = _env("SUPERADMIN_EMAIL", "admin@epartners.pl")
SUPERADMIN_PASSWORD = _env("SUPERADMIN_PASSWORD", "")

# Aplikacja multi-tenant w Entra ID ePartners (Microsoft 365).
M365_CLIENT_ID = _env("M365_CLIENT_ID")
M365_CLIENT_SECRET = _env("M365_CLIENT_SECRET")
# Sekret weryfikujący powiadomienia Graph (clientState).
M365_WEBHOOK_SECRET = _env("M365_WEBHOOK_SECRET", SECRET_KEY[:32])

# Co ile sekund worker synchronizuje kalendarze i sprząta rezerwacje.
SYNC_INTERVAL = int(_env("SYNC_INTERVAL", "60"))
# Ile dni do przodu synchronizować kalendarze sal.
SYNC_DAYS_AHEAD = int(_env("SYNC_DAYS_AHEAD", "14"))
# Wyłączenie workera w tle (testy).
DISABLE_WORKER = _env("DISABLE_WORKER", "0") == "1"
# Tablet uznawany za offline po tylu sekundach bez kontaktu.
DEVICE_OFFLINE_AFTER = int(_env("DEVICE_OFFLINE_AFTER", "120"))

# Katalog na pliki (zrzuty ekranu, APK do aktualizacji OTA).
DATA_DIR = _env("DATA_DIR", "./data")
