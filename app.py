"""Walhad Housing Solutions — single-file Flask application.

All application logic, HTML, CSS, and JavaScript live in this file. Uploaded brand
and property imagery is served from static/images so the files can be versioned
and deployed with the application.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    render_template_string,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix


# -----------------------------------------------------------------------------
# Application configuration
# -----------------------------------------------------------------------------

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=24 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(
        os.environ.get("SESSION_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}
        or bool(os.environ.get("RENDER"))
    ),
    SEND_FILE_MAX_AGE_DEFAULT=60 * 60 * 24 * 7,
)

COMPANY = {
    "name": "Walhad Housing Solutions",
    "executive": "Mohamed Said Ahmed",
    "email": os.environ.get("CONTACT_EMAIL", "").strip(),
    "phone": os.environ.get("CONTACT_PHONE", "").strip(),
    "whatsapp": os.environ.get("WHATSAPP_NUMBER", "").strip(),
    "address": os.environ.get("OFFICE_ADDRESS", "").strip(),
    "facebook": os.environ.get("FACEBOOK_URL", "").strip(),
    "instagram": os.environ.get("INSTAGRAM_URL", "").strip(),
    "linkedin": os.environ.get("LINKEDIN_URL", "").strip(),
}

LOCATIONS = ("Borama", "Hargeisa", "Gabiley")
INQUIRY_TYPES = ("Rent", "Buy", "List a Property", "General Inquiry")


# -----------------------------------------------------------------------------
# Editable property data
# Add future listings here. Keep factual fields empty/None when not yet supplied.
# Image names map to files in static/images.
# -----------------------------------------------------------------------------

PROPERTIES: list[dict[str, Any]] = [
    {
        "id": "hodan-hills-3-bedroom-home",
        "reference": "WHS-HRG-001",
        "title": "Hodan Hills 3-Bedroom Home",
        "location": "Hargeisa",
        "area": "Hodan Hills",
        "purpose": "rent",
        "property_type": "House",
        "price": 600,
        "currency": "USD",
        "price_period": "month",
        "bedrooms": 3,
        "bathrooms": 2,
        "description": (
            "A three-bedroom, two-bathroom residential home in Hodan Hills. "
            "The supplied listing shows an asking rent of USD 600 per month. "
            "Please contact our team to confirm availability, features, and viewing details."
        ),
        "features": [
            "3 bedrooms",
            "2 bathrooms",
            "Residential house",
            "Listing details subject to confirmation",
        ],
        "images": ["hodan-hills-3-bedroom.jpg"],
        "availability": "Contact to confirm",
        "is_sample": False,
    },
    {
        "id": "hodan-hills-5-bedroom-home",
        "reference": "WHS-HRG-002",
        "title": "Hodan Hills 5-Bedroom Home",
        "location": "Hargeisa",
        "area": "Hodan Hills",
        "purpose": "rent",
        "property_type": "House",
        "price": 150,
        "currency": "USD",
        "price_period": "month",
        "bedrooms": 5,
        "bathrooms": 2,
        "description": (
            "A five-bedroom, two-bathroom residential home in Hodan Hills. "
            "The supplied listing shows an asking rent of USD 150 per month. "
            "Please contact our team to confirm availability, features, and viewing details."
        ),
        "features": [
            "5 bedrooms",
            "2 bathrooms",
            "Residential house",
            "Listing details subject to confirmation",
        ],
        "images": ["hodan-hills-5-bedroom.jpg"],
        "availability": "Contact to confirm",
        "is_sample": False,
    },
]


# -----------------------------------------------------------------------------
# Helpers, security, and contact delivery
# -----------------------------------------------------------------------------

_rate_buckets: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = threading.Lock()
RATE_LIMIT = 5
RATE_WINDOW_SECONDS = 10 * 60


def csrf_token() -> str:
    """Return a session-bound CSRF token, creating it when needed."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def price_display(property_item: dict[str, Any]) -> str:
    """Format only supplied prices; never manufacture a value."""
    price = property_item.get("price")
    if price is None:
        return "Price on Request"
    currency = property_item.get("currency") or ""
    symbol = "$" if currency == "USD" else f"{currency} "
    amount = f"{price:,.0f}" if isinstance(price, (int, float)) else str(price)
    period = property_item.get("price_period")
    return f"{symbol}{amount}{f' / {period}' if period else ''}"


def public_properties() -> list[dict[str, Any]]:
    """Create browser-safe property dictionaries with resolved asset URLs."""
    result = []
    for source in PROPERTIES:
        item = deepcopy(source)
        item["images"] = [
            url_for("static", filename=f"images/{filename}")
            for filename in source.get("images", [])
        ]
        if not item["images"]:
            item["images"] = [url_for("fallback_property_image")]
        item["price_display"] = price_display(item)
        item["purpose_label"] = "For Rent" if item["purpose"] == "rent" else "For Sale"
        result.append(item)
    return result


def normalize_single_line(value: str, limit: int) -> str:
    value = value.replace("\x00", " ")
    return " ".join(value.split())[:limit]


def normalize_message(value: str, limit: int = 2000) -> str:
    value = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in value.split("\n")]
    return "\n".join(lines).strip()[:limit]


def is_rate_limited(client_key: str) -> bool:
    now = time.monotonic()
    with _rate_lock:
        bucket = _rate_buckets[client_key]
        while bucket and now - bucket[0] > RATE_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT:
            return True
        bucket.append(now)
        if len(_rate_buckets) > 5000:
            stale = [key for key, values in _rate_buckets.items() if not values or now - values[-1] > RATE_WINDOW_SECONDS]
            for key in stale:
                _rate_buckets.pop(key, None)
    return False


def validate_contact_form(form: Any) -> tuple[dict[str, str], dict[str, str]]:
    data = {
        "full_name": normalize_single_line(form.get("full_name", ""), 100),
        "email": normalize_single_line(form.get("email", ""), 254).lower(),
        "phone": normalize_single_line(form.get("phone", ""), 30),
        "location": normalize_single_line(form.get("location", ""), 30),
        "inquiry_type": normalize_single_line(form.get("inquiry_type", ""), 30),
        "property_reference": normalize_single_line(form.get("property_reference", ""), 80),
        "message": normalize_message(form.get("message", "")),
    }
    errors: dict[str, str] = {}

    if not 2 <= len(data["full_name"]) <= 100:
        errors["full_name"] = "Enter your full name (2–100 characters)."
    email_pattern = r"^[^\s@]{1,64}@[^\s@.]+(?:\.[^\s@.]+)+$"
    if not re.fullmatch(email_pattern, data["email"]):
        errors["email"] = "Enter a valid email address."
    phone_digits = re.sub(r"\D", "", data["phone"])
    if not re.fullmatch(r"[+()\-\.\s\d]{7,30}", data["phone"]) or not 7 <= len(phone_digits) <= 18:
        errors["phone"] = "Enter a valid phone number."
    if data["location"] not in LOCATIONS:
        errors["location"] = "Choose Borama, Hargeisa, or Gabiley."
    if data["inquiry_type"] not in INQUIRY_TYPES:
        errors["inquiry_type"] = "Choose a valid inquiry type."
    if len(data["message"]) < 20:
        errors["message"] = "Please provide at least 20 characters so we can understand your inquiry."
    if len(data["message"]) > 2000:
        errors["message"] = "Keep your message under 2,000 characters."
    if len(data["property_reference"]) > 80:
        errors["property_reference"] = "Keep the property reference under 80 characters."

    return data, errors


