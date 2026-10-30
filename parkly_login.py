"""Logger inn på Parkly og henter et ferskt access token.

Gjør det samme som Parkly-appen i nettleseren: åpner Parklys innloggingsside
(Azure AD B2C) med en egen PKCE-kode, fyller inn brukernavn og passord i en
usynlig nettleser, fanger opp innloggingskoden og veksler den inn i et token.
Får også et refresh token, så senere fornyelser går uten nettleser.

Test alene:
  python parkly_login.py

Miljøvariabler:
  PARKLY_USERNAME   e-post/brukernavn
  PARKLY_PASSWORD   passord
"""
import base64
import hashlib
import os
import secrets
import sys
import time
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from playwright.sync_api import sync_playwright

AUTHORITY = "https://login.parkly.no/login.parkly.no/B2C_1A_signup_signin/oauth2/v2.0"
CLIENT_ID = "68841595-6caf-4778-b02b-1ec681884288"
SCOPE = "https://b2cparklyidpprd.onmicrosoft.com/customer-api/read openid profile offline_access"
REDIRECT_URI = "https://app.parkly.no"
ORIGIN = "https://app.parkly.no"

DEBUG_DIR = "debug"

USER_SELECTORS = [
    "input#signInName", "input#email", "input#logonIdentifier",
    "input[name='username']", "input[name='loginfmt']",
    "input[type='email']", "input[autocomplete='username']",
]
PASS_SELECTORS = ["input#password", "input[type='password']"]
SUBMIT_SELECTORS = [
    "button#next", "button#continue", "button[type='submit']",
    "input[type='submit']", "#idSIButton9",
]

_step = [0]


def _host(url):
    # Bare domene + sti, aldri query-parametre (kan inneholde koder)
    return url.split("?")[0].split("#")[0]


def _save_debug(page, name):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        page.screenshot(path=f"{DEBUG_DIR}/{name}.png", full_page=True)
        with open(f"{DEBUG_DIR}/{name}.html", "w") as f:
            f.write(page.content())
    except Exception:
        pass


def _trace(page, what):
    _step[0] += 1
    try:
        title = page.title()
    except Exception:
        title = "?"
    print(f"   [login {_step[0]}] {what} | {_host(page.url)} | tittel: {title}", flush=True)
    _save_debug(page, f"{_step[0]:02d}_{what.replace(' ', '_')[:30]}")


def _first_visible(page, selectors, timeout_ms):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for frame in page.frames:
            for sel in selectors:
                try:
                    loc = frame.locator(sel).first
                    if loc.count() and loc.is_visible():
                        return loc
                except Exception:
                    pass
        time.sleep(0.3)
    return None


def _page_error(page):
    for sel in (".error.pageLevel", ".error.itemLevel.show", "[role='alert']", ".error"):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                txt = loc.inner_text().strip()
                if txt:
                    return txt[:200]
        except Exception:
            pass
    return ""


def _token_request(data):
    r = requests.post(
        f"{AUTHORITY}/token",
        data={"client_id": CLIENT_ID, "scope": SCOPE, **data},
        headers={"Origin": ORIGIN},  # SPA-klient: Azure krever Origin-header
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Token-endepunktet svarte {r.status_code}: {r.text[:300]}")
    body = r.json()
    if "access_token" not in body:
        raise RuntimeError(f"Fikk ikke access_token. Felter: {sorted(body)}")
    return body


def refresh_token(rt):
    """Fornyer med refresh token, uten nettleser. Returnerer (access_token, nytt refresh_token)."""
    body = _token_request({"grant_type": "refresh_token", "refresh_token": rt})
    return body["access_token"], body.get("refresh_token", rt)


def login(username=None, password=None):
    """Full innlogging. Returnerer (access_token, refresh_token eller None)."""
    username = username or os.environ.get("PARKLY_USERNAME", "")
    password = password or os.environ.get("PARKLY_PASSWORD", "")
    if not (username and password):
        raise RuntimeError("PARKLY_USERNAME og PARKLY_PASSWORD må være satt.")

    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    auth_url = f"{AUTHORITY}/authorize?" + urlencode({
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "response_mode": "query",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "ui_locales": "en",
        "state": state,
        "prompt": "login",
    })

    captured = {}
    seen = set()

    def grab(url, body=None):
        """Finn code/state i URL (query eller #fragment) eller i POST-body."""
        if captured or not url.startswith(REDIRECT_URI):
            return
        parsed = urlparse(url)
        seen.add(f"{parsed.path or '/'}?{','.join(sorted(parse_qs(parsed.query)))}#{','.join(sorted(parse_qs(parsed.fragment)))}")
        for raw in (parsed.query, parsed.fragment, body or ""):
            q = parse_qs(raw)
            if "code" in q or "error" in q:
                captured.update({k: v[0] for k, v in q.items()})
                return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="en-US", timezone_id="Europe/Oslo")
        page = ctx.new_page()
        # Fanger også forespørsler som er resultat av redirect (det gjør ikke page.route)
        ctx.on("request", lambda req: grab(req.url, req.post_data if req.method == "POST" else None))
        page.on("framenavigated", lambda fr: grab(fr.url))
        try:
            page.goto(auth_url, wait_until="domcontentloaded", timeout=45000)
            user = _first_visible(page, USER_SELECTORS, 20000)
            _trace(page, "brukernavnfelt " + ("funnet" if user else "IKKE funnet"))
            if not user:
                raise RuntimeError("Fant ikke brukernavnfeltet på innloggingssiden.")

            user.fill(username)
            pw = _first_visible(page, PASS_SELECTORS, 1500)
            if not pw:  # to-stegs: e-post først, så passord
                sub = _first_visible(page, SUBMIT_SELECTORS, 3000)
                if sub:
                    sub.click()
                pw = _first_visible(page, PASS_SELECTORS, 20000)
            if not pw:
                _trace(page, "passordfelt IKKE funnet")
                raise RuntimeError("Fant ikke passordfeltet.")
            pw.fill(password)
            sub = _first_visible(page, SUBMIT_SELECTORS, 3000)
            if sub:
                sub.click()
            else:
                pw.press("Enter")

            deadline = time.time() + 45
            while time.time() < deadline and not captured:
                page.wait_for_timeout(500)
                err = _page_error(page)
                if err and "login.parkly.no" in page.url:
                    _trace(page, "feilmelding")
                    raise RuntimeError(f"Innloggingssiden sa: {err}")

            if not captured:
                print(f"   app-adresser sett (bare feltnavn): {sorted(seen)[:8]}", flush=True)
                _trace(page, "ingen kode etter innsending")
                raise RuntimeError("Fikk ingen innloggingskode tilbake.")
            print("   [login] innloggingskode mottatt", flush=True)
        finally:
            ctx.close()
            browser.close()

    if "error" in captured:
        raise RuntimeError(f"Innlogging avvist: {captured.get('error')} {captured.get('error_description', '')[:200]}")
    if captured.get("state") != state:
        raise RuntimeError("Uventet svar fra innloggingen (state stemte ikke).")

    body = _token_request({
        "grant_type": "authorization_code",
        "code": captured["code"],
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })
    return body["access_token"], body.get("refresh_token")


def get_token():
    return login()[0]


if __name__ == "__main__":
    try:
        tok, rt = login()
    except Exception as e:
        print(f"Innlogging feilet: {e}")
        sys.exit(1)
    print(f"Innlogging OK. Token: {len(tok)} tegn. Refresh token: {'ja' if rt else 'nei'}.")
