#!/usr/bin/env python3
"""Parkly auto-booker.

Parkly legger ut plassene kl. 13:00 dagen før. Scriptet starter litt før,
venter til frigivelsen og prøver å booke morgendagen om og om igjen til
bookingen er bekreftet (eller fristen går ut).

Miljøvariabler:
  PARKLY_USERNAME       brukernavn/e-post til Parkly  } scriptet logger inn selv
  PARKLY_PASSWORD       passord                       } og henter ferskt token
  PARKLY_WEB_URL        (valgfritt, standard app.parkly.no)
  PARKLY_TOKEN          (valgfritt) fast token i stedet for innlogging, varer ca. 1 time
  PARKLY_ASSIGNMENT_ID  (påkrevd) tallet i booking-adressen .../reservation/assignment/<tall>
  RELEASE_TIME          når plassene legges ut, norsk tid (standard 13:00)
  DEADLINE              gi opp etter dette klokkeslettet (standard 16:00)
  START_WINDOW_MIN      avslutt hvis startet mer enn så mange min før RELEASE_TIME (standard 60)
  WEEKDAYS_ONLY         true/false - bare book man-fre (standard true)
  FORCE_NOW             true/false - ikke vent på RELEASE_TIME (for testing)
  DRY_RUN               true/false - vis hva som ville skjedd, uten å booke

Bruk:
  python parkly.py                  # vent til 13:00 og book i morgen
  python parkly.py --now            # prøv med en gang (ikke vent)
  python parkly.py --dry-run        # ingen booking
  python parkly.py --check-token    # logg inn og vis når tokenet utløper
"""
import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://api.parkly.no/api/prepaidpackage/reservation"
TZ = ZoneInfo("Europe/Oslo")
TIMEOUT = 20


class AuthError(Exception):
    pass


def now():
    return datetime.now(TZ)


def env_bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "ja")


def log(level, msg):
    stamp = now().strftime("%H:%M:%S")
    if os.environ.get("GITHUB_ACTIONS") == "true" and level in ("warning", "error", "notice"):
        print(f"::{level}::{msg}", flush=True)
    else:
        print(f"{stamp} [{level.upper()}] {msg}", flush=True)


def info(msg):
    print(f"{now().strftime('%H:%M:%S')} {msg}", flush=True)


