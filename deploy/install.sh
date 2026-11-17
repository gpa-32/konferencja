#!/usr/bin/env bash
# Instalacja / aktualizacja serwera sal konferencyjnych na VPS.
#   sudo ./install.sh                 – pierwsza instalacja (pyta o domenę i hasło)
#   sudo ./install.sh                 – kolejne uruchomienie = aktualizacja (git pull + rebuild)
# Gdy porty 80/443 zajmuje istniejący serwer www (Caddy/nginx, także w kontenerze),
# aplikacja działa na 127.0.0.1:APP_PORT, a domena jest dopisywana do tamtego serwera.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Uruchom jako root: sudo ./install.sh"; exit 1; }

# --- Docker -------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  say "Instalacja Dockera"
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || { echo "Brak 'docker compose' (plugin v2)."; exit 1; }

# --- Aktualizacja kodu -------------------------------------------------------
if [ -d ../.git ]; then
  say "Pobieranie najnowszej wersji z GitHub"
  git -C .. pull --ff-only || warn "git pull nie powiódł się – instaluję bieżącą wersję"
fi

# --- Konfiguracja (.env) -----------------------------------------------------
rand() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "${1:-32}"; }
if [ ! -f .env ]; then
  say "Pierwsza konfiguracja"
  read -rp "Domena [konferencja.nettrade.pl]: " DOMAIN; DOMAIN=${DOMAIN:-konferencja.nettrade.pl}
  read -rp "E-mail superadmina (logowanie do panelu) [gpa@epartners.pl]: " SA; SA=${SA:-gpa@epartners.pl}
  read -rsp "Hasło superadmina (puste = losowe): " SAP; echo
  if [ -z "$SAP" ]; then SAP=$(rand 16); GEN_PASS=1; fi
  APP_PORT=8010
  while ss -ltn 2>/dev/null | grep -q ":$APP_PORT "; do APP_PORT=$((APP_PORT+1)); done
  cat > .env <<EOF
DOMAIN=$DOMAIN
PUBLIC_URL=https://$DOMAIN
BASE_DOMAIN=
ACME_EMAIL=$SA
APP_PORT=$APP_PORT
POSTGRES_PASSWORD=$(rand 32)
SECRET_KEY=$(rand 48)
SUPERADMIN_EMAIL=$SA
SUPERADMIN_PASSWORD=$SAP
M365_CLIENT_ID=
M365_CLIENT_SECRET=
M365_WEBHOOK_SECRET=$(rand 32)
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=
EOF
  chmod 600 .env
  if [ "${GEN_PASS:-0}" = 1 ]; then echo "$SAP" > admin-password.txt; chmod 600 admin-password.txt; fi
fi
set -a; . ./.env; set +a

# --- Kto obsługuje 80/443? -----------------------------------------------------
port_busy() { ss -ltn 2>/dev/null | grep -qE "[:.]$1 "; }
OWN_CADDY=0
if docker ps --format '{{.Names}}' | grep -qx 'konferencja-caddy-1'; then
  OWN_CADDY=1
elif ! port_busy 80 && ! port_busy 443; then
  OWN_CADDY=1
fi

say "Budowanie i uruchamianie kontenerów"
if [ "$OWN_CADDY" = 1 ]; then
  docker compose --profile caddy up -d --build
else
  docker compose up -d --build
fi

say "Czekam na start aplikacji"
for i in $(seq 1 40); do
  curl -fsS "http://127.0.0.1:$APP_PORT/healthz" >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS "http://127.0.0.1:$APP_PORT/healthz" >/dev/null || { docker compose logs --tail 50 app; exit 1; }

# --- Podpięcie domeny do istniejącego serwera www ------------------------------
SITE_CADDY="$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:$APP_PORT
}"

