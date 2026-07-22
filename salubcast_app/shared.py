from __future__ import annotations

import hashlib
import hmac
import mimetypes
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
import zipfile
import urllib.parse
import urllib.request
import feedparser
from io import BytesIO
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path
from typing import Any
from werkzeug.middleware.proxy_fix import ProxyFix

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    send_file,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = PROJECT_ROOT
DATA_DIR = Path(os.environ.get("SALUBCAST_DATA_DIR", str(PROJECT_ROOT / "data"))).resolve()
UPLOAD_DIR = Path(os.environ.get("SALUBCAST_UPLOAD_DIR", str(DATA_DIR / "uploads_clean"))).resolve()
DB_PATH = Path(os.environ.get("SALUBCAST_DB_PATH", str(DATA_DIR / "salubcast_v4_clean.db"))).resolve()
BRAND_LOGO = Path(os.environ.get("SALUBCAST_BRAND_LOGO", str(PROJECT_ROOT / "branding_logo.png"))).resolve()
PLAYER_INSTALL_DIR = Path(os.environ.get("SALUBCAST_PLAYER_BUILD_DIR", str(PROJECT_ROOT / "player_build"))).resolve()
PLAYER_INSTALL_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "mp4", "webm", "pdf"}

BRAND = {
    "name": "SalubCast",
    "tagline": "Narrowcasting with swagger.",
    "primary": "#22c55e",
    "secondary": "#16a34a",
    "panel": "#0f172a",
    "soft": "#1f2937",
    "line": "#334155",
    "text": "#e5e7eb",
    "muted": "#94a3b8",
}

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SALUBCAST_SECRET", secrets.token_hex(16))
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=int(os.environ.get("SALUBCAST_PROXY_FIX_FOR", "1")),
    x_proto=int(os.environ.get("SALUBCAST_PROXY_FIX_PROTO", "1")),
    x_host=int(os.environ.get("SALUBCAST_PROXY_FIX_HOST", "1")),
    x_port=int(os.environ.get("SALUBCAST_PROXY_FIX_PORT", "1")),
)
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('SALUBCAST_MAX_UPLOAD_MB', '250')) * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = os.environ.get('SALUBCAST_SESSION_SAMESITE', 'Lax')
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SALUBCAST_SESSION_SECURE', '0').strip().lower() in {'1', 'true', 'yes', 'on'}
app.config['PREFERRED_URL_SCHEME'] = os.environ.get('SALUBCAST_PREFERRED_URL_SCHEME', 'https')
ACTIVATION_CODE_TTL_MINUTES = int(os.environ.get('SALUBCAST_ACTIVATION_TTL_MINUTES', '30'))
PLAYER_HEARTBEAT_SECONDS = int(os.environ.get('SALUBCAST_PLAYER_HEARTBEAT_SECONDS', '30'))
WEATHER_CACHE_TTL_SECONDS = int(os.environ.get('SALUBCAST_WEATHER_CACHE_TTL_SECONDS', '900'))
HEALTH_STATUS_TOKEN = os.environ.get('SALUBCAST_HEALTH_TOKEN', '').strip()
PUBLIC_BASE_URL = os.environ.get('SALUBCAST_PUBLIC_BASE_URL', '').strip().rstrip('/')
PASSWORD_RESET_TTL_MINUTES = int(os.environ.get('SALUBCAST_PASSWORD_RESET_TTL_MINUTES', '30'))
SMTP_HOST = os.environ.get('SALUBCAST_SMTP_HOST', '').strip()
SMTP_PORT = int(os.environ.get('SALUBCAST_SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SALUBCAST_SMTP_USER', '').strip()
SMTP_PASSWORD = os.environ.get('SALUBCAST_SMTP_PASSWORD', '')
SMTP_FROM = os.environ.get('SALUBCAST_SMTP_FROM', '').strip() or SMTP_USER
SMTP_USE_TLS = os.environ.get('SALUBCAST_SMTP_USE_TLS', '1').strip().lower() in {'1', 'true', 'yes', 'on'}
_weather_cache: dict[str, tuple[float, dict[str, str]]] = {}

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PORTAL_CONFIGURATION_ID = os.environ.get("STRIPE_PORTAL_CONFIGURATION_ID", "").strip()
STRIPE_PRICE_IDS = {
    "starter": os.environ.get("STRIPE_PRICE_STARTER", "").strip(),
    "professional": os.environ.get("STRIPE_PRICE_PROFESSIONAL", "").strip(),
    "enterprise": os.environ.get("STRIPE_PRICE_ENTERPRISE", "").strip(),
}


# -----------------------------
# Database helpers
# -----------------------------
def external_base_url() -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    return request.url_root.rstrip("/")


def stripe_enabled() -> bool:
    return bool(STRIPE_SECRET_KEY)


def stripe_client():
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("Stripe is niet geconfigureerd. Zet STRIPE_SECRET_KEY.")
    import stripe  # lazy import so local use without Stripe stays possible

    stripe.api_key = STRIPE_SECRET_KEY
    return stripe


def stripe_price_for_plan(plan_name: str) -> str:
    return STRIPE_PRICE_IDS.get((plan_name or "").strip().lower(), "")


def plan_name_from_price_id(price_id: str | None) -> str:
    normalized = (price_id or "").strip()
    for plan_name, configured_price in STRIPE_PRICE_IDS.items():
        if configured_price and configured_price == normalized:
            return plan_name
    return "starter"


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 310000).hex()


