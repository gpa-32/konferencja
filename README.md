# Sale konferencyjne – ePartners

System rezerwacji sal konferencyjnych dla wielu klientów na jednym VPS.
Tablety PoE przy salach (Android) pokazują zajętość sali i pozwalają ją korygować,
a serwer synchronizuje się z kalendarzami Microsoft 365 (Google Workspace – etap 2).

```
 Outlook / M365  ⇄  Graph API + webhooki  ⇄  serwer (VPS, Docker)  ⇄  WebSocket  ⇄  tablety Android
                                                 │
                                           panel WWW (klienci, sale, tablety, zatwierdzenia)
```

## Co jest w repozytorium

| Katalog | Zawartość |
|---|---|
| `server/` | FastAPI + PostgreSQL: API tabletów, panel WWW, integracja M365, worker synchronizacji |
| `android/` | Aplikacja tabletu „Sala” (Kotlin): kiosk, rejestracja kodem QR, ekran sali, NFC, LED, OTA |
| `deploy/` | docker-compose (Postgres, aplikacja, Caddy z automatycznym TLS, opcjonalnie RustDesk) |
| `.github/workflows/` | Testy serwera, budowanie APK i publikacja w GitHub Releases |

## Funkcje (etap 1)

- **Wielu klientów** – własni administratorzy, limity sal i tabletów; wspólna domena
  (`konferencja.nettrade.pl`) albo subdomena dla każdego klienta; superadmin ePartners zarządza klientami.
- **Sale** – pojemność, skrzynka sali w M365, opiekun i zastępca, opcjonalny check-in.
- **Tablety** – dodawanie kodem QR (jednorazowy, ważny 24 h); zdalnie z panelu: przypisanie sali,
  jasność, wygaszanie ekranu po godzinach, zrzut ekranu, restart aplikacji/tabletu, test LED,
  aktualizacja OTA, wyjście z kiosku; status online/offline, IP, bateria.
- **Korekta zajętości z tabletu** – zarezerwuj teraz, przedłuż, zakończ wcześniej, potwierdź obecność
  (brak check-in → sala zwalniana automatycznie). Opcjonalnie wymagany PIN lub karta NFC.
- **Pojemność i zatwierdzanie**
  - rezerwacja z panelu ponad pojemność: zaproszenia do osób w limicie wychodzą od razu, reszta czeka
    na liście oczekujących; opiekun zatwierdza (zaproszenia wychodzą) albo odrzuca (spotkanie bez dodatkowych osób),
  - rezerwacja z Outlooka ponad pojemność: sala odpowiada „wstępnie”, organizator i opiekun dostają e-mail;
    opiekun zatwierdza (sala akceptuje) albo odrzuca (sala odrzuca zaproszenie),
  - brak decyzji: eskalacja do zastępcy po X godz., 15 min przed spotkaniem decyzja domyślna (ustawienie klienta).
- **Dziennik zdarzeń** – kto, co i skąd zmienił.

## Uruchomienie na VPS

Wymagania: VPS z Debianem/Ubuntu, rekord A domeny (np. `konferencja.nettrade.pl`) → IP VPS.

```bash
git clone https://github.com/gpa-32/konferencja.git
cd konferencja/deploy
sudo ./install.sh        # pyta o domenę i hasło superadmina; kolejne uruchomienie = aktualizacja
```

Skrypt instaluje Dockera (jeśli go nie ma), tworzy `deploy/.env` z losowymi hasłami i uruchamia
PostgreSQL + aplikację. Jeśli porty 80/443 są wolne – uruchamia własny Caddy z certyfikatem Let's Encrypt.
Jeśli zajmuje je już serwer www innego systemu na tym VPS (signage, Domofon SIP) – aplikacja działa
na `127.0.0.1:8010`, a domena jest dopisywana do istniejącego Caddy (w systemie lub w kontenerze) albo nginx.

Panel: `https://konferencja.nettrade.pl` – logowanie superadmina z instalacji
(losowe hasło zapisuje się w `deploy/admin-password.txt`). Dane M365 i SMTP uzupełnia się w `deploy/.env`,
potem `sudo ./install.sh` ponownie.

**Subdomeny klientów (opcjonalnie):** przy rekordzie wildcard `*.sale.firma.pl` ustaw `BASE_DOMAIN` w `.env`
i odkomentuj blok wildcard w `deploy/Caddyfile` – każdy klient loguje się wtedy pod `<klient>.BASE_DOMAIN`.
Bez tego wszyscy klienci logują się pod jedną domeną, a konto użytkownika wyznacza klienta.

Zdalny podgląd tabletów: `docker compose --profile rustdesk up -d` (porty 21115–21117).

## Microsoft 365

### Jednorazowo – aplikacja ePartners w Entra ID

1. Entra ID → Rejestracje aplikacji → Nowa rejestracja, **konta w dowolnym katalogu organizacyjnym (multi-tenant)**.
2. Identyfikator URI przekierowania (Web): `PUBLIC_URL/m365/callback`, np. `https://konferencja.nettrade.pl/m365/callback`
   (jeden adres dla wszystkich klientów – po zgodzie użytkownik wraca na subdomenę swojego klienta).