def at_today(hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    return now().replace(hour=h, minute=m, second=0, microsecond=0)


# ---------- token ----------

def token_expiry(token):
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        exp = json.loads(base64.urlsafe_b64decode(part)).get("exp")
        return datetime.fromtimestamp(exp, tz=timezone.utc) if exp else None
    except Exception:
        return None


def check_token(token, warn_days, needed_until):
    exp = token_expiry(token)
    if exp is None:
        log("warning", "Fant ingen utløpstid i tokenet. Fortsetter likevel.")
        return True
    left = exp - datetime.now(timezone.utc)
    local = exp.astimezone(TZ).strftime("%d.%m.%Y kl. %H:%M")
    if left.total_seconds() <= 0:
        log("error", f"Tokenet utløp {local}. Hent nytt og oppdater PARKLY_TOKEN.")
        return False
    info(f"Token gyldig til {local} ({left.days} d {left.seconds // 3600} t igjen)")
    if exp < needed_until.astimezone(timezone.utc):
        log("warning", f"Tokenet utløper ({local}) før bookingvinduet er over.")
    elif left < timedelta(days=warn_days):
        log("warning", f"Tokenet utløper snart ({local}). Bytt det i GitHub Secrets.")
    return True


class TokenManager:
    """Holder på et gyldig token, og logger inn på nytt når det er i ferd med å gå ut."""

    def __init__(self, session):
        self.session = session
        self.token = None
        self.static = os.environ.get("PARKLY_TOKEN", "").strip()
        if self.static.lower().startswith("bearer "):
            self.static = self.static[7:].strip()
        self.can_login = all(os.environ.get(k) for k in ("PARKLY_USERNAME", "PARKLY_PASSWORD"))
        if not (self.static or self.can_login):
            raise RuntimeError("Sett PARKLY_USERNAME og PARKLY_PASSWORD (eller PARKLY_TOKEN).")

    def _set(self, token):
        self.token = token
        self.session.headers["Authorization"] = f"Bearer {token}"
        exp = token_expiry(token)
        if exp:
            info(f"Token gyldig til {exp.astimezone(TZ):%H:%M:%S}")

    def refresh(self):
        if not self.can_login:
            raise AuthError()
        from parkly_login import login, refresh_token
        if getattr(self, "rt", None):
            try:
                info("Fornyer token ...")
                tok, self.rt = refresh_token(self.rt)
                self._set(tok)
                return
            except Exception as e:
                log("warning", f"Fornying feilet, logger inn på nytt: {e}")
                self.rt = None
        for attempt in range(1, 4):
            try:
                info("Logger inn på Parkly ...")
                tok, self.rt = login()
                self._set(tok)
                return
            except Exception as e:
                log("warning", f"Innlogging feilet (forsøk {attempt}/3): {e}")
                time.sleep(10)
        raise AuthError()

    def ensure(self, valid_until=None):
        """Sørg for et token som er gyldig minst til valid_until (standard: 5 min fra nå)."""
        valid_until = valid_until or (now() + timedelta(minutes=5))
        if self.token is None and self.static:
            self._set(self.static)
            self.static = ""
        exp = token_expiry(self.token) if self.token else None
        if self.token and (exp is None or exp > valid_until.astimezone(timezone.utc)):
            return
        self.refresh()


# ---------- API ----------

def collect_dates(obj, found):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in ("date", "reservationdate") and isinstance(value, str):
                found.add(value[:10])
            else:
                collect_dates(value, found)
    elif isinstance(obj, list):
        for item in obj:
            collect_dates(item, found)
    return found


def is_booked(session, day):
    """True/False, eller None hvis vi ikke fikk svar."""
    try:
        r = session.get(
            f"{BASE_URL}/my-reservations",
            params={"fromDate": day.isoformat(), "toDate": day.isoformat()},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        log("warning", f"my-reservations feilet: {e}")
        return None
    if r.status_code == 401:
        raise AuthError()
    if not r.ok:
        log("warning", f"my-reservations svarte {r.status_code}: {r.text[:200]}")
        return None
    try:
        return day.isoformat() in collect_dates(r.json(), set())
    except ValueError:
        return None


def try_book(session, assignment_id, day):
    """Returnerer (True, tekst) ved bekreftet booking, ellers (False, tekst)."""
    try:
        r = session.post(
            f"{BASE_URL}/assignment/{assignment_id}",
            json={"dates": [day.isoformat()]},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return False, f"nettverksfeil: {e}"
    if r.status_code == 401:
        raise AuthError()
    body = r.text[:300].replace("\n", " ")
    if not r.ok:
        return False, f"HTTP {r.status_code}: {body}"
    try:
        booked = collect_dates(r.json(), set())
    except ValueError:
        booked = set()
    if day.isoformat() in booked:
        return True, body
    return False, f"HTTP {r.status_code}, men datoen var ikke i svaret: {body}"


def retry_interval(since_release):
    """Svært hyppig rett etter 13:00, roligere etter hvert."""
    s = since_release.total_seconds()
    if s < 60:
        return 1
    if s < 300:
        return 3
    if s < 900:
        return 10
    if s < 3600:
        return 30
    return 120


# ---------- main ----------

def main():
    p = argparse.ArgumentParser(description="Automatisk booking av parkering hos Parkly")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--now", action="store_true", help="ikke vent på frigivelsestidspunktet")
    p.add_argument("--check-token", action="store_true")
    args = p.parse_args()

    assignment_id = os.environ.get("PARKLY_ASSIGNMENT_ID", "").strip()
    if not assignment_id.isdigit():
        log("error", "PARKLY_ASSIGNMENT_ID mangler eller er ikke et tall. Legg det inn som secret.")
        return 1
    release = at_today(os.environ.get("RELEASE_TIME", "13:00"))
    deadline = at_today(os.environ.get("DEADLINE", "16:00"))
    start_window = timedelta(minutes=int(os.environ.get("START_WINDOW_MIN", "60")))
    weekdays_only = env_bool("WEEKDAYS_ONLY", True)
    force_now = args.now or env_bool("FORCE_NOW", False)
    dry_run = args.dry_run or env_bool("DRY_RUN", False)

    session = requests.Session()
    session.headers.update({"Accept": "application/json"})
    try:
        tokens = TokenManager(session)
    except RuntimeError as e:
        log("error", str(e))
        return 1

    if args.check_token:
        try:
            tokens.ensure()
        except AuthError:
            log("error", "Fikk ikke logget inn. Sjekk brukernavn og passord.")
            return 1
        exp = token_expiry(tokens.token)
        log("notice", "Innlogging OK" + (f", token gyldig til {exp.astimezone(TZ):%H:%M}" if exp else ""))
        return 0

    day = (now() + timedelta(days=1)).date()
    info(f"Assignment: {assignment_id}")
    info(f"Måldato: {day.isoformat()} ({['man','tir','ons','tor','fre','lør','søn'][day.weekday()]})")
    if weekdays_only and day.weekday() >= 5:
        info("Måldatoen er helg. Ingenting å gjøre.")
        return 0

    if not force_now:
        if now() < release - start_window:
            # Skjer når begge cron-tidene kjører (sommer-/vintertid). Den andre kjøringen tar det.
            info(f"For tidlig (frigivelse {release:%H:%M}). Avslutter, neste planlagte kjøring tar det.")
            return 0
        if now() > deadline:
            info(f"Etter fristen {deadline:%H:%M}. Sjekker bare status.")

    try:
        tokens.ensure()
        if is_booked(session, day):
            info(f"{day.isoformat()} er allerede booket.")
            return 0

        if dry_run:
            info(f"DRY RUN: ville ventet til {release:%H:%M} og postet "
                 f"{{\"dates\": [\"{day.isoformat()}\"]}} til assignment {assignment_id}")
            return 0

        if not force_now and now() < release:
            wake = release - timedelta(seconds=3)
            login_at = release - timedelta(minutes=5)
            info(f"Venter til {wake:%H:%M:%S} (ny innlogging {login_at:%H:%M}) ...")
            while now() < login_at:
                time.sleep(min(30, max(0.2, (login_at - now()).total_seconds())))
            tokens.ensure(valid_until=release + timedelta(minutes=30))
            while now() < wake:
                time.sleep(min(30, max(0.2, (wake - now()).total_seconds())))

        stop = deadline if not force_now else now() + timedelta(minutes=2)
        attempt = 0
        while now() <= stop:
            attempt += 1
            tokens.ensure()
            try:
                ok, msg = try_book(session, assignment_id, day)
            except AuthError:
                log("warning", "Token avvist (401), logger inn på nytt.")
                tokens.refresh()
                continue
            if ok:
                log("notice", f"Booket {day.isoformat()} på forsøk {attempt}.")
                info(msg)
                return 0
            info(f"Forsøk {attempt}: {msg}")

            # Av og til: sjekk om bookingen faktisk gikk gjennom likevel
            if attempt % 10 == 0 and is_booked(session, day):
                log("notice", f"{day.isoformat()} står som booket.")
                return 0

            time.sleep(retry_interval(now() - release))

    except AuthError:
        log("error", "Fikk ikke logget inn / token avvist. Sjekk brukernavn og passord.")
        return 1

    log("error", f"Fikk ikke booket {day.isoformat()} innen {stop:%H:%M}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