if [ "$OWN_CADDY" = 0 ]; then
  say "Porty 80/443 są zajęte – podpinam $DOMAIN do istniejącego serwera www"
  DONE=0
  # 1) Caddy zainstalowany w systemie
  if [ -f /etc/caddy/Caddyfile ] && systemctl is-active --quiet caddy 2>/dev/null; then
    if ! grep -qE "^\s*$DOMAIN\b" /etc/caddy/Caddyfile; then
      cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%s)"
      printf '\n%s\n' "$SITE_CADDY" >> /etc/caddy/Caddyfile
    fi
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && systemctl reload caddy && DONE=1
  fi
  # 2) Caddy w kontenerze (np. serwera signage lub Domofon SIP)
  if [ "$DONE" = 0 ]; then
    CC=$(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /caddy/ && $1 != "konferencja-caddy-1" {print $1; exit}')
    if [ -n "${CC:-}" ]; then
      SRC=$(docker inspect "$CC" --format '{{range .Mounts}}{{if eq .Destination "/etc/caddy/Caddyfile"}}{{.Source}}{{end}}{{end}}')
      NET=$(docker inspect "$CC" --format '{{.HostConfig.NetworkMode}}')
      if [ -n "$SRC" ] && [ -f "$SRC" ]; then
        if [ "$NET" = "host" ]; then
          UPSTREAM="127.0.0.1:$APP_PORT"
        else
          # Dołącz aplikację do sieci kontenera Caddy i kieruj ruch po nazwie kontenera.
          CNET=$(docker inspect "$CC" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
          docker network connect "$CNET" konferencja-app-1 2>/dev/null || true
          UPSTREAM="konferencja-app-1:8000"
        fi
        if grep -qE "^\s*$DOMAIN\b" "$SRC"; then
          # Wpis dla domeny już istnieje – użyj portu, na który kieruje (np. przygotowany wcześniej 8100).
          EXIST=$(awk -v d="$DOMAIN" '$1==d {f=1} f && /reverse_proxy/ {print $2; exit}' "$SRC")
          EPORT=${EXIST##*:}
          if [ "$NET" = "host" ] && [ -n "$EPORT" ] && [ "$EPORT" != "$APP_PORT" ]; then
            echo "Istniejący wpis $DOMAIN kieruje na port $EPORT – przestawiam aplikację na ten port."
            sed -i "s/^APP_PORT=.*/APP_PORT=$EPORT/" .env
            APP_PORT=$EPORT
            docker compose up -d
          fi
        else
          cp "$SRC" "$SRC.bak.$(date +%s)"
          printf '\n%s {\n\tencode gzip\n\treverse_proxy %s\n}\n' "$DOMAIN" "$UPSTREAM" >> "$SRC"
        fi
        docker exec "$CC" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile && DONE=1
        echo "Dopisano $DOMAIN do $SRC (kontener $CC)."
        # Po restarcie kontenera aplikacji połączenie sieciowe trzeba odtworzyć – zapamiętujemy je.
        [ "$NET" != "host" ] && echo "CADDY_NETWORK=$CNET" > .caddy-network
      fi
    fi
  fi
  # 3) nginx w systemie
  if [ "$DONE" = 0 ] && command -v nginx >/dev/null 2>&1 && [ -d /etc/nginx/sites-enabled ]; then
    cat > /etc/nginx/sites-available/konferencja.conf <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 50m;
    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600s;
    }
}
EOF
    ln -sf /etc/nginx/sites-available/konferencja.conf /etc/nginx/sites-enabled/konferencja.conf
    nginx -t && systemctl reload nginx && DONE=1
    if command -v certbot >/dev/null 2>&1; then
      certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$ACME_EMAIL" --redirect || warn "certbot nie wystawił certyfikatu"
    else
      warn "Brak certbot – zainstaluj: apt install certbot python3-certbot-nginx; certbot --nginx -d $DOMAIN"
    fi
  fi
  if [ "$DONE" = 0 ]; then
    warn "Nie rozpoznałem serwera www na portach 80/443. Dodaj ręcznie przekierowanie $DOMAIN → 127.0.0.1:$APP_PORT"
    ss -ltnp | grep -E ':(80|443) ' || true
  fi
fi

# Odtworzenie połączenia z siecią zewnętrznego Caddy po przebudowie kontenera.
if [ -f .caddy-network ]; then
  . ./.caddy-network
  docker network connect "$CADDY_NETWORK" konferencja-app-1 2>/dev/null || true
fi

say "Gotowe"
echo "Panel:  https://$DOMAIN  (login: $SUPERADMIN_EMAIL)"
[ -f admin-password.txt ] && echo "Hasło:  $(cat admin-password.txt)  (zapisane w deploy/admin-password.txt)"
echo "Logi:   cd $DIR && docker compose logs -f app"