3. Uprawnienia API → Microsoft Graph → **uprawnienia aplikacji** → `Calendars.ReadWrite`.
4. Certyfikaty i wpisy tajne → nowy wpis tajny. `M365_CLIENT_ID` i `M365_CLIENT_SECRET` wpisz do `.env`.

### U każdego klienta

1. Administrator klienta w panelu: **Ustawienia → Połącz z Microsoft 365** (zgoda administratora Microsoft 365).
2. **Ogranicz dostęp aplikacji tylko do sal** (Exchange Online PowerShell):
   ```powershell
   New-DistributionGroup -Name "Sale - system rezerwacji" -Type Security -Members sala1@firma.pl,sala2@firma.pl
   New-ApplicationAccessPolicy -AppId <M365_CLIENT_ID> -PolicyScopeGroupId "Sale - system rezerwacji" `
       -AccessRight RestrictAccess -Description "System sal ePartners – tylko skrzynki sal"
   ```
3. **Wyłącz automatyczne akceptowanie w skrzynkach sal** – decyzję (w tym kontrolę pojemności) podejmuje system:
   ```powershell
   Set-CalendarProcessing -Identity sala1@firma.pl -AutomateProcessing AutoUpdate
   ```
4. W panelu przy każdej sali wpisz adres skrzynki sali.

Zmiany w kalendarzach przychodzą webhookami Graph (subskrypcje odnawiane automatycznie),
dodatkowo co `SYNC_INTERVAL` sekund jest synchronizacja kontrolna.

## Tablety

1. Pobierz APK z [Releases](https://github.com/gpa-32/konferencja/releases) (`latest`).
2. Instalacja:
   - **zalecane – tryb kiosku (device owner)** na tablecie po resecie fabrycznym, bez konta Google:
     ```bash
     adb install sala-*.apk
     adb shell dpm set-device-owner pl.epartners.sala/.AdminReceiver
     ```
     Aplikacja staje się ekranem głównym, blokuje wyjście, aktualizuje się bez pytania, może zrestartować tablet.
   - **tablet zrootowany** – kiosk przez ekran główny, aktualizacje i restart przez `su`.
   - zwykła instalacja – działa, ale aktualizacja wymaga potwierdzenia na tablecie.
3. Panel → Tablety → **Generuj kod QR** → w aplikacji „Skanuj kod QR” (albo wpisz serwer i kod).

**Listwa LED** – w panelu przy tablecie wpisz ścieżki sysfs (wymaga roota), np.
`{"red": "/sys/class/leds/red/brightness", "green": "/sys/class/leds/green/brightness", "max": 255}`.
Kolory: zielony – wolna, czerwony – zajęta, żółty – czeka na zatwierdzenie.

**NFC** – w Ustawieniach klienta włącz „wymagaj PIN lub karty NFC”. Przyłożenie nieznanej karty zapisuje jej UID
w panelu (Użytkownicy → Karty NFC), skąd można ją przypisać osobie. Tablety bez NFC pytają o PIN.

**Aktualizacje OTA** – każdy push do `android/` buduje APK w GitHub Actions (release `latest`).
Superadmin wgrywa APK w panelu (Klienci → Aplikacja tabletu); tablety sprawdzają aktualizację co 6 h
lub od razu poleceniem „Sprawdź aktualizację”.

Klucz podpisu APK jest w `android/keystore/` (ten sam klucz jest konieczny, żeby aktualizacje instalowały się
na istniejącą aplikację). Przy publicznym repozytorium zalecane przeniesienie go do sekretów GitHub
(`SALA_KEYSTORE`, `SALA_KEYSTORE_PASSWORD`, `SALA_KEY_ALIAS`, `SALA_KEY_PASSWORD`).

**Zdalny podgląd** – klient RustDesk na tablecie skonfigurowany na serwer ePartners (profil `rustdesk` w compose).

## Rozwój lokalny

```bash
cd server
pip install -r requirements.txt pytest
SUPERADMIN_PASSWORD=haslo123 uvicorn app.main:app --reload   # SQLite, http://localhost:8000
python -m pytest -q tests
```

## Ograniczenia i kolejne etapy

- Rezerwacji z Outlooka nie da się wstrzymać na liście oczekujących – zaproszenia wysyła Outlook, system decyduje
  tylko o potwierdzeniu sali. Pełna kontrola wymaga dodatku do Outlooka/Google (etap 4).
- Listy dystrybucyjne liczone są jako jedna osoba – rozwijanie grup (Graph `/groups/{id}/members`) w etapie 2.
- Google Workspace – etap 2. Izolacja klientów w bazie przez `tenant_id` w każdym zapytaniu; Row Level Security
  PostgreSQL – etap 3.
- Model danych tworzony przy starcie (`create_all`); migracje Alembic przed pierwszą zmianą schematu na produkcji.