def legacy_hash_password(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


def verify_password(password: str, salt: str, stored_hash: str) -> tuple[bool, bool]:
    current = hash_password(password, salt)
    if hmac.compare_digest(current, stored_hash):
        return True, False
    legacy = legacy_hash_password(password, salt)
    if hmac.compare_digest(legacy, stored_hash):
        return True, True
    return False, False


def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def send_email(to_email: str, subject: str, body: str) -> bool:
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        print(f"[SalubCast] SMTP niet geconfigureerd; e-mail naar {to_email} niet verstuurd: {subject}")
        return False
    import smtplib
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_FROM or SMTP_USER
    message["To"] = to_email
    message.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(message)
        return True
    except Exception as exc:
        print(f"[SalubCast] Versturen van e-mail naar {to_email} mislukt: {exc}")
        return False


def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    conn = db()
    row = conn.execute(query, params).fetchone()
    conn.close()
    return row


def execute(query: str, params: tuple[Any, ...] = ()) -> None:
    conn = db()
    conn.execute(query, params)
    conn.commit()
    conn.close()


def company_logo_dir() -> Path:
    d = DATA_DIR / "branding"
    d.mkdir(exist_ok=True)
    return d


def company_logo_path(filename: str) -> Path:
    return company_logo_dir() / filename


def safe_branding_filename(filename: str) -> str | None:
    candidate = secure_filename((filename or "").strip())
    if not candidate or candidate != (filename or "").strip():
        return None
    return candidate


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_hex(16)
        session["_csrf_token"] = token
    return token


def verify_screen_token(token: str, stored_token: str | None) -> bool:
    if not token or not stored_token:
        return False
    return hmac.compare_digest(token, stored_token)


def log_event(actor: str, action: str, target_type: str, target_id: str = "", details: str = "", company_id: str | None = None) -> None:
    execute(
        "INSERT INTO audit_logs (company_id, actor, action, target_type, target_id, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (company_id, actor, action, target_type, target_id, details[:2000], now_iso()),
    )


def validate_upload_file(file_storage) -> tuple[bool, str]:
    filename = file_storage.filename or ""
    if not allowed_file(filename):
        return False, "Bestandstype niet toegestaan."
    content_type = (file_storage.mimetype or "").strip().lower()
    guessed, _ = mimetypes.guess_type(filename)
    guessed = (guessed or get_mimetype(filename) or "").lower()
    valid_guessed = guessed.startswith("image/") or guessed.startswith("video/") or guessed == "application/pdf"
    generic_content_types = {"", "application/octet-stream", "binary/octet-stream", "application/x-empty"}
    valid_content = content_type.startswith("image/") or content_type.startswith("video/") or content_type == "application/pdf"
    if not valid_guessed:
        return False, "Bestandsnaam/extensie matcht geen geldig mediatype."
    if content_type not in generic_content_types and not valid_content:
        return False, "Bestand heeft geen geldig content-type."
    return True, ""


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def iso_to_timestamp(value: str | None) -> float | None:
    parsed = parse_iso(value)
    if not parsed:
        return None
    try:
        return parsed.timestamp()
    except Exception:
        return None


def is_recent_heartbeat(value: str | None, threshold_seconds: int = 120) -> bool:
    timestamp = iso_to_timestamp(value)
    if timestamp is None:
        return False
    return (time.time() - timestamp) < threshold_seconds


def parse_int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def fetch_company_row(table: str, record_id: str, company_id: str) -> sqlite3.Row | None:
    allowed_tables = {"media", "playlists", "screens", "feeds", "users", "schedules"}
    if table not in allowed_tables or not record_id or not company_id:
        return None
    return fetch_one(f"SELECT * FROM {table} WHERE id = ? AND company_id = ? LIMIT 1", (record_id, company_id))


def time_in_schedule_range(start_time: str, end_time: str, now_str: str) -> bool:
    if start_time <= end_time:
        return start_time <= now_str <= end_time
    return now_str >= start_time or now_str <= end_time


def expand_schedule_minutes(start_time: str, end_time: str, days: list[int]) -> set[tuple[int, int]]:
    def to_min(t: str) -> int:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    start = to_min(start_time)
    end = to_min(end_time)
    slots: set[tuple[int, int]] = set()
    for day in days:
        if start <= end:
            for minute in range(start, end + 1):
                slots.add((day, minute))
        else:
            for minute in range(start, 24 * 60):
                slots.add((day, minute))
            next_day = (day + 1) % 7
            for minute in range(0, end + 1):
                slots.add((next_day, minute))
    return slots


def schedule_conflicts(company_id: str, screen_id: str, days: list[int], start_time: str, end_time: str, exclude_id: str | None = None) -> list[sqlite3.Row]:
    base = "SELECT * FROM schedules WHERE company_id = ? AND screen_id = ?"
    params: tuple[Any, ...] = (company_id, screen_id)
    if exclude_id:
        base += " AND id != ?"
        params = (company_id, screen_id, exclude_id)
    schedules = fetch_all(base, params)
    conflicts = []
    target_minutes = expand_schedule_minutes(start_time, end_time, days)
    for sch in schedules:
        existing_days = json.loads(sch["days_json"])
        if not set(existing_days) & set(days):
            continue
        existing_minutes = expand_schedule_minutes(sch["start_time"], sch["end_time"], existing_days)
        if target_minutes & existing_minutes:
            conflicts.append(sch)
    return conflicts


def is_schedule_live(schedule_row: sqlite3.Row, weekday: int, now_str: str) -> bool:
    days = json.loads(schedule_row["days_json"])
    if time_in_schedule_range(schedule_row["start_time"], schedule_row["end_time"], now_str):
        if schedule_row["start_time"] <= schedule_row["end_time"]:
            return weekday in days
        prev_day = (weekday - 1) % 7
        return weekday in days or prev_day in days
    return False


def get_company_branding_filename(company_id: str | None) -> str | None:
    if not company_id:
        return None
    row = fetch_one("SELECT logo_filename FROM companies WHERE id = ? LIMIT 1", (company_id,))
    return row["logo_filename"] if row and row["logo_filename"] else None


def current_company_branding_url() -> str | None:
    company_id = current_company_id() or session.get("company_id")
    filename = get_company_branding_filename(company_id)
    if not filename:
        return None
    return url_for("company_branding_logo", filename=filename)


def init_db() -> None:
    conn = db()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            slug TEXT NOT NULL UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            billing_email TEXT,
            plan_name TEXT NOT NULL DEFAULT 'starter',
            billing_status TEXT NOT NULL DEFAULT 'trial',
            trial_ends_at TEXT,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            logo_filename TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (company_id) REFERENCES companies(id)
        );

        CREATE TABLE IF NOT EXISTS media (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            title TEXT NOT NULL,
            filename TEXT NOT NULL,
            mimetype TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL DEFAULT 10,
            uploaded_at TEXT NOT NULL,
            FOREIGN KEY (company_id) REFERENCES companies(id)
        );

        CREATE TABLE IF NOT EXISTS screens (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            name TEXT NOT NULL,
            location TEXT DEFAULT '',
            orientation TEXT NOT NULL DEFAULT 'landscape',
            insert_feed_pages INTEGER NOT NULL DEFAULT 0,
            feed_page_every INTEGER NOT NULL DEFAULT 3,
            feed_page_duration INTEGER NOT NULL DEFAULT 12,
            badge_visible INTEGER NOT NULL DEFAULT 1,
            badge_position TEXT NOT NULL DEFAULT 'top-right',
            image_fit TEXT NOT NULL DEFAULT 'contain',
            portrait_image_fit TEXT NOT NULL DEFAULT 'contain',
            feed_layout TEXT NOT NULL DEFAULT 'cards',
            weather_city TEXT NOT NULL DEFAULT '',
            token TEXT NOT NULL,
            device_secret_hash TEXT,
            device_last_ip TEXT,
            activation_code TEXT,
            activation_expires_at TEXT,
            activation_used_at TEXT,
            activated_at TEXT,
            last_seen TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (company_id) REFERENCES companies(id)
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS playlists (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (company_id) REFERENCES companies(id)
        );

        CREATE TABLE IF NOT EXISTS playlist_items (
            id TEXT PRIMARY KEY,
            playlist_id TEXT NOT NULL,
            media_id TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            FOREIGN KEY (playlist_id) REFERENCES playlists(id),
            FOREIGN KEY (media_id) REFERENCES media(id)
        );

        CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            screen_id TEXT NOT NULL,
            playlist_id TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            days_json TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 100,
            active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (company_id) REFERENCES companies(id),
            FOREIGN KEY (screen_id) REFERENCES screens(id),
            FOREIGN KEY (playlist_id) REFERENCES playlists(id)
        );

        CREATE TABLE IF NOT EXISTS feeds (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            max_items INTEGER NOT NULL DEFAULT 5,
            refresh_seconds INTEGER NOT NULL DEFAULT 300,
            is_active INTEGER NOT NULL DEFAULT 1,
            is_ticker INTEGER NOT NULL DEFAULT 0,
            last_fetched_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (company_id) REFERENCES companies(id)
        );

        CREATE TABLE IF NOT EXISTS feed_items (
            id TEXT PRIMARY KEY,
            feed_id TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT,
            summary TEXT,
            published_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (feed_id) REFERENCES feeds(id)
        );

        CREATE TABLE IF NOT EXISTS password_resets (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        """
    )
    screen_cols = {row['name'] for row in conn.execute("PRAGMA table_info(screens)").fetchall()}
    if 'activation_code' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN activation_code TEXT")
    if 'activation_expires_at' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN activation_expires_at TEXT")
    if 'activation_used_at' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN activation_used_at TEXT")
    if 'activated_at' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN activated_at TEXT")
    if 'device_secret_hash' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN device_secret_hash TEXT")
    if 'device_last_ip' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN device_last_ip TEXT")
    if 'orientation' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN orientation TEXT NOT NULL DEFAULT 'landscape'")
    if 'insert_feed_pages' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN insert_feed_pages INTEGER NOT NULL DEFAULT 0")
    if 'feed_page_every' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN feed_page_every INTEGER NOT NULL DEFAULT 3")
    if 'feed_page_duration' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN feed_page_duration INTEGER NOT NULL DEFAULT 12")
    if 'badge_visible' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN badge_visible INTEGER NOT NULL DEFAULT 1")
    if 'badge_position' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN badge_position TEXT NOT NULL DEFAULT 'top-right'")
    if 'image_fit' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN image_fit TEXT NOT NULL DEFAULT 'contain'")
    if 'portrait_image_fit' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN portrait_image_fit TEXT NOT NULL DEFAULT 'contain'")
    if 'feed_layout' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN feed_layout TEXT NOT NULL DEFAULT 'cards'")
    if 'weather_city' not in screen_cols:
        conn.execute("ALTER TABLE screens ADD COLUMN weather_city TEXT NOT NULL DEFAULT ''")
    feed_item_cols = {row['name'] for row in conn.execute("PRAGMA table_info(feed_items)").fetchall()}
    if 'summary' not in feed_item_cols:
        conn.execute("ALTER TABLE feed_items ADD COLUMN summary TEXT")
    company_cols = {row['name'] for row in conn.execute("PRAGMA table_info(companies)").fetchall()}
    if 'logo_filename' not in company_cols:
        conn.execute("ALTER TABLE companies ADD COLUMN logo_filename TEXT")
    if 'billing_email' not in company_cols:
        conn.execute("ALTER TABLE companies ADD COLUMN billing_email TEXT")
    if 'plan_name' not in company_cols:
        conn.execute("ALTER TABLE companies ADD COLUMN plan_name TEXT NOT NULL DEFAULT 'starter'")
    if 'billing_status' not in company_cols:
        conn.execute("ALTER TABLE companies ADD COLUMN billing_status TEXT NOT NULL DEFAULT 'trial'")
    if 'trial_ends_at' not in company_cols:
        conn.execute("ALTER TABLE companies ADD COLUMN trial_ends_at TEXT")
    if 'stripe_customer_id' not in company_cols:
        conn.execute("ALTER TABLE companies ADD COLUMN stripe_customer_id TEXT")
    if 'stripe_subscription_id' not in company_cols:
        conn.execute("ALTER TABLE companies ADD COLUMN stripe_subscription_id TEXT")
    schedule_cols = {row['name'] for row in conn.execute("PRAGMA table_info(schedules)").fetchall()}
    if 'priority' not in schedule_cols:
        conn.execute("ALTER TABLE schedules ADD COLUMN priority INTEGER NOT NULL DEFAULT 100")
    conn.execute("CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT, actor TEXT NOT NULL, action TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)")
    conn.commit()
    conn.close()


def seed_defaults() -> None:
    company = fetch_one("SELECT * FROM companies LIMIT 1")
    if not company:
        company_id = str(uuid.uuid4())
        created_at = now_iso()
        trial_days = parse_int(os.environ.get('SALUBCAST_DEFAULT_TRIAL_DAYS', '14'), 14, minimum=1, maximum=365)
        trial_ends_at = (datetime.now(timezone.utc) + timedelta(days=trial_days)).isoformat()
        execute(
            "INSERT INTO companies (id, name, slug, is_active, billing_email, plan_name, billing_status, trial_ends_at, created_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
            (company_id, "Default Workspace", "default-workspace", os.environ.get('SALUBCAST_BOOTSTRAP_ADMIN_EMAIL', 'admin@salubcast.local'), "starter", "trial", trial_ends_at, created_at),
        )
    else:
        company_id = company['id']

    if not fetch_one("SELECT id FROM users LIMIT 1"):
        bootstrap_password = os.environ.get('SALUBCAST_BOOTSTRAP_ADMIN_PASSWORD')
        bootstrap_email = os.environ.get('SALUBCAST_BOOTSTRAP_ADMIN_EMAIL', 'admin@salubcast.local')
        if bootstrap_password:
            salt = secrets.token_hex(8)
            execute(
                "INSERT INTO users (id, company_id, email, password_hash, salt, full_name, role, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    str(uuid.uuid4()),
                    company_id,
                    bootstrap_email,
                    hash_password(bootstrap_password, salt),
                    salt,
                    os.environ.get('SALUBCAST_BOOTSTRAP_ADMIN_NAME', 'Bootstrap Admin'),
                    "superadmin",
                    now_iso(),
                ),
            )
            print(f"[SalubCast] Bootstrap superadmin created for {bootstrap_email}")
        else:
            print("[SalubCast] No bootstrap admin created. Set SALUBCAST_BOOTSTRAP_ADMIN_PASSWORD to seed one safely.")

    if not fetch_one("SELECT id FROM playlists LIMIT 1"):
        playlist_id = str(uuid.uuid4())
        screen_id = str(uuid.uuid4())
        execute(
            "INSERT INTO playlists (id, company_id, name, created_at) VALUES (?, ?, ?, ?)",
            (playlist_id, company_id, "Reception Loop", now_iso()),
        )
        execute(
            "INSERT INTO screens (id, company_id, name, location, token, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (screen_id, company_id, "Receptie TV", "Front Office", str(uuid.uuid4()), now_iso()),
        )
        execute(
            "INSERT INTO schedules (id, company_id, screen_id, playlist_id, start_time, end_time, days_json, active) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (
                str(uuid.uuid4()), company_id, screen_id, playlist_id, "00:00", "23:59", json.dumps([0,1,2,3,4,5,6])
            ),
        )


init_db()
seed_defaults()


# -----------------------------
# Helpers
# -----------------------------
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_mimetype(filename: str) -> str:
    ext = filename.rsplit(".", 1)[1].lower()
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
        "mp4": "video/mp4",
        "webm": "video/webm",
        "pdf": "application/pdf",
    }.get(ext, "application/octet-stream")


def current_company_id() -> str | None:
    if session.get("user_role") == "superadmin" and session.get("company_view_id"):
        return session.get("company_view_id")
    return session.get("company_id")


def current_company_name() -> str:
    if session.get("user_role") == "superadmin" and session.get("company_view_name"):
        return session.get("company_view_name")
    return session.get("company_name", "Geen bedrijf")


def is_superadmin() -> bool:
    return session.get("user_role") == "superadmin"


def is_admin_like() -> bool:
    return session.get("user_role") in {"superadmin", "company_admin"}

def resolve_current_company_id() -> str | None:
    company_id = current_company_id()
    if company_id and fetch_one("SELECT id FROM companies WHERE id = ? LIMIT 1", (company_id,)):
        return company_id

    user_id = session.get("user_id")
    if user_id:
        user_company = fetch_one(
            "SELECT users.company_id, companies.name AS company_name FROM users JOIN companies ON companies.id = users.company_id WHERE users.id = ? LIMIT 1",
            (user_id,),
        )
        if user_company:
            session["company_id"] = user_company["company_id"]
            session["company_name"] = user_company["company_name"]
            if session.get("user_role") == "superadmin":
                session.pop("company_view_id", None)
                session.pop("company_view_name", None)
            return user_company["company_id"]

    if is_superadmin():
        company = fetch_one("SELECT id, name FROM companies WHERE is_active = 1 ORDER BY created_at ASC LIMIT 1") or fetch_one("SELECT id, name FROM companies ORDER BY created_at ASC LIMIT 1")
        if company:
            session["company_view_id"] = company["id"]
            session["company_view_name"] = company["name"]
            return company["id"]

    return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def content_admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        if session.get("user_role") not in {"superadmin", "company_admin"}:
            flash("Alleen admins mogen content beheren.")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        if not is_admin_like():
            flash("Alleen admins mogen daar naar binnen.")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapper


def superadmin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        if not is_superadmin():
            flash("Alleen de hoofdbeheerder mag bedrijven beheren.")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapper


def generate_activation_code() -> str:
    return secrets.token_hex(3).upper()


def brand_css() -> str:
    return f"""
    :root {{
      --panel: rgba(255,255,255,.64);
      --soft: rgba(248,250,252,.58);
      --line: rgba(15,23,42,.1);
      --text: #111827;
      --muted: #5f6f83;
      --accent: #10b981;
      --accent2: #0f766e;
      --danger: #ef4444;
      --shell: #eef4f8;
      --shell2: #dfe8ee;
      --panel-strong: rgba(255,255,255,.66);
      --panel-soft: rgba(255,255,255,.34);
      --glow: rgba(16, 185, 129, .12);
      --glow2: rgba(15, 118, 110, .1);
    }}
    body[data-theme="dark"] {{
      --panel: rgba(9,16,28,.88);
      --soft: rgba(9,16,28,.74);
      --line: rgba(148,163,184,.14);
      --text: #edf4fb;
      --muted: #93a4b8;
      --shell: #070d17;
      --shell2: #0a1220;
      --panel-strong: rgba(9,16,28,.88);
      --panel-soft: rgba(9,16,28,.7);
      --glow: rgba(16, 185, 129, .08);
      --glow2: rgba(148, 163, 184, .06);
    }}
    * {{ box-sizing: border-box; }}
    html {{ min-height:100%; background:#eef4f8; }}
    body {{
      margin:0;
      min-height:100vh;
      font-family:"Segoe UI Variable Display","Segoe UI","Trebuchet MS",sans-serif;
      background:
        radial-gradient(circle at 12% -8%, rgba(16,185,129,.16), transparent 30%),
        radial-gradient(circle at 88% 0%, rgba(15,118,110,.1), transparent 32%),
        linear-gradient(135deg, #f7fafc 0%, #edf4f8 48%, #f8fafc 100%);
      color:var(--text);
    }}
    body[data-theme="dark"] {{
      background:linear-gradient(145deg, #070d17 0%, #0a1220 52%, #070d17 100%);
    }}
    body::before {{
      content:'';
      position:fixed;
      inset:0;
      pointer-events:none;
      background:
        linear-gradient(120deg, rgba(255,255,255,.42), rgba(255,255,255,0) 30%, rgba(255,255,255,.28)),
        linear-gradient(180deg, rgba(15,23,42,.035), rgba(15,23,42,0));
      opacity:1;
    }}
    body[data-theme="dark"]::before {{
      background:linear-gradient(180deg, rgba(255,255,255,.025), rgba(255,255,255,0));
      opacity:1;
    }}
    a {{ color:inherit; text-decoration:none; transition:all .22s ease; }}
    .wrap {{ max-width: 1480px; margin:0 auto; padding:28px 24px 42px; position:relative; z-index:1; }}
    .topbar {{
      display:flex;
      justify-content:space-between;
      gap:18px;
      flex-wrap:wrap;
      align-items:flex-start;
      margin-bottom:24px;
      padding:18px 20px;
      border-radius:24px;
      border:1px solid rgba(255,255,255,.62);
      background:linear-gradient(180deg, rgba(255,255,255,.62), rgba(255,255,255,.32));
      box-shadow:0 24px 70px rgba(15,23,42,.1);
      backdrop-filter: blur(34px) saturate(180%);
      -webkit-backdrop-filter: blur(34px) saturate(180%);
    }}
    .brand {{ display:flex; align-items:center; gap:18px; max-width:760px; }}
    .brand-text {{ display:grid; gap:6px; }}
    .brand-title {{
      font-size: clamp(1.5rem, 1.9vw, 2.15rem);
      font-weight: 850;
      letter-spacing:-.03em;
      line-height:1.05;
    }}
    .brand-sub {{ color:var(--muted); font-size:15px; max-width:58ch; }}
    @media (max-width: 640px) {{
      .brand {{ gap:12px; }}
      .topbar-logo {{ height:56px; }}
    }}
    .header-actions {{ display:flex; align-items:center; justify-content:flex-end; gap:10px; flex-wrap:wrap; }}
    .header-actions form {{ display:inline; }}
    .shell-action, .theme-toggle {{
      width:auto;
      min-height:42px;
      padding:11px 15px;
      border-radius:14px;
      border:1px solid rgba(15,23,42,.08);
      background:rgba(255,255,255,.5);
      color:#102033;
      font-weight:850;
      box-shadow:0 14px 30px rgba(15,23,42,.1), inset 0 1px 0 rgba(255,255,255,.76);
      backdrop-filter: blur(22px) saturate(170%);
      -webkit-backdrop-filter: blur(22px) saturate(170%);
    }}
    .shell-action:hover, .theme-toggle:hover {{ transform:translateY(-1px); box-shadow:0 18px 38px rgba(15,23,42,.14); }}
    body[data-theme="dark"] .shell-action,
    body[data-theme="dark"] .theme-toggle {{
      background:rgba(9,16,28,.9);
      color:#edf4fb;
      border-color:rgba(148,163,184,.16);
      box-shadow:0 14px 30px rgba(0,0,0,.22), inset 0 1px 0 rgba(255,255,255,.08);
    }}
    .theme-toggle {{ display:inline-flex; align-items:center; gap:8px; }}
    .theme-toggle::before {{ content:'Thema'; font-size:10px; letter-spacing:.12em; text-transform:uppercase; opacity:.7; }}
    .nav {{
      display:flex;
      gap:12px;
      flex-wrap:wrap;
      padding:14px;
      border-radius:20px;
      background:rgba(255,255,255,.36);
      border:1px solid rgba(255,255,255,.56);
      position:sticky;
      top:12px;
      backdrop-filter: blur(26px) saturate(180%);
      z-index:99;
      margin-bottom:22px;
      box-shadow:0 16px 44px rgba(15,23,42,.1);
      justify-content:flex-start;
    }}
    .nav a {{
      background:rgba(255,255,255,.44);
      padding:10px 14px;
      border-radius:13px;
      border:1px solid rgba(15,23,42,.07);
      font-size:14px;
      font-weight:720;
      color:#1e293b;
    }}
    body[data-theme="dark"] .nav a {{
      background:linear-gradient(180deg, rgba(15,23,42,.68), rgba(15,23,42,.38));
      border-color:rgba(226,232,240,.13);
      color:#e2e8f0;
    }}
    .nav a:hover {{
      border-color: rgba(34,197,94,.35);
      transform: translateY(-1px);
      box-shadow:0 12px 30px rgba(34,197,94,.12);
    }}
    .grid {{ display:grid; gap:18px; }}
    .cols-4 {{ grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .two {{ grid-template-columns: 430px 1fr; align-items:start; }}
    .three {{ display:grid; grid-template-columns: repeat(3, 1fr); gap:16px; }}
    .card {{
      background:linear-gradient(180deg, var(--panel-strong), var(--panel-soft));
      border:1px solid rgba(255,255,255,.58);
      border-radius:20px;
      padding:22px;
      box-shadow:0 18px 48px rgba(15,23,42,.09);
      backdrop-filter: blur(32px) saturate(180%);
      -webkit-backdrop-filter: blur(32px) saturate(180%);
    }}

    body[data-theme="dark"] .topbar,
    body[data-theme="dark"] .nav,
    body[data-theme="dark"] .card {{
      background:linear-gradient(180deg, rgba(9,16,28,.9), rgba(9,16,28,.74));
      border-color:rgba(148,163,184,.16);
      box-shadow:0 20px 54px rgba(0,0,0,.28);
    }}
    body[data-theme="dark"] .nav {{ background:rgba(9,16,28,.84); }}
    body[data-theme="dark"] .nav a {{
      background:rgba(9,16,28,.86);
      border-color:rgba(148,163,184,.14);
      color:#e5edf7;
    }}
    body[data-theme="dark"] .nav a:hover {{
      border-color:rgba(16,185,129,.34);
      box-shadow:0 12px 30px rgba(0,0,0,.22);
    }}
    body[data-theme="dark"] input,
    body[data-theme="dark"] select,
    body[data-theme="dark"] textarea {{
      background:rgba(9,16,28,.86);
      border-color:rgba(148,163,184,.18);
      color:#eef6ff;
      box-shadow:inset 0 1px 0 rgba(255,255,255,.04);
    }}
    body[data-theme="dark"] input::placeholder,
    body[data-theme="dark"] textarea::placeholder {{ color:#7f8fa3; }}
    body[data-theme="dark"] .table {{ background:rgba(9,16,28,.76); }}
    body[data-theme="dark"] .table th {{
      background:rgba(9,16,28,.92);
      color:#cbd7e6;
    }}
    body[data-theme="dark"] .table th,
    body[data-theme="dark"] .table td,
    body[data-theme="dark"] .playlist-row,
    body[data-theme="dark"] .tenant-line {{ border-color:rgba(148,163,184,.12); }}
    body[data-theme="dark"] .dropzone,
    body[data-theme="dark"] .codebox {{
      background:rgba(9,16,28,.86);
      border-color:rgba(148,163,184,.16);
      color:#eef6ff;
    }}
    body[data-theme="dark"] .metric-card::after {{
      background:radial-gradient(circle, rgba(148,163,184,.08), transparent 68%);
    }}
    body[data-theme="dark"] .screen-url {{ color:#8fa1b7; }}
    .muted {{ color:var(--muted); }}
    .hero {{ display:grid; gap:18px; grid-template-columns: 1.2fr .8fr; margin-bottom:18px; }}
    .stat {{ font-size:44px; font-weight:900; letter-spacing:-.04em; }}
    h1,h2,h3 {{ margin:0 0 14px 0; letter-spacing:-.03em; }}
    h1 {{ font-size: clamp(2rem, 2.3vw, 2.8rem); }}
    h2 {{ font-size: clamp(1.35rem, 1.8vw, 2rem); }}
    h3 {{ font-size: 1.15rem; }}
    p, li, td, th, label, input, select, textarea, button {{ font-size:15px; }}
    form {{ display:grid; gap:14px; }}
    input, select, textarea, button {{
      width:100%;
      padding:14px 16px;
      border-radius:16px;
      border:1px solid rgba(148,163,184,.24);
      background:rgba(255,255,255,.68);
      color:var(--text);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.55);
    }}
    input:focus, select:focus, textarea:focus {{
      outline:none;
      border-color:rgba(34,197,94,.42);
      box-shadow:0 0 0 4px rgba(34,197,94,.1);
    }}
    button {{
      cursor:pointer;
      border:none;
      background:linear-gradient(135deg, #0f766e, #10b981);
      color:white;
      font-weight:800;
      letter-spacing:.01em;
    }}
    button.secondary {{ background:linear-gradient(180deg, rgba(255,255,255,.08), rgba(255,255,255,.04)); }}
    .secondary {{
      color:#102033;
      border:1px solid rgba(255,255,255,.64);
      background:rgba(255,255,255,.58);
      box-shadow:0 12px 26px rgba(15,23,42,.1);
    }}
    body[data-theme="dark"] .secondary {{
      color:#eef6ff;
      border-color:rgba(226,232,240,.14);
      background:rgba(9,16,28,.86);
    }}
    button.danger {{ background:linear-gradient(135deg, #b91c1c, #ef4444); }}
    .table {{
      width:100%;
      border-collapse: separate;
      border-spacing:0;
      overflow:hidden;
      border-radius:22px;
      background:rgba(255,255,255,.42);
    }}
    .table th, .table td {{
      padding:14px 16px;
      border-bottom:1px solid rgba(148,163,184,.1);
      text-align:left;
      vertical-align:top;
    }}
    .table th {{
      color:#334155;
      font-size:13px;
      text-transform:uppercase;
      letter-spacing:.12em;
      background:rgba(255,255,255,.48);
    }}
    .pill {{ display:inline-flex; align-items:center; gap:8px; padding:8px 12px; border-radius:999px; background:#052e16; color:#86efac; font-size:12px; font-weight:800; }}
    .pill.off {{ background:#450a0a; color:#fca5a5; }}
    .flash {{
      padding:14px 16px;
      border-radius:18px;
      margin-bottom:16px;
      background:linear-gradient(180deg, rgba(5,46,22,.95), rgba(20,83,45,.78));
      border:1px solid #14532d;
      box-shadow:0 14px 34px rgba(0,0,0,.18);
    }}
    .media-preview {{ max-height:84px; max-width:132px; border-radius:14px; }}
    .inline {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
    .playlist-row {{ display:grid; grid-template-columns: 60px 1fr 180px; gap:12px; align-items:center; padding:12px 0; border-bottom:1px solid rgba(148,163,184,.1); }}
    .dropzone {{ border:1px dashed rgba(148,163,184,.24); border-radius:20px; padding:14px; background:rgba(255,255,255,.02); }}
    .login-shell {{ min-height:100vh; display:grid; place-items:center; padding:24px; }}
    .login-card {{ width:min(100%, 620px); }}
    .topbar-logo {{ height:80px; width:auto; max-width:300px; object-fit:contain; flex-shrink:0; }}
    .badge {{
      display:inline-flex;
      padding:8px 12px;
      border-radius:999px;
      background:rgba(255,255,255,.58);
      color:#14532d;
      font-size:12px;
      font-weight:800;
      border:1px solid rgba(255,255,255,.72);
      backdrop-filter: blur(18px);
    }}
    body[data-theme="dark"] .badge {{
      background:rgba(9,16,28,.86);
      color:#d8fbe6;
      border-color:rgba(148,163,184,.16);
    }}
    .logo-preview {{ width: 100%; max-width: 520px; max-height: 160px; object-fit: contain; border-radius: 0; background: transparent; padding: 0; display:block; }}
    .kicker {{ font-size:12px; color:#fcd34d; text-transform:uppercase; letter-spacing:.18em; font-weight:800; }}
    .codebox {{ font-family:Consolas, monospace; font-size:30px; letter-spacing:.12em; padding:18px; border-radius:22px; background:rgba(255,255,255,.58); border:1px solid rgba(255,255,255,.72); text-align:center; color:#0f172a; }}
    .section-intro {{
      display:flex;
      justify-content:space-between;
      gap:16px;
      align-items:flex-end;
      flex-wrap:wrap;
      margin-bottom:14px;
    }}
    .section-intro p {{ margin:0; max-width:70ch; color:var(--muted); font-size:16px; }}
    .dashboard-shell {{ display:grid; gap:18px; }}
    .dashboard-hero {{
      display:grid;
      grid-template-columns:minmax(0, 1.15fr) minmax(280px, .85fr);
      gap:18px;
      align-items:stretch;
      margin-bottom:18px;
    }}
    .dashboard-title {{ display:grid; gap:12px; align-content:center; }}
    .dashboard-title h1 {{ margin:0; font-size:clamp(2rem, 2.8vw, 3.6rem); line-height:1; max-width:12ch; }}
    .dashboard-title p {{ margin:0; color:var(--muted); font-size:17px; max-width:58ch; line-height:1.55; }}
    .quick-actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:6px; }}
    .quick-actions a {{ display:inline-flex; }}
    .metric-card {{ min-height:142px; display:grid; align-content:space-between; gap:14px; position:relative; overflow:hidden; }}
    .metric-card::after {{
      content:'';
      position:absolute;
      inset:auto -18% -44% auto;
      width:180px;
      height:180px;
      border-radius:999px;
      background:radial-gradient(circle, rgba(15,118,110,.13), transparent 66%);
      pointer-events:none;
    }}
    .metric-label {{ color:var(--muted); font-size:13px; font-weight:850; letter-spacing:.12em; text-transform:uppercase; }}
    .metric-value {{ font-size:clamp(2.2rem, 3.5vw, 4rem); font-weight:900; letter-spacing:-.05em; line-height:.92; }}
    .tenant-card {{ display:grid; gap:12px; align-content:space-between; }}
    .tenant-line {{ display:flex; justify-content:space-between; gap:14px; border-bottom:1px solid var(--line); padding:10px 0; }}
    .tenant-line:last-child {{ border-bottom:0; }}
    .screen-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:16px; }}
    .screen-card {{ display:grid; gap:12px; min-height:170px; }}
    .screen-top {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
    .screen-url {{ font-size:12px; color:var(--muted); word-break:break-all; line-height:1.45; }}
    @media (max-width: 950px) {{
      .hero, .two, .three, .dashboard-hero {{ grid-template-columns: 1fr; }}
      .wrap {{ padding:18px 14px 28px; }}
      .topbar {{ padding:18px; border-radius:24px; }}
      .header-actions {{ justify-content:flex-start; }}
      .nav {{ border-radius:22px; }}
    }}

    /* Auth pages (login, register, forgot/reset password) */
    .auth-shell {{
      min-height:100vh;
      display:grid;
      place-items:center;
      padding:32px 24px;
      position:relative;
      overflow:hidden;
    }}
    .auth-shell::before,
    .auth-shell::after {{
      content:'';
      position:fixed;
      width:46vw;
      height:46vw;
      max-width:620px;
      max-height:620px;
      border-radius:50%;
      pointer-events:none;
      z-index:0;
      filter:blur(10px);
    }}
    .auth-shell::before {{
      top:-16vw;
      left:-12vw;
      background:radial-gradient(circle, rgba(16,185,129,.22), transparent 70%);
    }}
    .auth-shell::after {{
      bottom:-18vw;
      right:-10vw;
      background:radial-gradient(circle, rgba(15,118,110,.16), transparent 70%);
    }}
    .auth-theme-toggle {{
      position:fixed;
      top:22px;
      right:22px;
      z-index:2;
    }}
    .auth-flash {{
      position:relative;
      z-index:1;
      width:min(100%, 440px);
      margin:0 auto 18px;
    }}
    .auth-card {{
      position:relative;
      z-index:1;
      width:min(100%, 440px);
      background:linear-gradient(180deg, var(--panel-strong), var(--panel-soft));
      border:1px solid rgba(255,255,255,.6);
      border-radius:28px;
      padding:40px 36px;
      box-shadow:0 30px 80px rgba(15,23,42,.14);
      backdrop-filter: blur(36px) saturate(180%);
      -webkit-backdrop-filter: blur(36px) saturate(180%);
      display:grid;
      gap:22px;
    }}
    body[data-theme="dark"] .auth-card {{
      background:linear-gradient(180deg, rgba(9,16,28,.92), rgba(9,16,28,.78));
      border-color:rgba(148,163,184,.16);
      box-shadow:0 30px 80px rgba(0,0,0,.4);
    }}
    .auth-brand {{ display:flex; align-items:center; gap:12px; margin-bottom:4px; }}
    .auth-brand img {{ height:84px; width:auto; max-width:280px; object-fit:contain; }}
    .auth-brand-mark {{
      display:inline-grid;
      place-items:center;
      width:48px;
      height:48px;
      border-radius:14px;
      background:linear-gradient(135deg, #0f766e, #10b981);
      color:#fff;
      font-weight:900;
      font-size:22px;
      flex-shrink:0;
    }}
    .auth-brand-name {{ font-weight:900; letter-spacing:-.02em; font-size:19px; }}
    .auth-head {{ display:grid; gap:6px; }}
    .auth-head h1 {{ margin:0; font-size:1.6rem; }}
    .auth-head p {{ margin:0; color:var(--muted); font-size:14.5px; line-height:1.5; }}
    .auth-card form {{ gap:16px; }}
    .field {{ display:grid; gap:7px; text-align:left; }}
    .field label {{
      font-size:12px;
      font-weight:800;
      letter-spacing:.05em;
      text-transform:uppercase;
      color:var(--muted);
    }}
    .field input {{ margin:0; }}
    .auth-submit {{ margin-top:4px; }}
    .auth-links {{
      display:flex;
      justify-content:space-between;
      gap:14px;
      flex-wrap:wrap;
      font-size:13.5px;
    }}
    .auth-links a {{ color:var(--accent2); font-weight:700; }}
    .auth-links a:hover {{ color:var(--accent); text-decoration:underline; }}
    body[data-theme="dark"] .auth-links a {{ color:#5eead4; }}
    .auth-note {{ text-align:center; font-size:13px; color:var(--muted); }}
    @media (max-width: 480px) {{
      .auth-card {{ padding:30px 24px; border-radius:22px; }}
      .auth-shell {{ padding:20px 16px; }}
    }}
    """


def render_shell(title: str, content: str) -> str:
    logo_html = ""
    company_logo_url = current_company_branding_url()
    if company_logo_url:
        logo_html = f'<img src="{company_logo_url}" class="topbar-logo">'
    elif BRAND_LOGO.exists():
        logo_html = '<img src="/branding_logo.png?%d" class="topbar-logo">' % int(time.time())
    template = f"""
    <!doctype html>
    <html lang="nl">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{{{{ title }}}}</title>
      <style>{brand_css()}</style>
    </head>
    <body data-theme="light">
      <div class="wrap">
        <div class="topbar">
          <div class="brand">
            {logo_html}
            <div class="brand-text">
              <div class="brand-title">{BRAND['name']} Control Center</div>
              <div class="brand-sub">{BRAND['tagline']}</div>
            </div>
          </div>
          {{% if session.get('user_id') %}}
          <div class="header-actions">
            <button type="button" class="theme-toggle" id="themeToggle" aria-label="Thema wisselen" aria-pressed="false">Donker</button>
            <span class="badge">{{{{ session.get('company_view_name', session.get('company_name', 'Geen bedrijf')) }}}}</span>
            <span class="badge">{{{{ session.get('user_name', 'Admin') }}}} | {{{{ session.get('user_role', 'user') }}}}</span>
            <form method="post" action="{{{{ url_for('logout') }}}}">
              <button class="shell-action" type="submit">Uitloggen</button>
            </form>
          </div>
          {{% endif %}}
        </div>
        {{% if session.get('user_id') %}}
        <div class="nav">
          <a href="{{{{ url_for('dashboard') }}}}">Dashboard</a>
          <a href="{{{{ url_for('media_library') }}}}">Media</a>
          <a href="{{{{ url_for('playlist_manager') }}}}">Playlists</a>
          <a href="{{{{ url_for('screens_manager') }}}}">Screens</a>
          <a href="{{{{ url_for('schedules_manager') }}}}">Schedules</a>
          <a href="{{{{ url_for('feeds_manager') }}}}">Feeds</a>
          {{% if session.get('user_role') in ['superadmin', 'company_admin'] %}}
          <a href="{{{{ url_for('layout_studio') }}}}">Layout Studio</a>
          {{% endif %}}
          {{% if session.get('user_role') == 'superadmin' %}}
          <a href="{{{{ url_for('player_installer') }}}}">Player installer</a>
          <a href="{{{{ url_for('companies_manager') }}}}">Bedrijven</a>
          <a href="{{{{ url_for('superadmin_companies') }}}}">Superadmin bedrijven</a>
          <a href="{{{{ url_for('monitoring_page') }}}}">Monitoring</a>
          <a href="{{{{ url_for('screen_stats') }}}}">Schermstatistieken</a>
          <a href="{{{{ url_for('audit_logs_page') }}}}">Audit logs</a>
          {{% endif %}}
          <a href="{{{{ url_for('branding_settings') }}}}">Branding</a>
          {{% if session.get('user_role') in ['superadmin', 'company_admin'] %}}
          <a href="{{{{ url_for('users_manager') }}}}">Gebruikers</a>
          {{% endif %}}
        </div>
        {{% endif %}}
        {{% with messages = get_flashed_messages() %}}
          {{% if messages %}}
            {{% for message in messages %}}
              <div class="flash">{{{{ message }}}}</div>
            {{% endfor %}}
          {{% endif %}}
        {{% endwith %}}
        {content}
      </div>
          <script>
      (function() {{
        const key = 'salubcast-theme';
        const button = document.getElementById('themeToggle');
        const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
        const saved = localStorage.getItem(key);
        function applyTheme(theme) {{
          document.body.dataset.theme = theme;
          if (button) {{
            button.textContent = theme === 'dark' ? 'Licht' : 'Donker';
            button.setAttribute('aria-pressed', theme === 'dark' ? 'true' : 'false');
          }}
        }}
        applyTheme(saved || (prefersDark ? 'dark' : 'light'));
        if (button) {{
          button.addEventListener('click', () => {{
            const next = document.body.dataset.theme === 'dark' ? 'light' : 'dark';
            localStorage.setItem(key, next);
            applyTheme(next);
          }});
        }}
      }})();
      document.querySelectorAll('form[method="post"], form:not([method])').forEach((form) => {{
        if (form.querySelector('input[name="_csrf_token"]')) return;
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = '_csrf_token';
        input.value = {{{{ csrf_token()|tojson }}}};
        form.appendChild(input);
      }});
      </script>
    </body>
    </html>
    """
    return render_template_string(template, title=title)


def auth_brand_block() -> str:
    if BRAND_LOGO.exists():
        return f'<div class="auth-brand"><img src="/branding_logo.png?{int(time.time())}" alt="{BRAND["name"]}"></div>'
    return f'<div class="auth-brand"><span class="auth-brand-mark">{BRAND["name"][0]}</span><span class="auth-brand-name">{BRAND["name"]}</span></div>'


def render_auth_shell(title: str, content: str) -> str:
    template = f"""
    <!doctype html>
    <html lang="nl">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{{{{ title }}}}</title>
      <style>{brand_css()}</style>
    </head>
    <body data-theme="light">
      <button type="button" class="theme-toggle auth-theme-toggle" id="themeToggle" aria-label="Thema wisselen" aria-pressed="false">Donker</button>
      <div class="auth-shell">
        <div style="width:min(100%, 440px); display:grid; gap:0;">
          {{% with messages = get_flashed_messages() %}}
            {{% if messages %}}
            <div class="auth-flash">
              {{% for message in messages %}}
                <div class="flash">{{{{ message }}}}</div>
              {{% endfor %}}
            </div>
            {{% endif %}}
          {{% endwith %}}
          {content}
        </div>
      </div>
      <script>
      (function() {{
        const key = 'salubcast-theme';
        const button = document.getElementById('themeToggle');
        const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
        const saved = localStorage.getItem(key);
        function applyTheme(theme) {{
          document.body.dataset.theme = theme;
          if (button) {{
            button.textContent = theme === 'dark' ? 'Licht' : 'Donker';
            button.setAttribute('aria-pressed', theme === 'dark' ? 'true' : 'false');
          }}
        }}
        applyTheme(saved || (prefersDark ? 'dark' : 'light'));
        if (button) {{
          button.addEventListener('click', () => {{
            const next = document.body.dataset.theme === 'dark' ? 'light' : 'dark';
            localStorage.setItem(key, next);
            applyTheme(next);
          }});
        }}
      }})();
      document.querySelectorAll('form[method="post"], form:not([method])').forEach((form) => {{
        if (form.querySelector('input[name="_csrf_token"]')) return;
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = '_csrf_token';
        input.value = {{{{ csrf_token()|tojson }}}};
        form.appendChild(input);
      }});
      </script>
    </body>
    </html>
    """
    return render_template_string(template, title=title)


def create_player_package_zip(server_base_url: str, screen_id: str, screen_name: str, activation_code: str, screen_token: str) -> BytesIO:
    mem = BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "player.py",
            """import json\nimport os\nimport shutil\nimport subprocess\nimport sys\nfrom pathlib import Path\nfrom urllib.parse import urlencode\n\n\ndef app_dir() -> Path:\n    if getattr(sys, 'frozen', False):\n        return Path(sys.executable).resolve().parent\n    return Path(__file__).resolve().parent\n\n\nBASE_DIR = app_dir()\nCONFIG_PATH = BASE_DIR / 'player_config.json'\n\n\ndef load_config() -> dict:\n    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:\n        return json.load(f)\n\n\ndef save_config(cfg: dict) -> None:\n    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:\n        json.dump(cfg, f, indent=2)\n\n\ndef detect_browser() -> tuple[str, str]:\n    order = [\n        ('chromium', [shutil.which('chromium'), shutil.which('chromium-browser')]),\n        ('chrome', [shutil.which('google-chrome'), shutil.which('chrome')]),\n        ('edge', [shutil.which('microsoft-edge'), shutil.which('msedge')]),\n    ]\n    for browser_name, candidates in order:\n        for path in candidates:\n            if path:\n                return browser_name, path\n    raise FileNotFoundError('Geen ondersteunde browser gevonden. Installeer chromium, chrome of edge.')\n\n\ndef find_browser(name: str) -> tuple[str, str]:\n    requested = (name or '').strip().lower()\n    if requested in {'auto', ''}:\n        return detect_browser()\n    candidates = {\n        'edge': [shutil.which('microsoft-edge'), shutil.which('msedge'), r'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe', r'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'],\n        'chrome': [shutil.which('google-chrome'), shutil.which('chrome'), r'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', r'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe'],\n        'chromium': [shutil.which('chromium'), shutil.which('chromium-browser')],\n    }\n    for path in candidates.get(requested, []):\n        if path and Path(path).exists():\n            return requested, path\n        if path and not any(sep in path for sep in ('\\\\', '/')):\n            return requested, path\n    raise FileNotFoundError(f'Browser niet gevonden: {requested}')\n\n\ndef build_target_url(cfg: dict) -> str:\n    base = cfg['server_url'].rstrip('/')\n    screen_id = str(cfg.get('screen_id', '')).strip()\n    screen_name = str(cfg.get('screen_name', '')).strip()\n    screen_token = str(cfg.get('screen_token', '')).strip()\n    activation_code = str(cfg.get('activation_code', '')).strip()\n\n    if screen_id and screen_token:\n        cfg['activation_code'] = ''\n        cfg['activated'] = True\n        params = {'screen_id': screen_id, 'screen': screen_name, 'token': screen_token}\n        return f\"{base}/player?{urlencode(params)}\"\n\n    if activation_code:\n        cfg['activated'] = False\n        params = {'code': activation_code}\n        return f\"{base}/activate-player?{urlencode(params)}\"\n\n    if screen_id:\n        cfg['activated'] = False\n        params = {'screen_id': screen_id, 'screen': screen_name}\n        return f\"{base}/player?{urlencode(params)}\"\n\n    raise RuntimeError('Geen geldige player-identiteit gevonden. Vul activation_code of screen_id in.')\n\n\ndef browser_args(browser_name: str, browser_path: str, url: str) -> list[str]:\n    args = [browser_path]\n    if browser_name == 'edge':\n        args.extend(['--kiosk', url, '--edge-kiosk-type=fullscreen'])\n    else:\n        args.extend(['--kiosk', url])\n    args.extend(['--no-first-run', '--disable-infobars'])\n    return args\n\n\ndef main():\n    cfg = load_config()\n    browser_name, browser = find_browser(cfg.get('browser', 'auto'))\n    cfg['browser'] = browser_name\n    url = build_target_url(cfg)\n    save_config(cfg)\n    subprocess.Popen(browser_args(browser_name, browser, url), env=os.environ.copy())\n\n\nif __name__ == '__main__':\n    main()\n""",
        )
        z.writestr(
            "player_config.json",
            json.dumps(
                {
                    "server_url": server_base_url.rstrip("/"),
                    "screen_name": screen_name,
                    "screen_id": screen_id,
                    "screen_token": screen_token,
                    "activation_code": activation_code,
                    "activated": True,
                    "browser": "chrome",
                    "fullscreen": "true",
                },
                indent=2,
            ),
        )
        z.writestr("start_player.bat", "py player.py\npause\n")
        start_sh = zipfile.ZipInfo("start_player.sh")
        start_sh.compress_type = zipfile.ZIP_DEFLATED
        start_sh.external_attr = 0o100755 << 16
        z.writestr(
            start_sh,
            "#!/usr/bin/env bash\nset -euo pipefail\ncd \"$(dirname \"$0\")\"\npython3 ./player.py\n",
        )
        install_and_start_sh = zipfile.ZipInfo("install_and_start_linux.sh")
        install_and_start_sh.compress_type = zipfile.ZIP_DEFLATED
        install_and_start_sh.external_attr = 0o100755 << 16
        z.writestr(
            install_and_start_sh,
            "#!/usr/bin/env bash\nset -euo pipefail\ncd \"$(dirname \"$0\")\"\nbash ./install_linux.sh\nbash ./start_player.sh\n",
        )
        click_install_sh = zipfile.ZipInfo("Install_SalubCast_Player.sh")
        click_install_sh.compress_type = zipfile.ZIP_DEFLATED
        click_install_sh.external_attr = 0o100755 << 16
        z.writestr(
            click_install_sh,
            "#!/usr/bin/env bash\nset -euo pipefail\ncd \"$(dirname \"$0\")\"\nchmod +x ./install_linux.sh ./start_player.sh ./install_and_start_linux.sh ./open_player_launcher.sh\nbash ./install_and_start_linux.sh\n",
        )
        launcher_sh = zipfile.ZipInfo("open_player_launcher.sh")
        launcher_sh.compress_type = zipfile.ZIP_DEFLATED
        launcher_sh.external_attr = 0o100755 << 16
        z.writestr(
            launcher_sh,
            "#!/usr/bin/env bash\nset -euo pipefail\nLAUNCHER=\"${XDG_DATA_HOME:-$HOME/.local/share}/applications/salubcast-player.desktop\"\nif command -v gtk-launch >/dev/null 2>&1; then\n  gtk-launch salubcast-player\nelif command -v xdg-open >/dev/null 2>&1; then\n  xdg-open \"$LAUNCHER\"\nelse\n  echo 'Geen launcher gevonden. Gebruik: bash start_player.sh' >&2\n  exit 1\nfi\n",
        )
        install_desktop = zipfile.ZipInfo("Install_SalubCast_Player.desktop")
        install_desktop.compress_type = zipfile.ZIP_DEFLATED
        install_desktop.external_attr = 0o100755 << 16
        z.writestr(
            install_desktop,
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Version=1.0\n"
            "Name=Installeer SalubCast Player\n"
            "Comment=Installeer en start de SalubCast player\n"
            "Exec=/usr/bin/env sh -c 'cd \"$(dirname \"$1\")\" && chmod +x ./install_linux.sh ./start_player.sh ./install_and_start_linux.sh ./open_player_launcher.sh ./Install_SalubCast_Player.sh && bash ./install_and_start_linux.sh' dummy %k\n"
            "Terminal=true\n"
            "Icon=system-run\n"
            "Categories=AudioVideo;Video;\n",
        )
        install_sh = zipfile.ZipInfo("install_linux.sh")
        install_sh.compress_type = zipfile.ZIP_DEFLATED
        install_sh.external_attr = 0o100755 << 16
        z.writestr(
            install_sh,
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "BASE_DIR=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
            "CONFIG_PATH=\"$BASE_DIR/player_config.json\"\n"
            "DESKTOP_DIR=\"${XDG_DATA_HOME:-$HOME/.local/share}/applications\"\n"
            "DESKTOP_FILE=\"$DESKTOP_DIR/salubcast-player.desktop\"\n"
            "AUTOSTART_DIR=\"${XDG_CONFIG_HOME:-$HOME/.config}/autostart\"\n"
            "AUTOSTART_FILE=\"$AUTOSTART_DIR/salubcast-player.desktop\"\n"
            "SYSTEMD_DIR=\"${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user\"\n"
            "SYSTEMD_FILE=\"$SYSTEMD_DIR/salubcast-player.service\"\n"
            "\n"
            "if ! command -v python3 >/dev/null 2>&1; then\n"
            "  echo 'python3 ontbreekt. Installeer eerst Python 3.' >&2\n"
            "  exit 1\n"
            "fi\n"
            "\n"
            "mkdir -p \"$DESKTOP_DIR\" \"$AUTOSTART_DIR\" \"$SYSTEMD_DIR\"\n"
            "chmod +x \"$BASE_DIR/start_player.sh\"\n"
            "chmod +x \"$BASE_DIR/install_and_start_linux.sh\" 2>/dev/null || true\n"
            "chmod +x \"$BASE_DIR/Install_SalubCast_Player.sh\" 2>/dev/null || true\n"
            "chmod +x \"$BASE_DIR/open_player_launcher.sh\" 2>/dev/null || true\n"
            "chmod +x \"$BASE_DIR/Install_SalubCast_Player.desktop\" 2>/dev/null || true\n"
            "\n"
            "if command -v python3 >/dev/null 2>&1; then\n"
            "  python3 - <<'PY' \"$CONFIG_PATH\"\n"
            "import json, shutil, sys\n"
            "path = sys.argv[1]\n"
            "with open(path, 'r', encoding='utf-8') as f:\n"
            "    cfg = json.load(f)\n"
            "browser = str(cfg.get('browser', 'auto')).strip().lower()\n"
            "cfg['fullscreen'] = 'true'\n"
            "if any(shutil.which(cmd) for cmd in ['chromium', 'chromium-browser']):\n"
            "    cfg['browser'] = 'chromium'\n"
            "elif browser in {'', 'auto'}:\n"
            "    for name, commands in [('chrome', ['google-chrome', 'chrome']), ('edge', ['microsoft-edge', 'msedge'])]:\n"
            "        if any(shutil.which(cmd) for cmd in commands):\n"
            "            cfg['browser'] = name\n"
            "            break\n"
            "with open(path, 'w', encoding='utf-8') as f:\n"
            "    json.dump(cfg, f, indent=2)\n"
            "PY\n"
            "fi\n"
            "\n"
            "cat > \"$DESKTOP_FILE\" <<EOF\n"
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=SalubCast Player\n"
            "Comment=Start SalubCast narrowcasting player\n"
            "Exec=bash \"$BASE_DIR/start_player.sh\"\n"
            "Path=$BASE_DIR\n"
            "Terminal=false\n"
            "Categories=AudioVideo;Video;\n"
            "EOF\n"
            "\n"
            "chmod +x \"$DESKTOP_FILE\"\n"
            "cp \"$DESKTOP_FILE\" \"$AUTOSTART_FILE\"\n"
            "\n"
            "cat > \"$SYSTEMD_FILE\" <<EOF\n"
            "[Unit]\n"
            "Description=SalubCast Player\n"
            "After=graphical-session.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            "WorkingDirectory=$BASE_DIR\n"
            "ExecStart=/usr/bin/env bash $BASE_DIR/start_player.sh\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
            "EOF\n"
            "\n"
            "if command -v systemctl >/dev/null 2>&1; then\n"
            "  systemctl --user daemon-reload || true\n"
            "fi\n"
            "\n"
            "echo \"SalubCast Player geinstalleerd.\" \n"
            "echo \"Launcher: $DESKTOP_FILE\"\n"
            "echo \"Autostart: $AUTOSTART_FILE\"\n"
            "echo \"Systemd service: $SYSTEMD_FILE\"\n"
            "echo \"Voer dit niet direct uit als shellscript: $AUTOSTART_FILE\"\n"
            "echo \"Klikbaar shellbestand: '$BASE_DIR/Install_SalubCast_Player.sh'\"\n"
            "echo \"Handmatig starten: bash '$BASE_DIR/start_player.sh'\"\n"
            "echo \"Klikbaar installbestand: '$BASE_DIR/Install_SalubCast_Player.desktop'\"\n"
            "echo \"Alles-in-een script: bash '$BASE_DIR/install_and_start_linux.sh'\"\n"
            "echo \"Launcher starten: bash '$BASE_DIR/open_player_launcher.sh'\"\n"
            "echo \"Systemd activeren: systemctl --user enable --now salubcast-player.service\"\n",
        )
        z.writestr(
            "build_player.bat",
            "@echo off\ncd /d %~dp0\npy -3.12 -m venv venv\ncall venv\\Scripts\\activate\npython -m pip install --upgrade pip\npip install pyinstaller\nif exist build rmdir /s /q build\nif exist dist rmdir /s /q dist\nif exist *.spec del *.spec\npyinstaller --onefile --windowed --name SalubCastPlayer player.py\ncopy player_config.json dist\\player_config.json\npause\n",
        )
        z.writestr(
            "README_player.txt",
            (
                "SalubCast Player Package\n\n"
                "Windows:\n"
                "1. Installeer Python of gebruik py launcher.\n"
                "2. Dubbelklik start_player.bat\n\n"
                "Linux:\n"
                "1. Zorg dat python3 en een kiosk-browser beschikbaar zijn.\n"
                "2. Windows gebruikt standaard Chrome; Linux zet player_config.json automatisch op Chromium als Chromium beschikbaar is.\n"
                "3. Snelste klikbare optie: dubbelklik Install_SalubCast_Player.sh of Install_SalubCast_Player.desktop.\n"
                "4. Alles-in-een via terminal: bash install_and_start_linux.sh\n"
                "5. Installeer launcher en autostart los met: bash install_linux.sh\n"
                "6. Direct starten kan ook met: bash start_player.sh\n"
                "7. Launcher openen kan met: bash open_player_launcher.sh\n"
                "8. Voer ~/.config/autostart/salubcast-player.desktop niet uit als shellscript.\n"
                "9. Systemd user-service activeren: systemctl --user enable --now salubcast-player.service\n"
                "10. Als je './start_player.sh' wilt gebruiken: voer eerst 'chmod +x start_player.sh' uit.\n"
            ),
        )
    mem.seek(0)
    return mem




def refresh_feed(feed_row) -> int:
    parsed = feedparser.parse(feed_row["url"])
    execute("DELETE FROM feed_items WHERE feed_id = ?", (feed_row["id"],))
    count = 0
    for entry in parsed.entries[: int(feed_row["max_items"])]:
        title = str(entry.get("title", "")).strip() or "Onbekend bericht"
        link = str(entry.get("link", "")).strip()
        published = str(entry.get("published", "") or entry.get("updated", "") or "").strip()
        summary_html = (
            entry.get("summary")
            or entry.get("description")
            or entry.get("subtitle")
            or ""
        )
        summary = re.sub(r"<[^>]+>", " ", str(summary_html))
        summary = " ".join(summary.replace("&nbsp;", " ").split())
        execute(
            "INSERT INTO feed_items (id, feed_id, title, link, summary, published_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), feed_row["id"], title, link, summary[:1200], published, now_iso()),
        )
        count += 1
    execute("UPDATE feeds SET last_fetched_at = ? WHERE id = ?", (now_iso(), feed_row["id"]))
    return count


def weather_code_label(code: int | None) -> str:
    mapping = {
        0: 'Helder', 1: 'Vrij helder', 2: 'Licht bewolkt', 3: 'Bewolkt',
        45: 'Mist', 48: 'Nevel', 51: 'Motregen', 53: 'Motregen', 55: 'Zware motregen',
        61: 'Lichte regen', 63: 'Regen', 65: 'Zware regen', 71: 'Lichte sneeuw',
        73: 'Sneeuw', 75: 'Zware sneeuw', 80: 'Buien', 81: 'Buien', 82: 'Zware buien', 95: 'Onweer'
    }
    return mapping.get(code, 'Weerupdate')


def get_weather_summary(city: str) -> dict[str, str] | None:
    city = (city or '').strip()
    if not city:
        return None
    key = city.lower()
    now_ts = time.time()
    cached = _weather_cache.get(key)
    if cached and now_ts - cached[0] < WEATHER_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        geo_url = 'https://geocoding-api.open-meteo.com/v1/search?' + urllib.parse.urlencode({'name': city, 'count': 1, 'language': 'nl', 'format': 'json'})
        with urllib.request.urlopen(geo_url, timeout=6) as resp:
            geo = json.loads(resp.read().decode('utf-8'))
        results = geo.get('results') or []
        if not results:
            return None
        first = results[0]
        wx_url = 'https://api.open-meteo.com/v1/forecast?' + urllib.parse.urlencode({
            'latitude': first.get('latitude'),
            'longitude': first.get('longitude'),
            'current': 'temperature_2m,apparent_temperature,weather_code,wind_speed_10m',
            'timezone': 'auto',
        })
        with urllib.request.urlopen(wx_url, timeout=6) as resp:
            wx = json.loads(resp.read().decode('utf-8'))
        current = wx.get('current') or {}
        summary = {
            'city': first.get('name') or city,
            'temperature': f"{round(float(current.get('temperature_2m', 0)))} C",
            'feels_like': f"{round(float(current.get('apparent_temperature', 0)))} C",
            'wind': f"{round(float(current.get('wind_speed_10m', 0)))} km/u",
            'condition': weather_code_label(current.get('weather_code')),
        }
        _weather_cache[key] = (now_ts, summary)
        return summary
    except Exception:
        return None


def refresh_feed_if_due(feed_row) -> int:
    try:
        refresh_seconds = max(30, int(feed_row['refresh_seconds'] or 300))
    except Exception:
        refresh_seconds = 300
    last_fetched = parse_iso(feed_row['last_fetched_at'])
    now_dt = datetime.now(timezone.utc)
    if last_fetched and (now_dt - last_fetched).total_seconds() < refresh_seconds:
        return 0
    try:
        return refresh_feed(feed_row)
    except Exception:
        return 0


def get_feed_page_entries(company_id: str, limit: int = 6) -> tuple[str, list[dict[str, str]]]:
    feed = fetch_one(
        "SELECT * FROM feeds WHERE company_id = ? AND is_active = 1 AND is_ticker = 1 ORDER BY created_at DESC LIMIT 1",
        (company_id,),
    ) or fetch_one(
        "SELECT * FROM feeds WHERE company_id = ? AND is_active = 1 ORDER BY created_at DESC LIMIT 1",
        (company_id,),
    )
    if not feed:
        return "", []
    refresh_feed_if_due(feed)
    items = fetch_all(
        "SELECT title, link, summary, published_at FROM feed_items WHERE feed_id = ? ORDER BY created_at ASC LIMIT ?",
        (feed["id"], limit),
    )
    return feed["name"], [{"title": i["title"], "link": i["link"] or "", "summary": i["summary"] or "", "published_at": i["published_at"] or ""} for i in items]


def _feed_entry_weight(entry: dict[str, str]) -> int:
    title = str(entry.get("title") or "").strip()
    summary = str(entry.get("summary") or "").strip()
    score = len(title) + min(len(summary), 260)
    if len(summary) > 220:
        score += 40
    return max(80, score)


def chunk_feed_entries(entries: list[dict[str, str]], chunk_size: int, orientation: str = "landscape") -> list[list[dict[str, str]]]:
    if not entries:
        return []
    if chunk_size <= 0:
        return [entries]

    normalized_orientation = (orientation or "landscape").strip().lower()
    max_items = max(1, chunk_size)
    max_weight = 700 if normalized_orientation == "portrait" else 880
    pages: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    current_weight = 0

    for entry in entries:
        entry_weight = _feed_entry_weight(entry)
        needs_new_page = bool(
            current and (
                len(current) >= max_items or
                current_weight + entry_weight > max_weight
            )
        )
        if needs_new_page:
            pages.append(current)
            current = []
            current_weight = 0
        current.append(entry)
        current_weight += entry_weight

    if current:
        pages.append(current)
    return pages

def get_ticker_text_for_screen(screen: sqlite3.Row | None) -> str:
    if not screen:
        return ""
    company = fetch_one("SELECT * FROM companies WHERE id = ? LIMIT 1", (screen["company_id"],))
    if not company or company["is_active"] != 1:
        return ""
    feed = fetch_one(
        "SELECT * FROM feeds WHERE company_id = ? AND is_active = 1 AND is_ticker = 1 ORDER BY created_at DESC LIMIT 1",
        (screen["company_id"],),
    )
    if not feed:
        return ""
    refresh_feed_if_due(feed)
    items = fetch_all("SELECT title FROM feed_items WHERE feed_id = ? ORDER BY created_at ASC", (feed["id"],))
    return " | ".join(item["title"] for item in items) if items else ""
@app.context_processor
def inject_security_helpers():
    return {'csrf_token': csrf_token, 'company_branding_url': current_company_branding_url()}


@app.before_request
def enforce_csrf():
    if request.method != "POST":
        return None
    if request.endpoint in {"heartbeat"}:
        return None
    sent = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
    if not sent or not hmac.compare_digest(sent, csrf_token()):
        return Response("CSRF-validatie mislukt.", status=400)
    return None


@app.errorhandler(413)
def too_large(_error):
    return render_shell("Upload te groot", '<div class="card"><h1>Upload te groot</h1><p class="muted">Bestand overschrijdt de ingestelde limiet.</p></div>'), 413


def actor_label() -> str:
    return session.get("user_name") or "system"


# -----------------------------
# Auth + registration
# -----------------------------