def deliver_to_webhook(data: dict[str, str]) -> tuple[bool, str]:
    """Optionally send validated data to an operator-configured HTTPS webhook."""
    webhook_url = os.environ.get("CONTACT_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return False, "demo"

    parsed = urllib.parse.urlparse(webhook_url)
    if parsed.scheme != "https" or not parsed.netloc:
        app.logger.error("CONTACT_WEBHOOK_URL must be a valid HTTPS URL")
        return False, "configuration"

    payload = {
        **data,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "source": "walhad-housing-solutions-website",
    }
    headers = {"Content-Type": "application/json", "User-Agent": "WalhadHousing/1.0"}
    token = os.environ.get("CONTACT_WEBHOOK_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    webhook_request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(webhook_request, timeout=8) as response:
            if 200 <= response.status < 300:
                return True, "delivered"
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        app.logger.warning("Contact webhook delivery failed: %s", type(exc).__name__)
    return False, "delivery"


@app.before_request
def set_security_context() -> None:
    g.csp_nonce = secrets.token_urlsafe(18)


@app.after_request
def add_security_headers(response: Response) -> Response:
    nonce = getattr(g, "csp_nonce", "")
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# -----------------------------------------------------------------------------
# Embedded template — all HTML, CSS, and JavaScript are intentionally in app.py
# -----------------------------------------------------------------------------

PAGE_TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#071d36">
  <meta name="description" content="Discover residential properties for rent and sale in Borama, Hargeisa, and Gabiley with Walhad Housing Solutions.">
  <meta property="og:title" content="Walhad Housing Solutions">
  <meta property="og:description" content="Quality property opportunities across Borama, Hargeisa, and Gabiley.">
  <meta property="og:type" content="website">
  <meta property="og:image" content="{{ url_for('static', filename='images/walhad-logo.jpg', _external=True) }}">
  <link rel="icon" href="{{ url_for('static', filename='images/walhad-logo.jpg') }}">
  <title>{{ page_title }}</title>
  <style nonce="{{ csp_nonce }}">
    :root {
      --navy-950: #06182d;
      --navy-900: #08213e;
      --navy-800: #0b315d;
      --navy-700: #124779;
      --gold-600: #b98216;
      --gold-500: #cf9a2e;
      --gold-400: #e2ba61;
      --ivory: #f8f6f0;
      --sand: #eee8dc;
      --white: #ffffff;
      --ink: #122033;
      --muted: #647083;
      --line: #e6e1d8;
      --success: #15715f;
      --danger: #b43b3b;
      --shadow-sm: 0 8px 24px rgba(6, 24, 45, .07);
      --shadow-md: 0 20px 55px rgba(6, 24, 45, .12);
      --shadow-lg: 0 35px 90px rgba(6, 24, 45, .20);
      --radius-sm: 12px;
      --radius-md: 20px;
      --radius-lg: 30px;
      --shell: min(1180px, calc(100% - 40px));
      --header-height: 78px;
    }

    *, *::before, *::after { box-sizing: border-box; }
    html { scroll-behavior: smooth; scroll-padding-top: calc(var(--header-height) + 18px); }
    body {
      margin: 0;
      color: var(--ink);
      background: var(--ivory);
      font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      line-height: 1.6;
      -webkit-font-smoothing: antialiased;
      overflow-x: hidden;
    }
    body.menu-open, body.modal-open { overflow: hidden; }
    img { display: block; max-width: 100%; }
    button, input, select, textarea { font: inherit; }
    button, a { -webkit-tap-highlight-color: transparent; }
    a { color: inherit; text-decoration: none; }
    h1, h2, h3, p { margin-top: 0; }
    h1, h2, h3 { line-height: 1.08; letter-spacing: -.035em; }
    :focus-visible { outline: 3px solid rgba(207, 154, 46, .65); outline-offset: 3px; }
    .shell { width: var(--shell); margin-inline: auto; }
    .sr-only {
      position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
      overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
    }
    .skip-link {
      position: fixed; z-index: 2000; left: 18px; top: -80px;
      padding: 11px 16px; border-radius: 10px; color: var(--white); background: var(--navy-900);
      transition: top .2s ease;
    }
    .skip-link:focus { top: 14px; }

    .eyebrow {
      display: inline-flex; align-items: center; gap: 10px; margin-bottom: 16px;
      color: var(--gold-600); font-size: .76rem; font-weight: 800; letter-spacing: .16em; text-transform: uppercase;
    }
    .eyebrow::before { content: ""; width: 30px; height: 2px; background: currentColor; }
    .eyebrow-light { color: var(--gold-400); }
    .section-heading { max-width: 700px; }
    .section-heading h2 { margin-bottom: 16px; color: var(--navy-950); font-size: clamp(2.15rem, 5vw, 3.75rem); }
    .section-heading p { margin-bottom: 0; color: var(--muted); font-size: 1.08rem; }

    .btn {
      min-height: 50px; display: inline-flex; align-items: center; justify-content: center; gap: 10px;
      padding: 12px 20px; border: 1px solid transparent; border-radius: 999px;
      font-weight: 750; line-height: 1; cursor: pointer; transition: transform .22s ease, box-shadow .22s ease, background .22s ease, border-color .22s ease;
    }
    .btn:hover { transform: translateY(-2px); }
    .btn-primary { color: var(--navy-950); background: var(--gold-400); box-shadow: 0 10px 28px rgba(185, 130, 22, .25); }
    .btn-primary:hover { background: #ebc975; box-shadow: 0 14px 32px rgba(185, 130, 22, .32); }
    .btn-navy { color: var(--white); background: var(--navy-900); box-shadow: 0 10px 28px rgba(6, 24, 45, .18); }
    .btn-navy:hover { background: var(--navy-800); }
    .btn-outline { color: var(--white); border-color: rgba(255,255,255,.5); background: rgba(255,255,255,.08); backdrop-filter: blur(10px); }
    .btn-outline:hover { background: rgba(255,255,255,.16); border-color: var(--white); }
    .btn-ghost { color: var(--navy-900); border-color: #cfd6dd; background: var(--white); }
    .btn-ghost:hover { border-color: var(--navy-700); background: #f8fafc; }
    .btn svg { width: 18px; height: 18px; flex: 0 0 auto; }

    /* Header */
    .site-header {
      position: sticky; z-index: 1000; top: 0; height: var(--header-height);
      background: rgba(255,255,255,.93); border-bottom: 1px solid rgba(6,24,45,.08);
      backdrop-filter: blur(18px); transition: box-shadow .25s ease;
    }
    .site-header.scrolled { box-shadow: 0 12px 34px rgba(6,24,45,.09); }
    .nav-wrap { height: 100%; display: flex; align-items: center; justify-content: space-between; gap: 28px; }
    .brand { display: inline-flex; align-items: center; gap: 12px; min-width: 230px; }
    .brand-logo { width: 56px; height: 51px; object-fit: contain; border-radius: 8px; background: var(--white); }
    .brand-copy { display: grid; line-height: 1.02; }
    .brand-name { color: var(--navy-900); font-size: 1.04rem; font-weight: 900; letter-spacing: .01em; }
    .brand-subtitle { margin-top: 5px; color: var(--gold-600); font-size: .59rem; font-weight: 800; letter-spacing: .16em; text-transform: uppercase; }
    .desktop-nav { display: flex; align-items: center; gap: 30px; }
    .nav-link { position: relative; color: #344255; font-size: .92rem; font-weight: 700; }
    .nav-link::after {
      content: ""; position: absolute; left: 0; right: 100%; bottom: -9px; height: 2px;
      background: var(--gold-500); transition: right .25s ease;
    }
    .nav-link:hover, .nav-link.active { color: var(--navy-900); }
    .nav-link:hover::after, .nav-link.active::after { right: 0; }
    .nav-actions { display: flex; align-items: center; gap: 12px; }
    .header-cta { min-height: 44px; padding-inline: 17px; font-size: .88rem; }
    .menu-toggle {
      display: none; width: 46px; height: 46px; border: 1px solid var(--line); border-radius: 50%;
      background: var(--white); color: var(--navy-900); cursor: pointer;
    }
    .menu-toggle span { display: block; width: 19px; height: 2px; margin: 4px auto; background: currentColor; transition: transform .25s ease, opacity .2s ease; }
    .menu-toggle[aria-expanded="true"] span:nth-child(1) { transform: translateY(6px) rotate(45deg); }
    .menu-toggle[aria-expanded="true"] span:nth-child(2) { opacity: 0; }
    .menu-toggle[aria-expanded="true"] span:nth-child(3) { transform: translateY(-6px) rotate(-45deg); }
    .mobile-menu {
      position: fixed; z-index: 999; inset: var(--header-height) 0 auto 0; padding: 18px 20px 24px;
      background: var(--white); border-bottom: 1px solid var(--line); box-shadow: var(--shadow-md);
      transform: translateY(-125%); visibility: hidden; transition: transform .28s ease, visibility .28s;
    }
    .mobile-menu.open { transform: translateY(0); visibility: visible; }
    .mobile-menu a:not(.btn) { display: block; padding: 14px 5px; border-bottom: 1px solid #eeeae3; color: var(--navy-900); font-weight: 750; }
    .mobile-menu .btn { width: 100%; margin-top: 18px; }
    .menu-scrim { position: fixed; z-index: 998; inset: var(--header-height) 0 0; background: rgba(6,24,45,.42); opacity: 0; visibility: hidden; transition: .25s ease; }
    .menu-scrim.open { opacity: 1; visibility: visible; }

    /* Hero */
    .hero { position: relative; height: clamp(730px, calc(100svh - var(--header-height)), 880px); min-height: 730px; overflow: hidden; background: var(--navy-950); }
    .hero-slides, .hero-slide { position: absolute; inset: 0; }
    .hero-slide { opacity: 0; visibility: hidden; transition: opacity .85s ease, visibility .85s ease; }
    .hero-slide.active { opacity: 1; visibility: visible; }
    .hero-bg { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; object-position: center; transform: scale(1.02); }
    .hero-shade { position: absolute; inset: 0; background: linear-gradient(90deg, rgba(4,18,34,.94) 0%, rgba(4,18,34,.78) 48%, rgba(4,18,34,.31) 76%, rgba(4,18,34,.15) 100%); }
    .hero-shade::after { content: ""; position: absolute; inset: auto 0 0; height: 34%; background: linear-gradient(0deg, rgba(4,18,34,.45), transparent); }
    .hero-inner { position: relative; z-index: 2; height: 100%; display: flex; flex-direction: column; justify-content: center; padding-block: 54px 48px; }
    .hero-content { width: min(720px, 72%); color: var(--white); }
    .hero-content h1 { max-width: 690px; margin-bottom: 22px; font-size: clamp(3rem, 6vw, 5.8rem); letter-spacing: -.055em; }
    .hero-content > p { max-width: 630px; margin-bottom: 30px; color: rgba(255,255,255,.8); font-size: clamp(1.02rem, 1.7vw, 1.22rem); }
    .hero-actions { display: flex; flex-wrap: wrap; gap: 12px; }
    .hero-search {
      width: 100%; margin-top: 52px; padding: 12px; display: grid; grid-template-columns: 1.1fr 1fr 1.15fr auto; gap: 9px;
      border: 1px solid rgba(255,255,255,.23); border-radius: 20px; background: rgba(255,255,255,.96);
      box-shadow: var(--shadow-lg); color: var(--ink); backdrop-filter: blur(16px);
    }
    .search-field { min-width: 0; padding: 5px 14px 6px; border-right: 1px solid #e6e8eb; }
    .search-field label { display: block; margin-bottom: 0; color: #778292; font-size: .66rem; font-weight: 800; letter-spacing: .1em; text-transform: uppercase; }
    .search-field select {
      width: 100%; height: 34px; padding: 0 30px 0 0; border: 0; color: var(--navy-900); background: transparent;
      font-weight: 750; outline: 0; cursor: pointer;
    }
    .hero-search .btn { min-width: 130px; border-radius: 13px; }
    .hero-note { display: flex; align-items: center; gap: 9px; margin-top: 17px; color: rgba(255,255,255,.68); font-size: .83rem; }
    .hero-note svg { width: 16px; height: 16px; color: var(--gold-400); }

    .executive-slide { background: radial-gradient(circle at 77% 48%, #173f68 0, var(--navy-900) 34%, var(--navy-950) 75%); }
    .executive-slide::before {
      content: ""; position: absolute; width: 720px; height: 720px; right: -140px; top: -150px; border: 1px solid rgba(226,186,97,.14); border-radius: 50%;
      box-shadow: 0 0 0 80px rgba(226,186,97,.025), 0 0 0 180px rgba(226,186,97,.018);
    }
    .executive-grid { position: relative; z-index: 2; height: 100%; display: grid; grid-template-columns: 1.08fr .75fr; align-items: center; gap: 70px; padding-block: 54px; }
    .executive-copy { color: var(--white); }
    .executive-copy h2 { max-width: 760px; margin-bottom: 22px; font-size: clamp(3rem, 5.8vw, 5.45rem); }
    .executive-copy > p { max-width: 650px; margin-bottom: 32px; color: rgba(255,255,255,.76); font-size: clamp(1.02rem, 1.6vw, 1.2rem); }
    .executive-id { display: flex; align-items: center; gap: 16px; padding-top: 25px; border-top: 1px solid rgba(255,255,255,.18); max-width: 570px; }
    .executive-monogram { width: 46px; height: 46px; display: grid; place-items: center; border-radius: 50%; background: var(--gold-400); color: var(--navy-950); font-weight: 900; }
    .executive-name { display: block; font-weight: 850; }
    .executive-role { display: block; color: var(--gold-400); font-size: .82rem; font-weight: 750; }
    .portrait-wrap { position: relative; width: min(100%, 410px); justify-self: end; }
    .portrait-frame { position: relative; padding: 12px; border: 1px solid rgba(226,186,97,.45); border-radius: 210px 210px 28px 28px; background: rgba(255,255,255,.06); box-shadow: var(--shadow-lg); }
    .portrait-frame img { width: 100%; aspect-ratio: 4 / 5; border-radius: 196px 196px 20px 20px; object-fit: cover; object-position: center top; background: #f4f4f4; }
    .portrait-plaque { position: absolute; right: -26px; bottom: 34px; max-width: 230px; padding: 15px 18px; border-left: 3px solid var(--gold-500); border-radius: 6px 14px 14px 6px; background: rgba(255,255,255,.96); box-shadow: var(--shadow-md); }
    .portrait-plaque strong { display: block; color: var(--navy-900); font-size: .94rem; }
    .portrait-plaque span { color: var(--muted); font-size: .76rem; }
    .hero-controls { position: absolute; z-index: 10; right: max(24px, calc((100vw - 1180px) / 2)); top: 50%; display: grid; gap: 10px; transform: translateY(-50%); }
    .hero-arrow { width: 42px; height: 42px; display: grid; place-items: center; border: 1px solid rgba(255,255,255,.35); border-radius: 50%; color: var(--white); background: rgba(6,24,45,.34); cursor: pointer; backdrop-filter: blur(10px); transition: background .2s ease, border-color .2s ease; }
    .hero-arrow:hover { background: rgba(6,24,45,.7); border-color: var(--gold-400); }
    .hero-arrow svg { width: 17px; height: 17px; }
    .slide-dots { display: flex; justify-content: center; gap: 7px; margin-block: 2px; }
    .slide-dot { width: 7px; height: 7px; padding: 0; border: 0; border-radius: 50%; background: rgba(255,255,255,.45); cursor: pointer; transition: width .2s ease, border-radius .2s ease, background .2s ease; }
    .slide-dot.active { width: 20px; border-radius: 99px; background: var(--gold-400); }

    /* Trust strip */
    .trust-strip { position: relative; z-index: 3; margin-top: -1px; background: var(--white); border-bottom: 1px solid var(--line); }
    .trust-grid { min-height: 108px; display: grid; grid-template-columns: 1.1fr repeat(3, 1fr); align-items: stretch; }
    .trust-intro, .trust-item { display: flex; align-items: center; gap: 14px; padding: 24px 28px; }
    .trust-intro { padding-left: 0; color: var(--navy-900); font-weight: 850; }
    .trust-item { border-left: 1px solid var(--line); }
    .trust-icon { width: 42px; height: 42px; display: grid; place-items: center; flex: 0 0 auto; border-radius: 50%; color: var(--gold-600); background: #fbf5e8; }
    .trust-icon svg { width: 20px; height: 20px; }
    .trust-item strong { display: block; color: var(--navy-900); font-size: .93rem; }
    .trust-item span { color: var(--muted); font-size: .76rem; }

    /* Properties */
    .properties-section { padding: 112px 0 120px; }
    .section-top { display: flex; align-items: end; justify-content: space-between; gap: 30px; margin-bottom: 40px; }
    .listing-note { max-width: 310px; padding: 15px 17px; border-left: 3px solid var(--gold-500); color: var(--muted); background: rgba(255,255,255,.7); font-size: .78rem; }
    .filters-panel { margin-bottom: 25px; padding: 22px; border: 1px solid var(--line); border-radius: var(--radius-md); background: var(--white); box-shadow: var(--shadow-sm); }
    .filter-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)) auto; gap: 12px; align-items: end; }
    .form-field label { display: block; margin-bottom: 7px; color: #435066; font-size: .72rem; font-weight: 800; letter-spacing: .055em; text-transform: uppercase; }
    .form-control {
      width: 100%; min-height: 48px; padding: 11px 13px; border: 1px solid #d8dce1; border-radius: 11px;
      color: var(--ink); background: var(--white); outline: 0; transition: border-color .2s ease, box-shadow .2s ease;
    }
    .form-control:focus { border-color: var(--gold-500); box-shadow: 0 0 0 4px rgba(207,154,46,.13); }
    select.form-control { cursor: pointer; }
    .clear-btn { min-height: 48px; padding: 11px 16px; border: 1px solid #d8dce1; border-radius: 11px; color: var(--navy-900); background: #f8f9fa; font-weight: 750; cursor: pointer; }
    .clear-btn:hover { border-color: var(--navy-700); background: var(--white); }
    .results-row { display: flex; justify-content: space-between; align-items: center; gap: 18px; margin: 20px 2px 24px; }
    .results-count { color: var(--navy-900); font-size: .9rem; font-weight: 800; }
    .results-hint { color: var(--muted); font-size: .79rem; }
    .property-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr)); gap: 26px; }
    .property-card { overflow: hidden; border: 1px solid var(--line); border-radius: 24px; background: var(--white); box-shadow: var(--shadow-sm); transition: transform .28s ease, box-shadow .28s ease, border-color .28s ease; }
    .property-card:hover { transform: translateY(-7px); border-color: #d9d0bf; box-shadow: var(--shadow-md); }
    .property-card[hidden] { display: none; }
    .property-media { position: relative; overflow: hidden; aspect-ratio: 16 / 9; background: linear-gradient(135deg, #e8edf1, #d7dee4); }
    .property-media img { width: 100%; height: 100%; object-fit: cover; transition: transform .55s ease; }
    .property-card:hover .property-media img { transform: scale(1.035); }
    .property-badges { position: absolute; z-index: 1; top: 15px; left: 15px; right: 15px; display: flex; justify-content: space-between; align-items: start; gap: 8px; }
    .badge { display: inline-flex; align-items: center; min-height: 29px; padding: 6px 10px; border-radius: 999px; font-size: .68rem; font-weight: 850; letter-spacing: .04em; text-transform: uppercase; box-shadow: 0 6px 18px rgba(6,24,45,.12); }
    .badge-purpose { color: var(--navy-950); background: var(--gold-400); }
    .badge-location { color: var(--white); background: rgba(6,24,45,.8); backdrop-filter: blur(8px); }
    .property-body { padding: 23px 23px 24px; }
    .property-ref { margin-bottom: 9px; color: var(--gold-600); font-size: .67rem; font-weight: 850; letter-spacing: .11em; text-transform: uppercase; }
    .property-title { margin-bottom: 10px; color: var(--navy-950); font-size: 1.42rem; }
    .property-price { margin-bottom: 17px; color: var(--navy-800); font-size: 1.38rem; font-weight: 900; letter-spacing: -.025em; }
    .property-facts { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-bottom: 18px; padding-block: 14px; border-block: 1px solid #eeeae4; color: #526074; font-size: .82rem; }
    .property-fact { display: inline-flex; align-items: center; gap: 7px; }
    .property-fact svg { width: 17px; height: 17px; color: var(--gold-600); }
    .property-description { min-height: 52px; margin-bottom: 20px; color: var(--muted); font-size: .86rem; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }
    .property-actions { display: grid; grid-template-columns: .8fr 1.2fr; gap: 9px; }
    .property-actions .btn { min-height: 45px; padding-inline: 13px; font-size: .77rem; }
    .no-results { display: none; padding: 54px 25px; border: 1px dashed #cfc7ba; border-radius: var(--radius-md); text-align: center; background: rgba(255,255,255,.64); }
    .no-results.show { display: block; }
    .no-results-icon { width: 58px; height: 58px; display: grid; place-items: center; margin: 0 auto 17px; border-radius: 50%; color: var(--gold-600); background: #f5ead3; }
    .no-results h3 { margin-bottom: 8px; color: var(--navy-900); }
    .no-results p { margin-bottom: 18px; color: var(--muted); }

    /* About */
    .about-section { padding: 120px 0; overflow: hidden; background: var(--white); }
    .about-grid { display: grid; grid-template-columns: .88fr 1.12fr; align-items: center; gap: 80px; }
    .about-visual { position: relative; padding: 0 42px 42px 0; }
    .about-image-shell { overflow: hidden; border-radius: 26px 90px 26px 26px; box-shadow: var(--shadow-lg); }
    .about-image-shell img { width: 100%; aspect-ratio: 4 / 5; object-fit: cover; }
    .about-visual::before { content: ""; position: absolute; z-index: 0; right: 8px; bottom: 10px; width: 70%; height: 68%; border: 1px solid rgba(207,154,46,.5); border-radius: 26px; }
    .about-image-shell, .location-card { position: relative; z-index: 1; }
    .location-card { position: absolute; right: 0; bottom: 78px; width: 215px; padding: 21px; border-radius: 17px; background: var(--navy-900); color: var(--white); box-shadow: var(--shadow-md); }
    .location-card small { display: block; margin-bottom: 7px; color: var(--gold-400); font-weight: 750; letter-spacing: .08em; text-transform: uppercase; }
    .location-card strong { display: block; font-size: 1.05rem; }
    .location-dots { display: flex; gap: 7px; margin-top: 15px; }
    .location-dots span { width: 7px; height: 7px; border-radius: 50%; background: var(--gold-400); }
    .about-copy .section-heading { margin-bottom: 30px; }
    .about-copy > p { color: var(--muted); }
    .mission-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin: 30px 0; }
    .purpose-card { padding: 22px; border: 1px solid var(--line); border-radius: 17px; background: #fcfbf8; }
    .purpose-card .card-number { display: block; margin-bottom: 14px; color: var(--gold-600); font-size: .69rem; font-weight: 900; letter-spacing: .12em; }
    .purpose-card h3 { margin-bottom: 9px; color: var(--navy-900); font-size: 1.14rem; }
    .purpose-card p { margin-bottom: 0; color: var(--muted); font-size: .82rem; }
    .values-label { margin-bottom: 12px; color: var(--navy-900); font-size: .78rem; font-weight: 850; letter-spacing: .09em; text-transform: uppercase; }
    .value-list { display: flex; flex-wrap: wrap; gap: 9px; }
    .value-pill { padding: 8px 13px; border: 1px solid #e3dac8; border-radius: 999px; color: #4b5666; background: #fffaf0; font-size: .78rem; font-weight: 750; }
    .executive-profile { margin-top: 90px; display: grid; grid-template-columns: 250px 1fr; align-items: center; overflow: hidden; border-radius: var(--radius-lg); background: var(--navy-950); color: var(--white); box-shadow: var(--shadow-md); }
    .executive-profile-image { height: 100%; min-height: 275px; background: #edf0f4; }
    .executive-profile-image img { width: 100%; height: 100%; object-fit: cover; object-position: center top; }
    .executive-profile-copy { padding: 44px 50px; }
    .executive-profile-copy h3 { margin-bottom: 8px; font-size: 2rem; }
    .executive-profile-role { margin-bottom: 17px; color: var(--gold-400); font-size: .82rem; font-weight: 800; letter-spacing: .09em; text-transform: uppercase; }
    .executive-profile-copy p { max-width: 700px; margin-bottom: 0; color: rgba(255,255,255,.72); }

    /* Contact */
    .contact-section { padding: 120px 0; background: #efeae0; }
    .contact-shell { display: grid; grid-template-columns: .72fr 1.28fr; overflow: hidden; border: 1px solid #ded7ca; border-radius: var(--radius-lg); background: var(--white); box-shadow: var(--shadow-md); }
    .contact-info { position: relative; overflow: hidden; padding: 54px 45px; color: var(--white); background: var(--navy-950); }
    .contact-info::after { content: ""; position: absolute; right: -130px; bottom: -150px; width: 330px; height: 330px; border: 1px solid rgba(226,186,97,.2); border-radius: 50%; box-shadow: 0 0 0 55px rgba(226,186,97,.035); }
    .contact-info h2 { position: relative; z-index: 1; margin-bottom: 18px; font-size: clamp(2.3rem, 4vw, 3.6rem); }
    .contact-info > p { position: relative; z-index: 1; margin-bottom: 35px; color: rgba(255,255,255,.7); }
    .contact-list { position: relative; z-index: 1; display: grid; gap: 12px; }
    .contact-item { display: grid; grid-template-columns: 42px 1fr; align-items: center; gap: 12px; padding: 13px; border: 1px solid rgba(255,255,255,.12); border-radius: 14px; background: rgba(255,255,255,.055); }
    .contact-item-icon { width: 42px; height: 42px; display: grid; place-items: center; border-radius: 11px; color: var(--gold-400); background: rgba(226,186,97,.1); }
    .contact-item-icon svg { width: 19px; height: 19px; }
    .contact-item small { display: block; color: rgba(255,255,255,.55); font-size: .67rem; font-weight: 750; letter-spacing: .08em; text-transform: uppercase; }
    .contact-item strong { display: block; color: var(--white); font-size: .83rem; overflow-wrap: anywhere; }
    .demo-note { position: relative; z-index: 1; margin-top: 28px; padding: 14px 15px; border: 1px solid rgba(226,186,97,.24); border-radius: 12px; color: rgba(255,255,255,.7); background: rgba(226,186,97,.07); font-size: .74rem; }
    .demo-note strong { color: var(--gold-400); }
    .contact-form-wrap { padding: 54px 50px; }
    .contact-form-heading { margin-bottom: 28px; }
    .contact-form-heading h3 { margin-bottom: 7px; color: var(--navy-950); font-size: 1.8rem; }
    .contact-form-heading p { margin-bottom: 0; color: var(--muted); font-size: .88rem; }
    .contact-form { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
    .contact-form .full-width { grid-column: 1 / -1; }
    .contact-form textarea { min-height: 138px; resize: vertical; }
    .field-error { min-height: 18px; margin: 5px 0 0; color: var(--danger); font-size: .72rem; }
    .form-control.invalid { border-color: var(--danger); box-shadow: 0 0 0 3px rgba(180,59,59,.1); }
    .form-footer { grid-column: 1 / -1; display: flex; align-items: center; justify-content: space-between; gap: 18px; margin-top: 2px; }
    .form-status { flex: 1; min-height: 24px; margin: 0; font-size: .8rem; }
    .form-status.success { color: var(--success); }
    .form-status.error { color: var(--danger); }
    .submit-btn[disabled] { cursor: wait; opacity: .75; transform: none; }
    .hp-field { position: absolute !important; left: -10000px !important; width: 1px !important; height: 1px !important; overflow: hidden !important; }

    /* Footer */
    .site-footer { padding: 70px 0 0; color: rgba(255,255,255,.7); background: #041426; }
    .footer-grid { display: grid; grid-template-columns: 1.35fr .7fr .8fr .95fr; gap: 55px; padding-bottom: 55px; }
    .footer-logo-shell { width: 132px; margin-bottom: 20px; padding: 8px; border-radius: 12px; background: var(--white); }
    .footer-logo-shell img { width: 100%; }
    .footer-brand p { max-width: 350px; margin-bottom: 0; font-size: .83rem; }
    .footer-column h3 { margin-bottom: 18px; color: var(--white); font-size: .83rem; letter-spacing: .11em; text-transform: uppercase; }
    .footer-links { display: grid; gap: 10px; }
    .footer-links a, .footer-links span { font-size: .82rem; }
    .footer-links a:hover { color: var(--gold-400); }
    .footer-credit-card { display: inline-flex; align-items: center; gap: 9px; margin-top: 24px; padding: 10px 13px; border: 1px solid rgba(226,186,97,.25); border-radius: 999px; color: var(--gold-400); font-size: .73rem; font-weight: 750; }
    .footer-credit-card::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--gold-400); box-shadow: 0 0 0 4px rgba(226,186,97,.1); }
    .footer-bottom { min-height: 72px; display: flex; align-items: center; justify-content: space-between; gap: 20px; border-top: 1px solid rgba(255,255,255,.1); font-size: .75rem; }
    .footer-bottom strong { color: var(--white); }

    /* Property modal */
    .property-dialog { width: min(1040px, calc(100% - 30px)); max-height: calc(100dvh - 30px); padding: 0; overflow: auto; border: 0; border-radius: 25px; color: var(--ink); background: var(--white); box-shadow: 0 35px 100px rgba(0,0,0,.35); }
    .property-dialog::backdrop { background: rgba(2,12,24,.73); backdrop-filter: blur(7px); }
    .dialog-close { position: sticky; z-index: 5; top: 14px; float: right; width: 42px; height: 42px; margin: 14px 14px -56px 0; display: grid; place-items: center; border: 1px solid rgba(255,255,255,.45); border-radius: 50%; color: var(--white); background: rgba(6,24,45,.72); cursor: pointer; backdrop-filter: blur(8px); }
    .dialog-close svg { width: 18px; height: 18px; }
    .dialog-layout { display: grid; grid-template-columns: 1.06fr .94fr; min-height: 620px; }
    .dialog-media { position: relative; min-height: 620px; background: #dde3e7; }
    .dialog-media > img { width: 100%; height: 100%; object-fit: cover; }
    .dialog-photo-note { position: absolute; left: 18px; top: 18px; padding: 8px 11px; border-radius: 9px; color: var(--white); background: rgba(6,24,45,.75); font-size: .68rem; backdrop-filter: blur(8px); }
    .dialog-thumbnails { position: absolute; left: 18px; right: 18px; bottom: 18px; display: flex; gap: 8px; overflow-x: auto; }
    .dialog-thumbnails[hidden] { display: none; }
    .dialog-thumbnail { width: 70px; height: 50px; flex: 0 0 auto; padding: 2px; overflow: hidden; border: 2px solid transparent; border-radius: 9px; background: rgba(255,255,255,.8); cursor: pointer; }
    .dialog-thumbnail.active { border-color: var(--gold-400); }
    .dialog-thumbnail img { width: 100%; height: 100%; border-radius: 5px; object-fit: cover; }
    .dialog-content { padding: 50px 42px 40px; }
    .dialog-badges { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }
    .dialog-badges .badge { box-shadow: none; }
    .dialog-badges .badge-location { background: var(--navy-900); }
    .dialog-content h2 { margin-bottom: 12px; color: var(--navy-950); font-size: clamp(2rem, 4vw, 3rem); }
    .dialog-price { margin-bottom: 20px; color: var(--gold-600); font-size: 1.55rem; font-weight: 900; }
    .dialog-meta { display: flex; flex-wrap: wrap; gap: 9px; margin-bottom: 24px; }
    .dialog-meta span { padding: 8px 11px; border: 1px solid var(--line); border-radius: 9px; color: #526074; background: #faf9f6; font-size: .77rem; font-weight: 700; }
    .dialog-description { margin-bottom: 27px; color: var(--muted); font-size: .9rem; }
    .dialog-section-title { margin-bottom: 12px; color: var(--navy-900); font-size: .79rem; font-weight: 900; letter-spacing: .09em; text-transform: uppercase; }
    .feature-list { margin: 0 0 29px; padding: 0; display: grid; grid-template-columns: 1fr 1fr; gap: 10px; list-style: none; }
    .feature-list li { position: relative; padding-left: 20px; color: #4b586b; font-size: .8rem; }
    .feature-list li::before { content: ""; position: absolute; left: 0; top: .55em; width: 8px; height: 8px; border-radius: 50%; background: var(--gold-500); }
    .availability-row { margin-bottom: 25px; padding: 12px 14px; border-left: 3px solid var(--gold-500); background: #fbf7ee; color: #556174; font-size: .78rem; }
    .dialog-actions { display: flex; flex-wrap: wrap; gap: 9px; }
    .dialog-actions .btn { font-size: .8rem; }

    /* Error page */
    .error-page { min-height: calc(100vh - var(--header-height)); display: grid; place-items: center; padding: 50px 20px; background: var(--ivory); text-align: center; }
    .error-card { width: min(560px, 100%); padding: 50px 35px; border: 1px solid var(--line); border-radius: var(--radius-lg); background: var(--white); box-shadow: var(--shadow-md); }
    .error-code { color: var(--gold-600); font-size: .8rem; font-weight: 900; letter-spacing: .16em; text-transform: uppercase; }
    .error-card h1 { margin: 10px 0 14px; color: var(--navy-950); font-size: clamp(2.2rem, 6vw, 3.8rem); }
    .error-card p { margin-bottom: 25px; color: var(--muted); }

    @media (max-width: 1060px) {
      .desktop-nav { gap: 19px; }
      .header-cta { display: none; }
      .filter-grid { grid-template-columns: repeat(3, 1fr); }
      .filter-grid .clear-btn { width: 100%; }
      .about-grid { gap: 48px; }
      .contact-shell { grid-template-columns: .8fr 1.2fr; }
      .footer-grid { grid-template-columns: 1.25fr repeat(3, .8fr); gap: 32px; }
    }

    @media (max-width: 860px) {
      :root { --header-height: 70px; --shell: min(100% - 30px, 720px); }
      .desktop-nav, .header-cta { display: none; }
      .menu-toggle { display: block; }
      .brand { min-width: 0; }
      .brand-logo { width: 48px; height: 44px; }
      .brand-name { font-size: .94rem; }
      .hero { height: 850px; min-height: 850px; }
      .hero-inner { justify-content: center; padding-block: 45px 80px; }
      .hero-content { width: calc(100% - 55px); }
      .hero-content h1 { font-size: clamp(2.75rem, 9vw, 4.6rem); }
      .hero-search { grid-template-columns: 1fr 1fr; margin-top: 35px; }
      .hero-search .search-field { border-right: 0; border-bottom: 1px solid #e6e8eb; }
      .hero-search .btn { grid-column: 1 / -1; }
      .executive-grid { grid-template-columns: 1.1fr .7fr; gap: 28px; }
      .executive-copy h2 { font-size: clamp(2.7rem, 7vw, 4.2rem); }
      .portrait-plaque { right: -5px; bottom: 22px; }
      .hero-controls { right: 15px; }
      .trust-grid { grid-template-columns: repeat(3, 1fr); }
      .trust-intro { grid-column: 1 / -1; min-height: 64px; padding: 18px 0 10px; }
      .trust-item { padding: 18px 15px 24px; }
      .trust-item:first-of-type { border-left: 0; }
      .properties-section, .about-section, .contact-section { padding-block: 88px; }
      .section-top { align-items: start; flex-direction: column; }
      .listing-note { max-width: 100%; }
      .about-grid { grid-template-columns: 1fr; gap: 55px; }
      .about-visual { max-width: 560px; margin-inline: auto; }
      .about-image-shell img { aspect-ratio: 5 / 4; }
      .executive-profile { grid-template-columns: 210px 1fr; }
      .executive-profile-copy { padding: 36px; }
      .contact-shell { grid-template-columns: 1fr; }
      .contact-info, .contact-form-wrap { padding: 45px 38px; }
      .footer-grid { grid-template-columns: 1.3fr 1fr 1fr; }
      .footer-brand { grid-column: 1 / -1; }
      .dialog-layout { grid-template-columns: 1fr; }
      .dialog-media { min-height: 350px; height: 42vh; }
    }

    @media (max-width: 620px) {
      :root { --shell: calc(100% - 24px); }
      .brand-copy { display: none; }
      .hero { height: 900px; min-height: 900px; }
      .hero-bg { object-position: 58% center; }
      .hero-shade { background: linear-gradient(90deg, rgba(4,18,34,.95), rgba(4,18,34,.72)); }
      .hero-inner { padding-block: 28px 82px; }
      .hero-content { width: 100%; }
      .hero-content h1 { margin-bottom: 17px; font-size: clamp(2.55rem, 12vw, 3.55rem); }
      .hero-content > p { margin-bottom: 23px; font-size: .97rem; }
      .hero-actions { gap: 8px; }
      .hero-actions .btn { min-height: 46px; padding-inline: 15px; font-size: .78rem; }
      .hero-search { grid-template-columns: 1fr; gap: 5px; margin-top: 28px; padding: 10px; }
      .hero-search .btn { grid-column: auto; }
      .search-field { padding-inline: 10px; }
      .search-field select { height: 31px; }
      .hero-note { font-size: .72rem; }
      .hero-controls { top: auto; right: 50%; bottom: 17px; display: flex; align-items: center; transform: translateX(50%); }
      .slide-dots { order: 2; }
      .hero-arrow:first-child { order: 1; }
      .hero-arrow:last-child { order: 3; }
      .executive-grid { display: flex; flex-direction: column; justify-content: center; align-items: stretch; gap: 25px; padding: 28px 0 75px; }
      .executive-copy { order: 1; }
      .executive-copy h2 { margin-bottom: 14px; font-size: 2.65rem; }
      .executive-copy > p { margin-bottom: 20px; font-size: .9rem; }
      .executive-id { padding-top: 16px; }
      .portrait-wrap { order: 2; width: 220px; justify-self: auto; align-self: center; }
      .portrait-frame { padding: 7px; }
      .portrait-frame img { aspect-ratio: 1 / 1.12; }
      .portrait-plaque { right: -64px; bottom: 16px; max-width: 185px; padding: 10px 12px; }
      .portrait-plaque strong { font-size: .77rem; }
      .portrait-plaque span { font-size: .65rem; }
      .trust-grid { grid-template-columns: 1fr; padding-block: 12px; }
      .trust-intro { min-height: 0; padding: 8px 4px 14px; }
      .trust-item { border-left: 0; border-top: 1px solid var(--line); padding: 15px 4px; }
      .properties-section, .about-section, .contact-section { padding-block: 70px; }
      .section-heading h2 { font-size: 2.45rem; }
      .filter-grid { grid-template-columns: 1fr 1fr; }
      .filter-grid .form-field:nth-child(5), .filter-grid .clear-btn { grid-column: 1 / -1; }
      .filters-panel { padding: 15px; }
      .results-row { align-items: start; flex-direction: column; gap: 3px; }
      .property-body { padding: 20px; }
      .property-actions { grid-template-columns: 1fr; }
      .about-visual { padding: 0 20px 34px 0; }
      .about-image-shell { border-radius: 20px 60px 20px 20px; }
      .about-image-shell img { aspect-ratio: 4 / 4.5; }
      .location-card { right: 0; bottom: 58px; width: 174px; padding: 15px; }
      .mission-grid { grid-template-columns: 1fr; }
      .executive-profile { grid-template-columns: 1fr; }
      .executive-profile-image { min-height: 320px; height: 320px; }
      .executive-profile-copy { padding: 30px 24px; }
      .contact-info, .contact-form-wrap { padding: 36px 22px; }
      .contact-form { grid-template-columns: 1fr; }
      .contact-form .full-width { grid-column: auto; }
      .form-footer { grid-column: auto; align-items: stretch; flex-direction: column; }
      .form-footer .btn { width: 100%; }
      .footer-grid { grid-template-columns: 1fr 1fr; gap: 38px 25px; }
      .footer-brand { grid-column: 1 / -1; }
      .footer-column:last-child { grid-column: 1 / -1; }
      .footer-bottom { padding-block: 20px; align-items: flex-start; flex-direction: column; gap: 5px; }
      .property-dialog { width: calc(100% - 16px); max-height: calc(100dvh - 16px); border-radius: 18px; }
      .dialog-media { min-height: 250px; height: 34vh; }
      .dialog-content { padding: 35px 22px 28px; }
      .feature-list { grid-template-columns: 1fr; }
      .dialog-actions { display: grid; }
    }

    @media (prefers-reduced-motion: reduce) {
      html { scroll-behavior: auto; }
      *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; }
    }
  </style>
</head>
<body>
  <a class="skip-link" href="#main-content">Skip to main content</a>

  <header class="site-header" id="siteHeader">
    <div class="shell nav-wrap">
      <a class="brand" href="{{ url_for('home') }}#home" aria-label="Walhad Housing Solutions home">
        <img class="brand-logo" src="{{ url_for('static', filename='images/walhad-logo.jpg') }}" alt="Walhad Housing Solutions logo" width="56" height="51">
        <span class="brand-copy">
          <span class="brand-name">Walhad Housing</span>
          <span class="brand-subtitle">Solutions</span>
        </span>
      </a>

      <nav class="desktop-nav" aria-label="Primary navigation">
        <a class="nav-link active" href="{{ url_for('home') }}#home" data-section-link="home">Home</a>
        <a class="nav-link" href="{{ url_for('home') }}#properties" data-section-link="properties">Properties</a>
        <a class="nav-link" href="{{ url_for('home') }}#about" data-section-link="about">About Us</a>
        <a class="nav-link" href="{{ url_for('home') }}#contact" data-section-link="contact">Contact</a>
      </nav>

      <div class="nav-actions">
        <a class="btn btn-navy header-cta" href="{{ url_for('home') }}#properties">Explore Properties</a>
        <button class="menu-toggle" id="menuToggle" type="button" aria-expanded="false" aria-controls="mobileMenu" aria-label="Open navigation menu">
          <span></span><span></span><span></span>
        </button>
      </div>
    </div>
  </header>

  <div class="menu-scrim" id="menuScrim" hidden></div>
  <nav class="mobile-menu" id="mobileMenu" aria-label="Mobile navigation" aria-hidden="true">
    <a href="{{ url_for('home') }}#home">Home</a>
    <a href="{{ url_for('home') }}#properties">Properties</a>
    <a href="{{ url_for('home') }}#about">About Us</a>
    <a href="{{ url_for('home') }}#contact">Contact</a>
    <a class="btn btn-primary" href="{{ url_for('home') }}#properties">Explore Properties</a>
  </nav>

  <main id="main-content">
    <section class="hero" id="home" aria-label="Featured content">
      <div class="hero-slides" id="heroSlides" aria-live="off">
        <article class="hero-slide active" data-slide="0" aria-hidden="false">
          <img class="hero-bg image-fallback" src="{{ url_for('static', filename='images/hodan-hills-3-bedroom.jpg') }}" alt="Residential home available through Walhad Housing Solutions" fetchpriority="high">
          <div class="hero-shade"></div>
          <div class="shell hero-inner">
            <div class="hero-content">
              <span class="eyebrow eyebrow-light">Homes across Somaliland</span>
              <h1>Find Your Perfect Home in Somaliland</h1>
              <p>Discover quality houses and apartments for rent and sale in Borama, Hargeisa, and Gabiley.</p>
              <div class="hero-actions">
                <a class="btn btn-primary" href="#properties">
                  Explore Properties
                  <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
                </a>
                <a class="btn btn-outline" href="#contact" data-inquiry-link="List a Property">List Your Property</a>
              </div>
            </div>

            <form class="hero-search" id="heroSearch" method="get" action="{{ url_for('home') }}#properties" aria-label="Search properties">
              <div class="search-field">
                <label for="heroLocation">Location</label>
                <select id="heroLocation" name="location">
                  <option value="all">All Locations</option>
                  <option value="borama">Borama</option>
                  <option value="hargeisa">Hargeisa</option>
                  <option value="gabiley">Gabiley</option>
                </select>
              </div>
              <div class="search-field">
                <label for="heroPurpose">Purpose</label>
                <select id="heroPurpose" name="purpose">
                  <option value="all">Rent or Buy</option>
                  <option value="rent">Rent</option>
                  <option value="sale">Buy</option>
                </select>
              </div>
              <div class="search-field">
                <label for="heroType">Property type</label>
                <select id="heroType" name="type">
                  <option value="all">All Property Types</option>
                  <option value="house">House</option>
                  <option value="apartment">Apartment</option>
                  <option value="villa">Villa</option>
                  <option value="commercial">Commercial</option>
                </select>
              </div>
              <button class="btn btn-navy" type="submit">
                <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>
                Search
              </button>
            </form>
            <div class="hero-note">
              <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21s7-5.2 7-12A7 7 0 0 0 5 9c0 6.8 7 12 7 12Z"/><circle cx="12" cy="9" r="2.5"/></svg>
              Locally focused property support in Borama, Hargeisa, and Gabiley.
            </div>
          </div>
        </article>

        <article class="hero-slide executive-slide" data-slide="1" aria-hidden="true" inert>
          <div class="shell executive-grid">
            <div class="executive-copy">
              <span class="eyebrow eyebrow-light">Leadership &amp; vision</span>
              <h2>Leadership Built on Trust. Homes Built for Life.</h2>
              <p>Committed to connecting people with quality properties through integrity, professionalism, and local expertise.</p>
              <div class="executive-id">
                <span class="executive-monogram" aria-hidden="true">MS</span>
                <span>
                  <span class="executive-name">Mohamed Said Ahmed</span>
                  <span class="executive-role">Chief Executive Officer · Walhad Housing Solutions</span>
                </span>
              </div>
            </div>
            <div class="portrait-wrap">
              <div class="portrait-frame">
                <img class="image-fallback" src="{{ url_for('static', filename='images/mohamed-said-ahmed.jpg') }}" alt="Mohamed Said Ahmed, Chief Executive Officer" width="600" height="600">
              </div>
              <div class="portrait-plaque">
                <strong>Mohamed Said Ahmed</strong>
                <span>Chief Executive Officer</span>
              </div>
            </div>
          </div>
        </article>
      </div>

      <div class="hero-controls" aria-label="Hero carousel controls">
        <button class="hero-arrow" id="heroPrev" type="button" aria-label="Previous slide">
          <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m15 18-6-6 6-6"/></svg>
        </button>
        <div class="slide-dots" role="tablist" aria-label="Choose hero slide">
          <button class="slide-dot active" type="button" data-slide-target="0" role="tab" aria-selected="true" aria-label="Property search slide"></button>
          <button class="slide-dot" type="button" data-slide-target="1" role="tab" aria-selected="false" aria-label="Chief executive slide"></button>
        </div>
        <button class="hero-arrow" id="heroNext" type="button" aria-label="Next slide">
          <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m9 18 6-6-6-6"/></svg>
        </button>
      </div>
    </section>

    <section class="trust-strip" aria-label="Service locations">
      <div class="shell trust-grid">
        <div class="trust-intro">Local knowledge. Clear guidance.</div>
        {% for location in locations %}
        <div class="trust-item">
          <span class="trust-icon">
            <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 21s7-5.2 7-12A7 7 0 0 0 5 9c0 6.8 7 12 7 12Z"/><circle cx="12" cy="9" r="2.5"/></svg>
          </span>
          <span><strong>{{ location }}</strong><span>Properties &amp; inquiries</span></span>
        </div>
        {% endfor %}
      </div>
    </section>

    <section class="properties-section" id="properties">
      <div class="shell">
        <div class="section-top">
          <div class="section-heading">
            <span class="eyebrow">Curated opportunities</span>
            <h2>Explore Our Properties</h2>
            <p>Find the right property for your lifestyle, family, or investment.</p>
          </div>
          <div class="listing-note">Current cards use only details visible in the supplied listings. Contact Walhad to confirm all information and availability.</div>
        </div>

        <div class="filters-panel" aria-label="Property filters">
          <div class="filter-grid">
            <div class="form-field">
              <label for="filterLocation">Location</label>
              <select class="form-control" id="filterLocation">
                <option value="all">All Locations</option>
                <option value="borama">Borama</option>
                <option value="hargeisa">Hargeisa</option>
                <option value="gabiley">Gabiley</option>
              </select>
            </div>
            <div class="form-field">
              <label for="filterPurpose">Purpose</label>
              <select class="form-control" id="filterPurpose">
                <option value="all">All</option>
                <option value="rent">For Rent</option>
                <option value="sale">For Sale</option>
              </select>
            </div>
            <div class="form-field">
              <label for="filterType">Property type</label>
              <select class="form-control" id="filterType">
                <option value="all">All</option>
                <option value="house">House</option>
                <option value="apartment">Apartment</option>
                <option value="villa">Villa</option>
                <option value="commercial">Commercial</option>
              </select>
            </div>
            <div class="form-field">
              <label for="filterBedrooms">Bedrooms</label>
              <select class="form-control" id="filterBedrooms">
                <option value="0">Any</option>
                <option value="1">1+</option>
                <option value="2">2+</option>
                <option value="3">3+</option>
                <option value="4">4+</option>
              </select>
            </div>
            <div class="form-field">
              <label for="filterPrice">Maximum price</label>
              <select class="form-control" id="filterPrice">
                <option value="0">Any price</option>
                <option value="200">Up to $200</option>
                <option value="400">Up to $400</option>
                <option value="700">Up to $700</option>
                <option value="1000">Up to $1,000</option>
              </select>
            </div>
            <button class="clear-btn" id="clearFilters" type="button">Clear Filters</button>
          </div>
        </div>

        <div class="results-row">
          <div class="results-count" id="resultsCount" aria-live="polite">Showing {{ properties|length }} properties</div>
          <div class="results-hint">Prices and availability should be reconfirmed before making a decision.</div>
        </div>

        <div class="property-grid" id="propertyGrid">
          {% for property in properties %}
          <article class="property-card"
                   data-property-id="{{ property.id }}"
                   data-location="{{ property.location|lower }}"
                   data-purpose="{{ property.purpose }}"
                   data-type="{{ property.property_type|lower }}"
                   data-bedrooms="{{ property.bedrooms or 0 }}"
                   data-price="{{ property.price if property.price is not none else 0 }}">
            <div class="property-media">
              <img class="image-fallback" src="{{ property.images[0] }}" alt="Exterior of {{ property.title }}" loading="lazy" width="967" height="413">
              <div class="property-badges">
                <span class="badge badge-purpose">{{ property.purpose_label }}</span>
                <span class="badge badge-location">{{ property.location }}</span>
              </div>
            </div>
            <div class="property-body">
              <div class="property-ref">{{ property.reference }} · {{ property.area }}</div>
              <h3 class="property-title">{{ property.title }}</h3>
              <div class="property-price">{{ property.price_display }}</div>
              <div class="property-facts" aria-label="Property facts">
                <span class="property-fact">
                  <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 18v-7h18v7M5 11V7h5a3 3 0 0 1 3 3v1M3 18v3M21 18v3M3 15h18"/></svg>
                  {{ property.bedrooms }} beds
                </span>
                <span class="property-fact">
                  <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 13h16v3a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4v-3ZM7 13V6a3 3 0 0 1 6 0"/><path d="M7 20v2M17 20v2"/></svg>
                  {{ property.bathrooms }} baths
                </span>
                <span class="property-fact">
                  <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m3 11 9-8 9 8M5 10v11h14V10M9 21v-7h6v7"/></svg>
                  {{ property.property_type }}
                </span>
              </div>
              <p class="property-description">{{ property.description }}</p>
              <div class="property-actions">
                <a class="btn btn-ghost details-link" href="{{ url_for('property_detail', property_id=property.id) }}" data-property-id="{{ property.id }}">View Details</a>
                <a class="btn btn-navy contact-property" href="#contact" data-reference="{{ property.reference }}" data-purpose="{{ 'Rent' if property.purpose == 'rent' else 'Buy' }}">Contact About This Property</a>
              </div>
            </div>
          </article>
          {% endfor %}
        </div>

        <div class="no-results" id="noResults" role="status">
          <div class="no-results-icon">
            <svg aria-hidden="true" width="27" height="27" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4M8.5 11h5"/></svg>
          </div>
          <h3>No matching properties yet</h3>
          <p>Try broadening your filters, or contact us and tell us what you need.</p>
          <button class="btn btn-navy" id="noResultsClear" type="button">Clear Filters</button>
        </div>
      </div>
    </section>

    <section class="about-section" id="about">
      <div class="shell">
        <div class="about-grid">
          <div class="about-visual">
            <div class="about-image-shell">
              <img class="image-fallback" src="{{ url_for('static', filename='images/hodan-hills-5-bedroom.jpg') }}" alt="Residential property represented by Walhad Housing Solutions" loading="lazy" width="968" height="411">
            </div>
            <div class="location-card">
              <small>Serving</small>
              <strong>Borama · Hargeisa · Gabiley</strong>
              <div class="location-dots" aria-hidden="true"><span></span><span></span><span></span></div>
            </div>
          </div>

          <div class="about-copy">
            <div class="section-heading">
              <span class="eyebrow">About Walhad</span>
              <h2>Your Trusted Property Partner</h2>
            </div>
            <p>Walhad Housing Solutions helps people discover residential properties for rent and sale in Borama, Hargeisa, and Gabiley. Our focus is on providing a professional, transparent, and convenient property-search experience while connecting clients with suitable housing opportunities.</p>
            <div class="mission-grid">
              <article class="purpose-card">
                <span class="card-number">01 · OUR MISSION</span>
                <h3>Make property discovery clearer</h3>
                <p>Present relevant opportunities professionally and help clients move from search to informed inquiry.</p>
              </article>
              <article class="purpose-card">
                <span class="card-number">02 · OUR VISION</span>
                <h3>Build confidence in every search</h3>
                <p>Create a trusted, locally focused property experience for the communities we serve.</p>
              </article>
            </div>
            <div class="values-label">Our values</div>
            <div class="value-list" aria-label="Company values">
              <span class="value-pill">Integrity</span>
              <span class="value-pill">Professionalism</span>
              <span class="value-pill">Transparency</span>
              <span class="value-pill">Local expertise</span>
              <span class="value-pill">Client care</span>
            </div>
          </div>
        </div>

        <article class="executive-profile">
          <div class="executive-profile-image">
            <img class="image-fallback" src="{{ url_for('static', filename='images/mohamed-said-ahmed.jpg') }}" alt="Mohamed Said Ahmed" loading="lazy" width="600" height="600">
          </div>
          <div class="executive-profile-copy">
            <span class="eyebrow eyebrow-light">Executive profile</span>
            <h3>Mohamed Said Ahmed</h3>
            <div class="executive-profile-role">Chief Executive Officer</div>
            <p>As Chief Executive Officer, Mohamed Said Ahmed leads Walhad Housing Solutions’ commitment to integrity, professional service, and locally informed property support.</p>
          </div>
        </article>
      </div>
    </section>

    <section class="contact-section" id="contact">
      <div class="shell contact-shell">
        <div class="contact-info">
          <span class="eyebrow eyebrow-light">Start a conversation</span>
          <h2>Let’s Find Your Next Place.</h2>
          <p>Tell us what you are looking for, where you want to live, or which property you would like to list.</p>
          <div class="contact-list">
            <div class="contact-item">
              <span class="contact-item-icon">
                <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 4h16v16H4zM4 7l8 6 8-6"/></svg>
              </span>
              <span><small>Business email</small><strong>{{ company.email or 'Not yet provided' }}</strong></span>
            </div>
            <div class="contact-item">
              <span class="contact-item-icon">
                <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M7 3h4l2 5-3 2a15 15 0 0 0 4 4l2-3 5 2v4a4 4 0 0 1-4 4C9.3 20.5 3.5 14.7 3 7a4 4 0 0 1 4-4Z"/></svg>
              </span>
              <span><small>Business phone</small><strong>{{ company.phone or 'Not yet provided' }}</strong></span>
            </div>
            <div class="contact-item">
              <span class="contact-item-icon">
                <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 21s7-5.2 7-12A7 7 0 0 0 5 9c0 6.8 7 12 7 12Z"/><circle cx="12" cy="9" r="2.5"/></svg>
              </span>
              <span><small>Areas served</small><strong>Borama · Hargeisa · Gabiley</strong></span>
            </div>
          </div>
          {% if demo_mode %}
          <div class="demo-note"><strong>Demo mode:</strong> the form validates your inquiry but does not send it until a secure contact webhook is configured.</div>
          {% else %}
          <div class="demo-note"><strong>Secure inquiry form:</strong> submitted details are sent to the company’s configured contact service.</div>
          {% endif %}
        </div>

        <div class="contact-form-wrap">
          <div class="contact-form-heading">
            <h3>Send an inquiry</h3>
            <p>Required fields are marked with an asterisk.</p>
          </div>
          <form class="contact-form" id="contactForm" action="{{ url_for('submit_contact') }}" method="post" novalidate>
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <div class="hp-field" aria-hidden="true">
              <label for="website">Leave this field empty</label>
              <input id="website" name="website" type="text" tabindex="-1" autocomplete="off">
            </div>
            <div class="form-field">
              <label for="fullName">Full name *</label>
              <input class="form-control" id="fullName" name="full_name" type="text" autocomplete="name" maxlength="100" required>
              <p class="field-error" id="full_nameError"></p>
            </div>
            <div class="form-field">
              <label for="email">Email address *</label>
              <input class="form-control" id="email" name="email" type="email" autocomplete="email" maxlength="254" required>
              <p class="field-error" id="emailError"></p>
            </div>
            <div class="form-field">
              <label for="phone">Phone number *</label>
              <input class="form-control" id="phone" name="phone" type="tel" autocomplete="tel" maxlength="30" required>
              <p class="field-error" id="phoneError"></p>
            </div>
            <div class="form-field">
              <label for="contactLocation">Location *</label>
              <select class="form-control" id="contactLocation" name="location" required>
                <option value="">Choose a location</option>
                <option value="Borama">Borama</option>
                <option value="Hargeisa">Hargeisa</option>
                <option value="Gabiley">Gabiley</option>
              </select>
              <p class="field-error" id="locationError"></p>
            </div>
            <div class="form-field">
              <label for="inquiryType">Inquiry type *</label>
              <select class="form-control" id="inquiryType" name="inquiry_type" required>
                <option value="">Choose inquiry type</option>
                <option value="Rent">Rent</option>
                <option value="Buy">Buy</option>
                <option value="List a Property">List a Property</option>
                <option value="General Inquiry">General Inquiry</option>
              </select>
              <p class="field-error" id="inquiry_typeError"></p>
            </div>
            <div class="form-field">
              <label for="propertyReference">Property reference <span class="sr-only">optional</span></label>
              <input class="form-control" id="propertyReference" name="property_reference" type="text" maxlength="80" placeholder="Optional">
              <p class="field-error" id="property_referenceError"></p>
            </div>
            <div class="form-field full-width">
              <label for="message">Message *</label>
              <textarea class="form-control" id="message" name="message" minlength="20" maxlength="2000" required placeholder="Tell us what you need, your preferred area, and any questions you have."></textarea>
              <p class="field-error" id="messageError"></p>
            </div>
            <div class="form-footer">
              <p class="form-status" id="formStatus" role="status" aria-live="polite"></p>
              <button class="btn btn-navy submit-btn" id="submitButton" type="submit">
                <span>Submit Inquiry</span>
                <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
              </button>
            </div>
          </form>
        </div>
      </div>
    </section>
  </main>

  <footer class="site-footer">
    <div class="shell">
      <div class="footer-grid">
        <div class="footer-brand">
          <div class="footer-logo-shell">
            <img src="{{ url_for('static', filename='images/walhad-logo.jpg') }}" alt="Walhad Housing Solutions" width="132" height="111" loading="lazy">
          </div>
          <p>A professional, locally focused property-search experience for residential opportunities in Somaliland.</p>
          <div class="footer-credit-card">Powered by Mohamed Said Ahmed</div>
        </div>
        <div class="footer-column">
          <h3>Quick links</h3>
          <div class="footer-links">
            <a href="#home">Home</a>
            <a href="#properties">Properties</a>
            <a href="#about">About Us</a>
            <a href="#contact">Contact</a>
          </div>
        </div>
        <div class="footer-column">
          <h3>Locations</h3>
          <div class="footer-links">
            <span>Borama</span><span>Hargeisa</span><span>Gabiley</span>
          </div>
        </div>
        <div class="footer-column">
          <h3>Contact</h3>
          <div class="footer-links">
            {% if company.email %}<a href="mailto:{{ company.email }}">{{ company.email }}</a>{% else %}<span>Email: not yet provided</span>{% endif %}
            {% if company.phone %}<a href="tel:{{ company.phone|replace(' ', '') }}">{{ company.phone }}</a>{% else %}<span>Phone: not yet provided</span>{% endif %}
            {% if company.address %}<span>{{ company.address }}</span>{% endif %}
            {% if company.whatsapp %}<a href="{{ site_config.whatsappUrl }}" target="_blank" rel="noopener noreferrer">WhatsApp</a>{% endif %}
            {% if company.facebook %}<a href="{{ company.facebook }}" target="_blank" rel="noopener noreferrer">Facebook</a>{% endif %}
            {% if company.instagram %}<a href="{{ company.instagram }}" target="_blank" rel="noopener noreferrer">Instagram</a>{% endif %}
            {% if company.linkedin %}<a href="{{ company.linkedin }}" target="_blank" rel="noopener noreferrer">LinkedIn</a>{% endif %}
          </div>
        </div>
      </div>
      <div class="footer-bottom">
        <span>© {{ current_year }} Walhad Housing Solutions. All rights reserved.</span>
        <span><strong>Powered by Mohamed Said Ahmed</strong></span>
      </div>
    </div>
  </footer>

  <dialog class="property-dialog" id="propertyModal" aria-labelledby="modalTitle">
    <button class="dialog-close" id="modalClose" type="button" aria-label="Close property details">
      <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 6l12 12M18 6 6 18"/></svg>
    </button>
    <div class="dialog-layout">
      <div class="dialog-media">
        <img id="modalImage" src="{{ url_for('fallback_property_image') }}" alt="">
        <div class="dialog-photo-note">Supplied property photograph</div>
        <div class="dialog-thumbnails" id="modalGallery" aria-label="Property photo gallery" hidden></div>
      </div>
      <div class="dialog-content">
        <div class="dialog-badges">
          <span class="badge badge-purpose" id="modalPurpose"></span>
          <span class="badge badge-location" id="modalLocation"></span>
        </div>
        <div class="property-ref" id="modalReference"></div>
        <h2 id="modalTitle"></h2>
        <div class="dialog-price" id="modalPrice"></div>
        <div class="dialog-meta" id="modalMeta"></div>
        <p class="dialog-description" id="modalDescription"></p>
        <div class="dialog-section-title">Property features</div>
        <ul class="feature-list" id="modalFeatures"></ul>
        <div class="availability-row" id="modalAvailability"></div>
        <div class="dialog-actions">
          <button class="btn btn-navy" id="modalContact" type="button">Contact About This Property</button>
          {% if company.whatsapp %}
          <a class="btn btn-ghost" id="modalWhatsapp" href="#" target="_blank" rel="noopener noreferrer">WhatsApp</a>
          {% endif %}
        </div>
      </div>
    </div>
  </dialog>

  <script id="propertyData" type="application/json" nonce="{{ csp_nonce }}">{{ properties|tojson }}</script>
  <script id="siteConfig" type="application/json" nonce="{{ csp_nonce }}">{{ site_config|tojson }}</script>
  <script nonce="{{ csp_nonce }}">
    (() => {
      "use strict";

      const $ = (selector, context = document) => context.querySelector(selector);
      const $$ = (selector, context = document) => [...context.querySelectorAll(selector)];
      const propertyData = JSON.parse($("#propertyData").textContent);
      const propertyMap = new Map(propertyData.map(item => [item.id, item]));
      const siteConfig = JSON.parse($("#siteConfig").textContent);
      const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

      // Image fallback for any missing or moved asset.
      $$("img.image-fallback").forEach(image => {
        const replaceBrokenImage = () => {
          if (image.dataset.fallbackApplied) return;
          image.dataset.fallbackApplied = "true";
          image.src = siteConfig.fallbackImage;
        };
        image.addEventListener("error", replaceBrokenImage, { once: true });
        if (image.complete && image.naturalWidth === 0) replaceBrokenImage();
      });

      // Sticky header and responsive menu.
      const header = $("#siteHeader");
      const menuToggle = $("#menuToggle");
      const mobileMenu = $("#mobileMenu");
      const menuScrim = $("#menuScrim");

      const closeMenu = () => {
        menuToggle.setAttribute("aria-expanded", "false");
        menuToggle.setAttribute("aria-label", "Open navigation menu");
        mobileMenu.classList.remove("open");
        mobileMenu.setAttribute("aria-hidden", "true");
        menuScrim.classList.remove("open");
        window.setTimeout(() => { if (!menuScrim.classList.contains("open")) menuScrim.hidden = true; }, 260);
        document.body.classList.remove("menu-open");
      };
      const openMenu = () => {
        menuScrim.hidden = false;
        requestAnimationFrame(() => menuScrim.classList.add("open"));
        mobileMenu.classList.add("open");
        mobileMenu.setAttribute("aria-hidden", "false");
        menuToggle.setAttribute("aria-expanded", "true");
        menuToggle.setAttribute("aria-label", "Close navigation menu");
        document.body.classList.add("menu-open");
      };
      menuToggle.addEventListener("click", () => menuToggle.getAttribute("aria-expanded") === "true" ? closeMenu() : openMenu());
      menuScrim.addEventListener("click", closeMenu);
      $$("a", mobileMenu).forEach(link => link.addEventListener("click", closeMenu));
      document.addEventListener("keydown", event => { if (event.key === "Escape") closeMenu(); });
      window.addEventListener("resize", () => { if (window.innerWidth > 860) closeMenu(); }, { passive: true });
      const updateHeader = () => header.classList.toggle("scrolled", window.scrollY > 12);
      updateHeader();
      window.addEventListener("scroll", updateHeader, { passive: true });

      // Active navigation section.
      const navLinks = $$("[data-section-link]");
      const observedSections = ["home", "properties", "about", "contact"].map(id => document.getElementById(id));
      if ("IntersectionObserver" in window) {
        const sectionObserver = new IntersectionObserver(entries => {
          const visible = entries.filter(entry => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
          if (!visible) return;
          navLinks.forEach(link => link.classList.toggle("active", link.dataset.sectionLink === visible.target.id));
        }, { rootMargin: "-20% 0px -64%", threshold: [0, .15, .35] });
        observedSections.forEach(section => sectionObserver.observe(section));
      }

      // Two-slide, 10-second hero carousel. Reduced-motion users control it manually.
      const hero = $("#home");
      const slides = $$(".hero-slide");
      const dots = $$(".slide-dot");
      let activeSlide = 0;
      let heroTimer = null;
      const showSlide = index => {
        activeSlide = (index + slides.length) % slides.length;
        slides.forEach((slide, i) => {
          const active = i === activeSlide;
          slide.classList.toggle("active", active);
          slide.setAttribute("aria-hidden", active ? "false" : "true");
          if (active) slide.removeAttribute("inert"); else slide.setAttribute("inert", "");
        });
        dots.forEach((dot, i) => {
          const active = i === activeSlide;
          dot.classList.toggle("active", active);
          dot.setAttribute("aria-selected", String(active));
        });
      };
      const stopCarousel = () => { if (heroTimer) window.clearInterval(heroTimer); heroTimer = null; };
      const startCarousel = () => {
        stopCarousel();
        if (!reduceMotion && !document.hidden) heroTimer = window.setInterval(() => showSlide(activeSlide + 1), 10000);
      };
      $("#heroPrev").addEventListener("click", () => { showSlide(activeSlide - 1); startCarousel(); });
      $("#heroNext").addEventListener("click", () => { showSlide(activeSlide + 1); startCarousel(); });
      dots.forEach(dot => dot.addEventListener("click", () => { showSlide(Number(dot.dataset.slideTarget)); startCarousel(); }));
      hero.addEventListener("mouseenter", stopCarousel);
      hero.addEventListener("mouseleave", startCarousel);
      hero.addEventListener("focusin", stopCarousel);
      hero.addEventListener("focusout", event => { if (!hero.contains(event.relatedTarget)) startCarousel(); });
      document.addEventListener("visibilitychange", () => document.hidden ? stopCarousel() : startCarousel());
      showSlide(0);
      startCarousel();

      // Property filtering, including the hero search.
      const filterControls = {
        location: $("#filterLocation"),
        purpose: $("#filterPurpose"),
        type: $("#filterType"),
        bedrooms: $("#filterBedrooms"),
        price: $("#filterPrice")
      };
      const propertyCards = $$(".property-card");
      const resultsCount = $("#resultsCount");
      const noResults = $("#noResults");

      const applyFilters = () => {
        const selected = {
          location: filterControls.location.value,
          purpose: filterControls.purpose.value,
          type: filterControls.type.value,
          bedrooms: Number(filterControls.bedrooms.value),
          price: Number(filterControls.price.value)
        };
        let count = 0;
        propertyCards.forEach(card => {
          const matches =
            (selected.location === "all" || card.dataset.location === selected.location) &&
            (selected.purpose === "all" || card.dataset.purpose === selected.purpose) &&
            (selected.type === "all" || card.dataset.type === selected.type) &&
            Number(card.dataset.bedrooms) >= selected.bedrooms &&
            (selected.price === 0 || (Number(card.dataset.price) > 0 && Number(card.dataset.price) <= selected.price));
          card.hidden = !matches;
          if (matches) count += 1;
        });
        resultsCount.textContent = `Showing ${count} ${count === 1 ? "property" : "properties"}`;
        noResults.classList.toggle("show", count === 0);
        $("#propertyGrid").hidden = count === 0;
      };

      const resetFilters = () => {
        Object.values(filterControls).forEach(control => control.selectedIndex = 0);
        applyFilters();
        const cleanUrl = `${window.location.pathname}#properties`;
        if (window.location.pathname === "/") history.replaceState(null, "", cleanUrl);
      };
      Object.values(filterControls).forEach(control => control.addEventListener("change", applyFilters));
      $("#clearFilters").addEventListener("click", resetFilters);
      $("#noResultsClear").addEventListener("click", resetFilters);

      $("#heroSearch").addEventListener("submit", event => {
        event.preventDefault();
        filterControls.location.value = $("#heroLocation").value;
        filterControls.purpose.value = $("#heroPurpose").value;
        filterControls.type.value = $("#heroType").value;
        filterControls.bedrooms.value = "0";
        filterControls.price.value = "0";
        applyFilters();
        const params = new URLSearchParams();
        if (filterControls.location.value !== "all") params.set("location", filterControls.location.value);
        if (filterControls.purpose.value !== "all") params.set("purpose", filterControls.purpose.value);
        if (filterControls.type.value !== "all") params.set("type", filterControls.type.value);
        history.replaceState(null, "", `/?${params.toString()}#properties`);
        $("#properties").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
      });

      const initialParams = new URLSearchParams(window.location.search);
      ["location", "purpose", "type"].forEach(key => {
        const value = initialParams.get(key);
        const control = filterControls[key];
        if (value && [...control.options].some(option => option.value === value)) control.value = value;
      });
      applyFilters();

      // Accessible property detail dialog with direct-link fallback route.
      const modal = $("#propertyModal");
      const modalImage = $("#modalImage");
      let activePropertyId = null;

      const openProperty = (id, updateHistory = true) => {
        const property = propertyMap.get(id);
        if (!property) return;
        activePropertyId = id;
        modalImage.src = property.images[0] || siteConfig.fallbackImage;
        modalImage.alt = `Exterior of ${property.title}`;
        modalImage.onerror = () => { modalImage.onerror = null; modalImage.src = siteConfig.fallbackImage; };
        const gallery = $("#modalGallery");
        gallery.replaceChildren();
        gallery.hidden = property.images.length <= 1;
        if (property.images.length > 1) {
          property.images.forEach((source, index) => {
            const thumbnail = document.createElement("button");
            thumbnail.type = "button";
            thumbnail.className = `dialog-thumbnail${index === 0 ? " active" : ""}`;
            thumbnail.setAttribute("aria-label", `View photo ${index + 1} of ${property.images.length}`);
            const image = document.createElement("img");
            image.src = source;
            image.alt = "";
            image.addEventListener("error", () => { image.src = siteConfig.fallbackImage; }, { once: true });
            thumbnail.appendChild(image);
            thumbnail.addEventListener("click", () => {
              modalImage.src = source;
              modalImage.alt = `${property.title}, photo ${index + 1}`;
              $$(".dialog-thumbnail", gallery).forEach(item => item.classList.remove("active"));
              thumbnail.classList.add("active");
            });
            gallery.appendChild(thumbnail);
          });
        }
        $("#modalPurpose").textContent = property.purpose_label;
        $("#modalLocation").textContent = property.location;
        $("#modalReference").textContent = `${property.reference} · ${property.area}`;
        $("#modalTitle").textContent = property.title;
        $("#modalPrice").textContent = property.price_display;
        $("#modalDescription").textContent = property.description;
        $("#modalAvailability").textContent = `Availability: ${property.availability}. Please confirm all listing details directly with Walhad Housing Solutions.`;
        $("#modalMeta").replaceChildren(...[
          `${property.bedrooms} bedrooms`,
          `${property.bathrooms} bathrooms`,
          property.property_type,
          property.area
        ].map(text => Object.assign(document.createElement("span"), { textContent: text })));
        $("#modalFeatures").replaceChildren(...property.features.map(text => Object.assign(document.createElement("li"), { textContent: text })));
        {% if company.whatsapp %}
        const whatsappMessage = encodeURIComponent(`Hello Walhad Housing Solutions. I am interested in ${property.title} (${property.reference}). Please share the current details.`);
        $("#modalWhatsapp").href = `${siteConfig.whatsappUrl}?text=${whatsappMessage}`;
        {% endif %}
        if (typeof modal.showModal === "function" && !modal.open) modal.showModal();
        document.body.classList.add("modal-open");
        if (updateHistory) history.pushState({ propertyId: id }, "", `/properties/${encodeURIComponent(id)}`);
      };

      const closeProperty = (updateHistory = true) => {
        if (modal.open) modal.close();
        document.body.classList.remove("modal-open");
        activePropertyId = null;
        if (updateHistory && window.location.pathname.startsWith("/properties/")) history.pushState(null, "", "/#properties");
      };

      $$(".details-link").forEach(link => link.addEventListener("click", event => {
        event.preventDefault();
        openProperty(link.dataset.propertyId);
      }));
      $("#modalClose").addEventListener("click", () => closeProperty());
      modal.addEventListener("click", event => {
        const box = modal.getBoundingClientRect();
        const outside = event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom;
        if (outside) closeProperty();
      });
      modal.addEventListener("close", () => {
        document.body.classList.remove("modal-open");
        if (activePropertyId && window.location.pathname.startsWith("/properties/")) history.pushState(null, "", "/#properties");
        activePropertyId = null;
      });
      window.addEventListener("popstate", () => {
        const match = window.location.pathname.match(/^\/properties\/([^/]+)$/);
        if (match && propertyMap.has(decodeURIComponent(match[1]))) openProperty(decodeURIComponent(match[1]), false);
        else if (modal.open) closeProperty(false);
      });

      // Fill contact form from listing CTAs.
      const preparePropertyInquiry = (reference, purpose) => {
        $("#propertyReference").value = reference || "";
        $("#inquiryType").value = purpose || "General Inquiry";
        if (!$("#message").value.trim()) $("#message").value = reference ? `I would like to confirm availability and receive more information about property ${reference}.` : "";
        window.setTimeout(() => $("#fullName").focus({ preventScroll: true }), reduceMotion ? 0 : 650);
      };
      $$(".contact-property").forEach(link => link.addEventListener("click", () => preparePropertyInquiry(link.dataset.reference, link.dataset.purpose)));
      $$("[data-inquiry-link]").forEach(link => link.addEventListener("click", () => {
        $("#inquiryType").value = link.dataset.inquiryLink;
        if (!$("#message").value.trim()) $("#message").value = "I would like to list a property with Walhad Housing Solutions. Please contact me with the next steps.";
      }));
      $("#modalContact").addEventListener("click", () => {
        const property = propertyMap.get(activePropertyId);
        closeProperty();
        if (property) preparePropertyInquiry(property.reference, property.purpose === "rent" ? "Rent" : "Buy");
        $("#contact").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
      });

      // Client validation plus server-validated contact submission.
      const contactForm = $("#contactForm");
      const formStatus = $("#formStatus");
      const submitButton = $("#submitButton");
      const fields = ["full_name", "email", "phone", "location", "inquiry_type", "property_reference", "message"];
      const controlsByName = Object.fromEntries(fields.map(name => [name, contactForm.elements[name]]));

      const clearErrors = () => {
        fields.forEach(name => {
          controlsByName[name].classList.remove("invalid");
          controlsByName[name].removeAttribute("aria-invalid");
          $(`#${name}Error`).textContent = "";
        });
        formStatus.textContent = "";
        formStatus.className = "form-status";
      };
      const showErrors = errors => {
        let firstInvalid = null;
        Object.entries(errors).forEach(([name, message]) => {
          const control = controlsByName[name];
          const error = $(`#${name}Error`);
          if (control && error) {
            control.classList.add("invalid");
            control.setAttribute("aria-invalid", "true");
            error.textContent = message;
            if (!firstInvalid) firstInvalid = control;
          }
        });
        if (firstInvalid) firstInvalid.focus();
      };
      Object.entries(controlsByName).forEach(([name, control]) => control.addEventListener("input", () => {
        control.classList.remove("invalid");
        control.removeAttribute("aria-invalid");
        $(`#${name}Error`).textContent = "";
      }));

      contactForm.addEventListener("submit", async event => {
        event.preventDefault();
        clearErrors();
        const data = new FormData(contactForm);
        const localErrors = {};
        if (data.get("full_name").trim().length < 2) localErrors.full_name = "Enter your full name.";
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(data.get("email").trim())) localErrors.email = "Enter a valid email address.";
        if ((data.get("phone").match(/\d/g) || []).length < 7) localErrors.phone = "Enter a valid phone number.";
        if (!data.get("location")) localErrors.location = "Choose a location.";
        if (!data.get("inquiry_type")) localErrors.inquiry_type = "Choose an inquiry type.";
        if (data.get("message").trim().length < 20) localErrors.message = "Please provide at least 20 characters.";
        if (Object.keys(localErrors).length) { showErrors(localErrors); return; }

        submitButton.disabled = true;
        submitButton.querySelector("span").textContent = "Submitting…";
        formStatus.textContent = "Validating your inquiry…";
        try {
          const response = await fetch(contactForm.action, {
            method: "POST",
            body: data,
            headers: { "Accept": "application/json", "X-Requested-With": "fetch" }
          });
          const result = await response.json().catch(() => ({ ok: false, message: "The server returned an unexpected response." }));
          if (!response.ok || !result.ok) {
            if (result.errors) showErrors(result.errors);
            formStatus.textContent = result.message || "Please review the form and try again.";
            formStatus.className = "form-status error";
            return;
          }
          contactForm.reset();
          formStatus.textContent = result.message;
          formStatus.className = "form-status success";
        } catch (error) {
          formStatus.textContent = "We could not reach the server. Check your connection and try again.";
          formStatus.className = "form-status error";
        } finally {
          submitButton.disabled = false;
          submitButton.querySelector("span").textContent = "Submit Inquiry";
        }
      });

      // Open a direct-linked property after page initialization.
      if (siteConfig.initialPropertyId && propertyMap.has(siteConfig.initialPropertyId)) {
        window.setTimeout(() => openProperty(siteConfig.initialPropertyId, false), 80);
      }
    })();
  </script>
</body>
</html>
"""


ERROR_TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ code }} · Walhad Housing Solutions</title>
  <style nonce="{{ csp_nonce }}">
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; color: #122033; background: #f8f6f0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    .card { width: min(560px, 100%); padding: 48px 32px; border: 1px solid #e6e1d8; border-radius: 28px; background: #fff; box-shadow: 0 24px 70px rgba(6,24,45,.12); text-align: center; }
    img { width: 128px; margin: 0 auto 22px; }
    .code { color: #b98216; font-size: .78rem; font-weight: 900; letter-spacing: .15em; text-transform: uppercase; }
    h1 { margin: 8px 0 13px; color: #06182d; font-size: clamp(2rem, 7vw, 3.4rem); line-height: 1.05; }
    p { margin: 0 auto 25px; max-width: 440px; color: #647083; line-height: 1.65; }
    a { display: inline-flex; min-height: 48px; align-items: center; padding: 10px 20px; border-radius: 999px; color: #fff; background: #08213e; font-weight: 750; text-decoration: none; }
    a:focus-visible { outline: 3px solid rgba(207,154,46,.65); outline-offset: 3px; }
  </style>
</head>
<body>
  <main class="card">
    <img src="{{ url_for('static', filename='images/walhad-logo.jpg') }}" alt="Walhad Housing Solutions">
    <div class="code">Error {{ code }}</div>
    <h1>{{ heading }}</h1>
    <p>{{ message }}</p>
    <a href="{{ url_for('home') }}">Return to homepage</a>
  </main>
</body>
</html>
"""


def render_site(initial_property_id: str | None = None) -> str:
    properties = public_properties()
    company = deepcopy(COMPANY)
    # Only expose secure, absolute social URLs supplied by the site operator.
    for field in ("facebook", "instagram", "linkedin"):
        parsed = urllib.parse.urlparse(company[field])
        if parsed.scheme != "https" or not parsed.netloc:
            company[field] = ""
    digits = re.sub(r"\D", "", company["whatsapp"])
    site_config = {
        "initialPropertyId": initial_property_id,
        "fallbackImage": url_for("fallback_property_image"),
        "whatsappUrl": f"https://wa.me/{digits}" if digits else "",
    }
    return render_template_string(
        PAGE_TEMPLATE,
        page_title="Walhad Housing Solutions · Homes in Somaliland",
        properties=properties,
        locations=LOCATIONS,
        company=company,
        current_year=datetime.now().year,
        csrf_token=csrf_token(),
        csp_nonce=g.csp_nonce,
        demo_mode=not bool(os.environ.get("CONTACT_WEBHOOK_URL", "").strip()),
        site_config=site_config,
    )


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.get("/")
def home() -> str:
    return render_site()


@app.get("/properties/<string:property_id>")
def property_detail(property_id: str) -> str:
    if not any(item["id"] == property_id for item in PROPERTIES):
        abort(404)
    return render_site(initial_property_id=property_id)


@app.post("/contact")
def submit_contact() -> tuple[Response, int] | Response:
    if request.form.get("website", ""):
        # Silently acknowledge honeypot submissions without processing them.
        return jsonify(ok=True, message="Thank you."), 200

    supplied_token = request.form.get("csrf_token", "")
    expected_token = session.get("csrf_token", "")
    if not supplied_token or not expected_token or not hmac.compare_digest(supplied_token, expected_token):
        return jsonify(ok=False, message="Your form session expired. Refresh the page and try again."), 400

    client_key = request.remote_addr or "unknown"
    if is_rate_limited(client_key):
        return jsonify(ok=False, message="Too many attempts. Please wait a few minutes before trying again."), 429

    data, errors = validate_contact_form(request.form)
    if errors:
        return jsonify(ok=False, message="Please correct the highlighted fields.", errors=errors), 422

    delivered, state = deliver_to_webhook(data)
    if delivered:
        return jsonify(ok=True, delivered=True, message="Thank you. Your inquiry was sent successfully."), 200
    if state == "demo":
        return jsonify(
            ok=True,
            delivered=False,
            message=(
                "Your inquiry passed validation. This website is in demo mode, so no message was sent. "
                "Configure CONTACT_WEBHOOK_URL to enable delivery."
            ),
        ), 200
    if state == "configuration":
        return jsonify(ok=False, message="Contact delivery is not configured correctly. Please contact the company by phone."), 503
    return jsonify(ok=False, message="We could not send your inquiry right now. Please try again later."), 502


@app.get("/media/property-placeholder.svg")
def fallback_property_image() -> Response:
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 675" role="img" aria-label="Property image unavailable">
    <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#0b315d"/><stop offset="1" stop-color="#06182d"/></linearGradient></defs>
    <rect width="1200" height="675" fill="url(#g)"/><g fill="none" stroke="#e2ba61" stroke-width="18" stroke-linecap="round" stroke-linejoin="round" opacity=".9"><path d="m390 365 210-185 210 185"/><path d="M435 330v190h330V330M545 520V390h110v130"/></g><text x="600" y="590" fill="#fff" opacity=".76" font-family="Arial, sans-serif" font-size="34" text-anchor="middle">PROPERTY IMAGE COMING SOON</text></svg>"""
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz")
def healthcheck() -> Response:
    return jsonify(status="ok")


@app.errorhandler(404)
def not_found(_error: Exception) -> tuple[str, int]:
    return (
        render_template_string(
            ERROR_TEMPLATE,
            code=404,
            heading="Page not found",
            message="The page or property you requested is not available. Browse the current listings from our homepage.",
            csp_nonce=g.csp_nonce,
        ),
        404,
    )


@app.errorhandler(413)
def payload_too_large(_error: Exception) -> tuple[Response, int]:
    return jsonify(ok=False, message="The submitted form was too large."), 413


@app.errorhandler(500)
def internal_error(_error: Exception) -> tuple[str, int]:
    return (
        render_template_string(
            ERROR_TEMPLATE,
            code=500,
            heading="Something went wrong",
            message="We could not complete that request. Please return to the homepage and try again.",
            csp_nonce=g.csp_nonce,
        ),
        500,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
