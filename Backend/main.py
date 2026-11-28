import base64
import concurrent.futures
import hashlib
import hmac
import json
import math
import os
import random
import re
import secrets
import shutil
import smtplib
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg
import yt_dlp

try:  # optional: only needed when DATABASE_URL is set
    import psycopg
except Exception:  # pragma: no cover
    psycopg = None
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi

# ============================================================
# SETTINGS (Easy to edit!)
# ============================================================
MAX_HEIGHT = 1080       # 1080 (best compatibility) or 1440
CAPTIONS = True         # Set False to skip burning captions
CLIP_DURATION = 30      # Default clip length in seconds
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days

# ---- ENCODING PROFILE (auto-tuned for cloud free tiers with tiny CPUs) ----
# Render Free = 0.1 CPU. "veryfast" 1080p there takes many minutes, so we
# automatically switch to "ultrafast" on Render unless you override it.
LOW_CPU = (
    os.getenv("LOW_CPU", "").strip().lower() in ("1", "true", "yes", "on")
    or bool(os.getenv("RENDER"))
)
ENCODE_PRESET = os.getenv("ENCODE_PRESET", "").strip() or (
    "ultrafast" if LOW_CPU else "veryfast"
)
ENCODE_CRF = os.getenv("ENCODE_CRF", "").strip() or ("24" if LOW_CPU else "21")
# Generous encode timeout: slow cloud CPUs need minutes, not seconds.
ENCODE_TIMEOUT = int(os.getenv("ENCODE_TIMEOUT", "600" if LOW_CPU else "240"))

# Add your admin email(s) here (the very first registered account is also auto-admin!)
ADMIN_EMAILS = {"rahulgoudapatil7@gmail.com"}

# REAL GMAIL EMAIL SENDING FOR FORGOT PASSWORD (100% secure — code is NEVER shown on the website!)
# Create a 16-character Google App Password at: https://myaccount.google.com/apppasswords
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "rahulgoudapatil7@gmail.com")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")  # Paste your 16-char Gmail App Password here (no spaces)

# CLOUD YOUTUBE BYPASS (for Render/Railway where YouTube blocks datacenter IPs)
# Option A: Set PROXY_URL env var in Render (e.g. http://user:pass@p.webshare.io:80)
# Option B: Set YOUTUBE_COOKIES or YOUTUBE_COOKIES_B64 env var in Render (exported Netscape cookies.txt)
PROXY_URL = os.getenv("PROXY_URL", "").strip()

# Max size for an uploaded video (Render Free disk/RAM are small; keep this sane).
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1024"))   # 1 GB default

# ============================================================
# FREE PROXY POOL — auto-discovers free proxies that YouTube accepts.
# Used ONLY when PROXY_URL is empty (a paid proxy always wins).
# ============================================================
FREE_POOL_ENABLED = os.getenv("FREE_PROXY_POOL", "1").strip().lower() not in (
    "0", "false", "no", "off"
)
FREE_POOL_TTL = int(os.getenv("FREE_PROXY_POOL_TTL", "1800"))       # refresh every 30 min
# Proxies that have already proven they can serve video bytes get tested first.
FREE_PROXY_SEEDS = [
    s.strip()
    for s in os.getenv(
        "FREE_PROXY_SEEDS", "189.51.168.165:999,89.106.84.72:1081,102.244.78.61:8089"
    ).split(",")
    if s.strip()
]
def _free_pool_path() -> "Path":
    return DATA_DIR / "proxy_pool.json"
FREE_PROXY_SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
]
PROXY_POOL: dict[str, Any] = {
    "proxies": [],
    "idx": 0,
    "updated_at": 0.0,
    "status": "idle",
    "candidates": 0,
    "reachable": 0,
    "last_error": "",
}
POOL_LOCK = threading.Lock()


def _pool_load() -> None:
    try:
        if _free_pool_path().exists():
            data = json.loads(_free_pool_path().read_text())
            with POOL_LOCK:
                PROXY_POOL.update(
                    {
                        "proxies": data.get("proxies", []),
                        "updated_at": data.get("updated_at", 0.0),
                        "status": "ready" if data.get("proxies") else "idle",
                        "candidates": data.get("candidates", 0),
                        "reachable": data.get("reachable", 0),
                    }
                )
            print(f"[FREE POOL] loaded {len(PROXY_POOL['proxies'])} proxies from disk")
    except Exception as exc:
        print(f"[FREE POOL] load failed: {exc}")


def _pool_save() -> None:
    try:
        with POOL_LOCK:
            payload = {
                "proxies": list(PROXY_POOL["proxies"]),
                "updated_at": PROXY_POOL["updated_at"],
                "candidates": PROXY_POOL["candidates"],
                "reachable": PROXY_POOL["reachable"],
            }
        _free_pool_path().write_text(json.dumps(payload))
    except Exception as exc:
        print(f"[FREE POOL] save failed: {exc}")


def get_active_proxy() -> str:
    """Paid PROXY_URL wins; otherwise use the current free-pool proxy."""
    if PROXY_URL:
        return PROXY_URL
    with POOL_LOCK:
        proxies = list(PROXY_POOL.get("proxies") or [])
        idx = int(PROXY_POOL.get("idx") or 0)
    if not proxies:
        return ""
    return proxies[idx % len(proxies)]


def _rotate_free_proxy() -> str:
    """Advance to the next free proxy (used after a failure)."""
    if PROXY_URL:
        return PROXY_URL
    with POOL_LOCK:
        proxies = list(PROXY_POOL.get("proxies") or [])
        if proxies:
            PROXY_POOL["idx"] = (int(PROXY_POOL.get("idx") or 0) + 1) % len(proxies)
            return proxies[int(PROXY_POOL["idx"])]
    return ""


def _using_free_pool() -> bool:
    return not bool(PROXY_URL) and bool(get_active_proxy())


def _pool_fetch_candidates(limit: int = 400) -> list[str]:
    found: list[str] = []
    for url in FREE_PROXY_SOURCES:
        try:
            with httpx.Client(timeout=25, follow_redirects=True) as client:
                text = client.get(url).text
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                line = re.sub(r"^\w+://", "", line)
                if re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d{2,5}$", line):
                    found.append(line)
        except Exception as exc:
            print(f"[FREE POOL] source failed ({url.split('/')[4][:20]}): {exc}")
    # de-duplicate, shuffle so we sample different proxies each refresh
    found = list(dict.fromkeys(found))
    random.shuffle(found)
    # Seeds (previously proven) go first so they're always re-tested.
    head = [s for s in FREE_PROXY_SEEDS if s not in found]
    return head + found[: max(0, limit - len(head))]


def _probe_reachable(addr: str) -> bool:
    try:
        with httpx.Client(proxy=f"http://{addr}", timeout=8, follow_redirects=False) as client:
            res = client.get("https://www.youtube.com/generate_204")
        return res.status_code in (200, 204)
    except Exception:
        return False


def _probe_youtube(addr: str) -> bool:
    """A proxy is only useful if it can (a) read player data AND (b) fetch media bytes.

    Many free proxies pass step (a) but return 403 on googlevideo, which is exactly
    what made clips fail. So we range-request a few hundred KB from the CDN too.
    """
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "proxy": f"http://{addr}",
            "socket_timeout": 15,
            "retries": 0,
            "extractor_retries": 0,
            "source_address": "0.0.0.0",
            "format": "bestvideo[height<=720][ext=mp4]/best[height<=720]/best",
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                "https://www.youtube.com/watch?v=Dz0naoD1QWo", download=False
            )
        req = info.get("requested_formats") or []
        media = (req[0] if req else info).get("url")
        headers = (req[0] if req else info).get("http_headers") or {}
        if not media:
            return False
        headers = dict(headers)
        headers["Range"] = "bytes=0-262143"
        with httpx.Client(proxy=f"http://{addr}", timeout=25, follow_redirects=True) as client:
            res = client.get(media, headers=headers)
        ok = res.status_code in (200, 206) and len(res.content) > 50_000
        if not ok:
            print(f"[FREE POOL]   ✗ {addr} player ok but media {res.status_code}")
        return ok
    except Exception as exc:
        if "403" in str(exc):
            print(f"[FREE POOL]   ✗ {addr} media 403")
        return False


def refresh_free_pool(max_candidates: int = 600, max_deep: int = 24) -> dict[str, Any]:
    """Fetch free proxy lists, keep only the ones that really work with YouTube."""
    with POOL_LOCK:
        if PROXY_POOL.get("status") == "testing":
            return {"status": "already testing"}
        PROXY_POOL["status"] = "testing"
        PROXY_POOL["last_error"] = ""

    started = time.time()
    try:
        print("[FREE POOL] refreshing: fetching free proxy lists...")
        candidates = _pool_fetch_candidates(max_candidates)
        with POOL_LOCK:
            PROXY_POOL["candidates"] = len(candidates)
        if not candidates:
            raise RuntimeError("no candidates found")

        print(f"[FREE POOL] testing {len(candidates)} proxies for YouTube reachability...")
        reachable: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=60) as ex:
            for addr, ok in zip(candidates, ex.map(_probe_reachable, candidates)):
                if ok:
                    reachable.append(addr)
        with POOL_LOCK:
            PROXY_POOL["reachable"] = len(reachable)
        print(f"[FREE POOL] {len(reachable)} reachable -> deep-testing {min(len(reachable), max_deep)}")

        sample = reachable[:max_deep]
        working: list[str] = []
        if sample:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                for addr, ok in zip(sample, ex.map(_probe_youtube, sample)):
                    if ok:
                        working.append(addr)
                        print(f"[FREE POOL]   ✅ {addr}")

        with POOL_LOCK:
            if working:
                PROXY_POOL["proxies"] = working
                PROXY_POOL["updated_at"] = time.time()
                PROXY_POOL["status"] = "ready"
            else:
                PROXY_POOL["status"] = "ready" if PROXY_POOL["proxies"] else "empty"
        if working:
            _pool_save()
        print(f"[FREE POOL] done in {time.time()-started:.0f}s -> {len(working)} working proxies")
        return {"working": working, "elapsed": round(time.time() - started, 1)}
    except Exception as exc:
        with POOL_LOCK:
            PROXY_POOL["status"] = "error"
            PROXY_POOL["last_error"] = str(exc)[:200]
        print(f"[FREE POOL] refresh failed: {exc}")
        return {"error": str(exc)[:200]}


def _pool_keeper() -> None:
    """Background loop: keep the pool fresh, but only when no paid proxy is set."""
    time.sleep(5)
    while True:
        try:
            if FREE_POOL_ENABLED and not PROXY_URL:
                stale = time.time() - float(PROXY_POOL.get("updated_at") or 0) > FREE_POOL_TTL
                if stale and PROXY_POOL.get("status") != "testing":
                    refresh_free_pool()
        except Exception as exc:
            print(f"[FREE POOL] keeper error: {exc}")
        time.sleep(120)



# Bumped whenever backend behaviour changes — lets us confirm what's actually deployed.
BUILD_TAG = "2026-11-29-gmail-otp-verify"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
CLIPS_DIR = DATA_DIR / "clips"
UPLOADS_DIR = DATA_DIR / "uploads"
THUMBS_DIR = DATA_DIR / "thumbs"
FFMPEG_DIR = BASE_DIR / "ffmpeg_bin"
DB_PATH = DATA_DIR / "viralcut.db"
SECRET_PATH = DATA_DIR / ".auth_secret"

# Read the DB URL early — the JWT secret helper below needs it.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
COOKIES_PATH = DATA_DIR / "cookies.txt"
FRONTEND_DIST = BASE_DIR.parent / "frontend" / "dist"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CLIPS_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
THUMBS_DIR.mkdir(parents=True, exist_ok=True)
FFMPEG_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_netscape_cookies(raw: str) -> tuple[str, int]:
    """Repair pasted Netscape cookies (adds header if missing, fixes spaces->tabs) and count valid cookies."""
    from http.cookiejar import MozillaCookieJar
    import tempfile

    text = raw.strip().lstrip("\ufeff")
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n").replace("\\t", "\t")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    out_lines = ["# Netscape HTTP Cookie File"]
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            # Preserve #HttpOnly_ cookie lines!
            if not stripped.startswith("#HttpOnly_"):
                continue
        if "\t" in stripped:
            parts = stripped.split("\t")
            if len(parts) >= 7:
                out_lines.append("\t".join(parts[:6] + ["\t".join(parts[6:])]))
                continue
        # Fallback if textarea converted tabs to spaces
        ws_parts = stripped.split(None, 6)
        if len(ws_parts) == 7 and ws_parts[1].upper() in ("TRUE", "FALSE") and ws_parts[3].upper() in ("TRUE", "FALSE"):
            out_lines.append("\t".join(ws_parts))

    normalized = "\n".join(out_lines) + "\n"
    count = 0
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as tf:
            tf.write(normalized)
            tmp_path = tf.name
        try:
            cj = MozillaCookieJar(tmp_path)
            cj.load(ignore_discard=True, ignore_expires=True)
            count = len(cj)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    except Exception as exc:
        print(f"COOKIES CHECK: MozillaCookieJar validation warning -> {exc}")
    return normalized, count


YOUTUBE_COOKIES_COUNT = 0
YOUTUBE_AUTH_COOKIES_OK = False


def _has_youtube_auth_cookies(normalized_text: str) -> bool:
    auth_names = {"SAPISID", "SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO"}
    for line in normalized_text.splitlines():
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5] in auth_names and parts[6].strip():
            return True
    return False


def setup_youtube_cookies() -> str | None:
    """Load YouTube cookies from Render Secret File, YOUTUBE_COOKIES_B64, YOUTUBE_COOKIES, or local cookies.txt."""
    global YOUTUBE_COOKIES_COUNT, YOUTUBE_AUTH_COOKIES_OK

    # 1. Check Render Secret Files (/etc/secrets/cookies.txt) first — zero paste corruption!
    for secret_file in (Path("/etc/secrets/cookies.txt"), BASE_DIR / "cookies.txt"):
        if secret_file.exists() and secret_file.stat().st_size > 20:
            raw_text = secret_file.read_text(encoding="utf-8", errors="ignore")
            norm, cnt = _normalize_netscape_cookies(raw_text)
            COOKIES_PATH.write_text(norm, encoding="utf-8")
            YOUTUBE_COOKIES_COUNT = cnt
            YOUTUBE_AUTH_COOKIES_OK = _has_youtube_auth_cookies(norm)
            print(f"COOKIES CHECK: Loaded {cnt} cookies (auth={YOUTUBE_AUTH_COOKIES_OK}) from file {secret_file}")
            return str(COOKIES_PATH)

    # 2. Check environment variables (YOUTUBE_COOKIES_B64 or YOUTUBE_COOKIES)
    b64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    raw = os.getenv("YOUTUBE_COOKIES", "").strip()
    try:
        source_label = ""
        decoded = ""
        if b64:
            clean_b64 = re.sub(r"\s+", "", b64)
            decoded = base64.b64decode(clean_b64).decode("utf-8", errors="ignore")
            source_label = "YOUTUBE_COOKIES_B64"
        elif raw:
            decoded = raw
            source_label = "YOUTUBE_COOKIES"

        if decoded.strip():
            norm, cnt = _normalize_netscape_cookies(decoded)
            COOKIES_PATH.write_text(norm, encoding="utf-8")
            YOUTUBE_COOKIES_COUNT = cnt
            YOUTUBE_AUTH_COOKIES_OK = _has_youtube_auth_cookies(norm)
            print(f"COOKIES CHECK: Loaded {cnt} cookies (auth={YOUTUBE_AUTH_COOKIES_OK}) from {source_label} ({COOKIES_PATH})")
            return str(COOKIES_PATH)
    except Exception as exc:
        print(f"COOKIES CHECK: Failed reading env cookies -> {exc}")

    if COOKIES_PATH.exists() and COOKIES_PATH.stat().st_size > 20:
        raw_text = COOKIES_PATH.read_text(encoding="utf-8", errors="ignore")
        norm, cnt = _normalize_netscape_cookies(raw_text)
        YOUTUBE_COOKIES_COUNT = cnt
        YOUTUBE_AUTH_COOKIES_OK = _has_youtube_auth_cookies(norm)
        print(f"COOKIES CHECK: Using existing file {COOKIES_PATH} ({cnt} cookies, auth={YOUTUBE_AUTH_COOKIES_OK})")
        return str(COOKIES_PATH)
    return None


YOUTUBE_COOKIE_FILE = setup_youtube_cookies()
if PROXY_URL:
    print("PROXY CHECK: Residential/HTTP proxy enabled for YouTube requests")


# ============================================================
# STARTUP CHECKS: FFMPEG & FONT (Windows + Linux)
# ============================================================
def setup_ffmpeg() -> str | None:
    """Copy imageio-ffmpeg binary as ffmpeg.exe (Windows) or ffmpeg (Linux) into Backend/ffmpeg_bin and add to PATH."""
    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    target = FFMPEG_DIR / exe_name
    try:
        src = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if not target.exists() or target.stat().st_size != src.stat().st_size:
            shutil.copy2(src, target)
            if os.name != "nt":
                os.chmod(target, 0o755)
        os.environ["PATH"] = str(FFMPEG_DIR) + os.pathsep + os.environ.get("PATH", "")
        res = subprocess.run([str(target), "-version"], capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            first_line = (res.stdout or "").splitlines()[0] if res.stdout else "OK"
            print(f"FFMPEG CHECK: OK ({first_line})")
            return str(target)
        print(f"FFMPEG CHECK: cannot run -> {(res.stderr or '')[:200]}")
        return None
    except Exception as exc:
        print(f"FFMPEG CHECK: cannot run -> {exc}")
        return None


def detect_caption_fonts() -> dict[str, tuple[str | None, str]]:
    """Detect best premium fonts for Latin (English) and Indic (Hindi/Devanagari) on Windows & Linux."""
    indic_candidates = [
        (Path("C:/Windows/Fonts/NirmalaB.ttf"), "Nirmala UI"),
        (Path("C:/Windows/Fonts/Nirmala.ttf"), "Nirmala UI"),
        (Path("C:/Windows/Fonts/Nirmala.ttc"), "Nirmala UI"),
        (Path("C:/Windows/Fonts/mangalb.ttf"), "Mangal"),
        (Path("C:/Windows/Fonts/mangal.ttf"), "Mangal"),
        (Path("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf"), "Noto Sans Devanagari"),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"), "DejaVu Sans"),
    ]
    latin_candidates = [
        (Path("C:/Windows/Fonts/segoeuib.ttf"), "Segoe UI"),
        (Path("C:/Windows/Fonts/bahnschrift.ttf"), "Bahnschrift"),
        (Path("C:/Windows/Fonts/NirmalaB.ttf"), "Nirmala UI"),
        (Path("C:/Windows/Fonts/arialbd.ttf"), "Arial"),
        (Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"), "Liberation Sans"),
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"), "DejaVu Sans"),
    ]

    def pick(cands):
        for p, label in cands:
            if p.exists():
                return str(p), label
        return None, "Sans"

    indic = pick(indic_candidates)
    latin = pick(latin_candidates)
    print(f"FONT CHECK: Latin={latin[1]} | Hindi/Indic={indic[1]}")
    return {"latin": latin, "indic": indic}


FFMPEG_BIN = setup_ffmpeg()
FONTS = detect_caption_fonts()
FONT_FILE, FONT_NAME = FONTS["indic"]


# ============================================================
# DATABASE & AUTH HELPERS (Built-in SQLite + PBKDF2 + HMAC)
# ============================================================
def get_secret_key() -> bytes:
    """Stable JWT signing key.

    Order of preference:
      1. AUTH_SECRET env var (best: never changes, survives everything)
      2. Stored in the database (survives Render restarts when Postgres is used)
      3. A local file (last resort on free hosting: rotates on every restart,
         which logs everybody out)
    """
    env_secret = os.getenv("AUTH_SECRET", "").strip()
    if env_secret:
        return hashlib.sha256(env_secret.encode("utf-8")).digest()

    if DATABASE_URL and psycopg is not None:
        try:
            with get_db() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                row = conn.execute(
                    "SELECT value FROM app_settings WHERE key = ?", ("jwt_secret",)
                ).fetchone()
                if row and row["value"]:
                    return bytes.fromhex(row["value"])
                new_key = secrets.token_bytes(32)
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
                    ("jwt_secret", new_key.hex()),
                )
                row = conn.execute(
                    "SELECT value FROM app_settings WHERE key = ?", ("jwt_secret",)
                ).fetchone()
                if row and row["value"]:
                    print("[AUTH] JWT secret loaded from the database (stable across restarts)")
                    return bytes.fromhex(row["value"])
        except Exception as exc:
            print(f"[AUTH] could not use DB for the signing key ({str(exc)[:120]}); using a local file")

    if SECRET_PATH.exists():
        return SECRET_PATH.read_bytes()
    key = secrets.token_bytes(32)
    try:
        SECRET_PATH.write_bytes(key)
    except Exception:
        pass
    return key


SECRET_KEY = get_secret_key()


# ---------------- DATABASE ----------------
# Render's free disk is wiped every time the instance restarts or sleeps, which
# deleted every account and project ("incorrect password" -> register again).
# If DATABASE_URL is set (free Neon / Supabase / Render Postgres) we store
# everything there instead. All existing SQLite-style SQL keeps working.
DB_WARNING = ""
if DATABASE_URL and psycopg is None:
    DB_WARNING = (
        "DATABASE_URL is set but the 'psycopg' package is missing, so accounts are still "
        "temporary. Add psycopg[binary] to your Dockerfile / requirements install."
    )
    print(f"[DB] WARNING: {DB_WARNING}")
DB_MODE = "postgres" if (DATABASE_URL and psycopg is not None) else "sqlite-ephemeral"


def _is_duplicate_error(exc: Exception) -> bool:
    """True for both SQLite's IntegrityError and Postgres' UniqueViolation."""
    name = type(exc).__name__
    text = str(exc).lower()
    return (
        name in ("IntegrityError", "UniqueViolation", "UniqueViolationError")
        or "unique constraint" in text
        or "duplicate key" in text
        or "unique failed" in text
    )


def _pg_sql(sql: str) -> str:
    """Translate the handful of SQLite-isms we use into Postgres."""
    out = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "SERIAL PRIMARY KEY", sql, flags=re.I)
    m = re.match(r"\s*PRAGMA\s+table_info\(\s*['\"]?(\w+)['\"]?\s*\)", out, re.I)
    if m:
        return (
            "SELECT column_name AS name FROM information_schema.columns "
            f"WHERE table_name = '{m.group(1)}'"
        )
    return out.replace("?", "%s")


class _PGCursor:
    def __init__(self, cur):
        self._cur = cur
        self._lastrowid = None
        self._rowid_read = False

    @property
    def lastrowid(self):
        if not self._rowid_read:
            self._rowid_read = True
            try:
                row = self._cur.fetchone()
                if row:
                    self._lastrowid = row.get("id") if isinstance(row, dict) else row[0]
            except Exception:
                self._lastrowid = None
        return self._lastrowid

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur.fetchall())


class _PGConn:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql: str, params=()):
        stmt = _pg_sql(sql).strip().rstrip(";")
        lowered = stmt.lower()
        if (
            lowered.startswith("insert")
            and "returning" not in lowered
            and "on conflict" not in lowered
        ):
            stmt = stmt + " RETURNING id"
        cur = self._raw.cursor()
        try:
            cur.execute(stmt, params if params else None)
        except Exception:
            # The failed statement aborts the transaction in Postgres, so we must
            # roll back before touching the connection again — otherwise the real
            # error is replaced by a confusing InFailedSqlTransaction.
            try:
                self._raw.rollback()
            except Exception:
                pass
            if "RETURNING id" in stmt:
                cur = self._raw.cursor()
                cur.execute(stmt.replace(" RETURNING id", ""), params if params else None)
            else:
                raise
        return _PGCursor(cur)

    def executescript(self, script: str):
        for part in [p.strip() for p in script.split(";") if p.strip()]:
            self.execute(part)

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()


_PG_FAILED = False


def _db_mode() -> str:
    if DATABASE_URL and psycopg is not None and not _PG_FAILED:
        return "postgres"
    return "sqlite-ephemeral"


@contextmanager
def get_db():
    global _PG_FAILED
    if DATABASE_URL and psycopg is not None and not _PG_FAILED:
        from psycopg.rows import dict_row

        try:
            raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        except Exception as exc:
            # A bad URL/password must never take the whole site down.
            _PG_FAILED = True
            print(
                f"[DB] Postgres unavailable ({str(exc)[:160]}). "
                "Falling back to local SQLite for this run — accounts will be temporary."
            )
        else:
            conn = _PGConn(raw)
            try:
                yield conn
                raw.commit()
            except Exception:
                try:
                    raw.rollback()
                except Exception:
                    pass
                raise
            finally:
                raw.close()
            return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _table_columns(conn, table: str) -> set[str]:
    try:
        if DATABASE_URL and psycopg is not None:
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,)
            ).fetchall()
            return {r["column_name"] for r in rows}
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"] for r in rows}
    except Exception as exc:
        print(f"[DB] could not read columns for {table}: {exc}")
        return set()


def _ensure_column(conn, table: str, column: str, decl: str) -> bool:
    """Add a column to an existing table. Returns True when it had to be created."""
    if column in _table_columns(conn, table):
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        print(f"[DB] migrated: {table}.{column} added")
        return True
    except Exception as exc:
        print(f"[DB] migrate {table}.{column} failed: {exc}")
        return False


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS project_thumbs (project_id INTEGER PRIMARY KEY, jpeg_b64 TEXT NOT NULL)"
        )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                video_id TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                thumbnail TEXT,
                duration REAL DEFAULT 0,
                language TEXT DEFAULT 'en',
                top_score INTEGER DEFAULT 0,
                suggestions_json TEXT NOT NULL,
                transcript_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS clips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                project_id INTEGER,
                video_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                hashtags_json TEXT DEFAULT '[]',
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                score INTEGER DEFAULT 85,
                filename TEXT NOT NULL,
                has_captions INTEGER DEFAULT 0,
                quality TEXT DEFAULT '1080p',
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS password_resets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                code TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                attempts INTEGER DEFAULT 0,
                used INTEGER DEFAULT 0
            );
            """
        )
        # Safe auto-migrations for existing viralcut.db files
        reset_cols = {r["name"] for r in conn.execute("PRAGMA table_info(password_resets)").fetchall()}
        if "attempts" not in reset_cols:
            conn.execute("ALTER TABLE password_resets ADD COLUMN attempts INTEGER DEFAULT 0")
        user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "is_admin" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")

        # Ensure the very first user account (site owner) is always an admin!
        first_user = conn.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()
        if first_user:
            conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (first_user["id"],))
        for admin_email in ADMIN_EMAILS:
            conn.execute("UPDATE users SET is_admin = 1 WHERE LOWER(email) = ?", (admin_email.lower(),))

        clip_cols = {r["name"] for r in conn.execute("PRAGMA table_info(clips)").fetchall()}
        if "description" not in clip_cols:
            conn.execute("ALTER TABLE clips ADD COLUMN description TEXT DEFAULT ''")
        if "hashtags_json" not in clip_cols:
            conn.execute("ALTER TABLE clips ADD COLUMN hashtags_json TEXT DEFAULT '[]'")

        proj_cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
        if "clip_length" not in proj_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN clip_length INTEGER DEFAULT 30")
        if "max_clips" not in proj_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN max_clips INTEGER DEFAULT 5")

        # Survive Render's disk wipes: keep the clip video itself in Postgres.
        _ensure_column(conn, "clips", "video_b64", "TEXT")
        # Admin can block an account: login + existing tokens stop working.
        _ensure_column(conn, "users", "blocked", "INTEGER DEFAULT 0")
        _ensure_column(conn, "users", "blocked_at", "TEXT")
        # Gmail OTP verification: a new sign-up stays locked until the code is entered.
        added_verified = _ensure_column(conn, "users", "verified", "INTEGER")
        _ensure_column(conn, "users", "verify_code", "TEXT")
        _ensure_column(conn, "users", "verify_expires", "TEXT")
        _ensure_column(conn, "users", "verify_attempts", "INTEGER DEFAULT 0")
        _ensure_column(conn, "users", "verify_sent_at", "TEXT")
        if added_verified:
            # Everybody who registered before this feature keeps working.
            conn.execute("UPDATE users SET verified = 1")
            print("[DB] users.verified added - existing accounts grandfathered as verified")


init_db()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected_hex = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
        return hmac.compare_digest(dk.hex(), expected_hex)
    except Exception:
        return False


def create_token(user_id: int, email: str, name: str) -> str:
    payload = {
        "uid": user_id,
        "email": email,
        "name": name,
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    sig = hmac.new(SECRET_KEY, raw.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def decode_token(token: str) -> dict[str, Any]:
    try:
        raw, sig = token.split(".", 1)
        expected = hmac.new(SECRET_KEY, raw.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("Bad signature")
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        if payload.get("exp", 0) < int(time.time()):
            raise ValueError("Token expired")
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired session. Please log in again.")


def serialize_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    r = dict(row)
    is_adm = bool(r.get("is_admin")) or (str(r.get("email", "")).lower() in {e.lower() for e in ADMIN_EMAILS})
    return {
        "id": r["id"],
        "name": r["name"],
        "email": r["email"],
        "is_admin": is_adm,
        "created_at": r["created_at"],
    }


def get_current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Please log in to continue.")
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_token(token)
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, email, is_admin, created_at, blocked FROM users WHERE id = ?",
            (payload["uid"],),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="User account not found.")
    try:
        if int(row["blocked"] or 0) == 1:
            raise HTTPException(
                status_code=403,
                detail="This account has been blocked by the admin.",
            )
    except HTTPException:
        raise
    except Exception:
        pass
    return serialize_user(row)


def require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


# ============================================================
# YOUTUBE & TRANSCRIPT SCORING
# ============================================================
HOOK_WORDS = {
    "secret", "never", "always", "mistake", "truth", "actually", "crazy",
    "insane", "best", "worst", "stop", "wait", "listen", "imagine", "why",
    "how", "money", "million", "viral", "shocking", "finally", "wtf", "omg",
    "bro", "warning", "hack", "trick", "rule", "biggest", "nobody", "everyone",
}


def extract_video_id(url: str) -> str | None:
    url = url.strip()
    patterns = [
        r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def fetch_video_metadata(url: str, video_id: str) -> dict[str, Any]:
    """Fetch title, duration, and thumbnail WITHOUT downloading the video."""
    title = f"YouTube Video ({video_id})"
    duration = 0.0
    thumbnail = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

    # Fast oEmbed lookup first
    try:
        oembed_url = (
            "https://www.youtube.com/oembed?format=json&url="
            + urllib.parse.quote(f"https://www.youtube.com/watch?v={video_id}", safe="")
        )
        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            title = data.get("title") or title
            thumbnail = data.get("thumbnail_url") or thumbnail
    except Exception:
        pass

    # Try yt-dlp metadata only (download=False!) to get accurate duration
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 12,
            **get_ytdlp_js_opts(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                title = info.get("title") or title
                duration = float(info.get("duration") or 0)
                thumbnail = info.get("thumbnail") or thumbnail
    except Exception:
        pass

    return {"title": title, "duration": duration, "thumbnail": thumbnail}


def _parse_vtt_time(ts: str) -> float:
    parts = ts.strip().replace(",", ".").split(":")
    if len(parts) == 3:
        return _safe_float(parts[0]) * 3600 + _safe_float(parts[1]) * 60 + _safe_float(parts[2])
    if len(parts) == 2:
        return _safe_float(parts[0]) * 60 + _safe_float(parts[1])
    return 0.0


def _parse_vtt_to_segments(vtt_text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    blocks = re.split(r"\r?\n\r?\n", vtt_text)
    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        time_idx = next((i for i, l in enumerate(lines) if "-->" in l), -1)
        if time_idx < 0:
            continue
        m = re.search(r"([\d:.]+)\s*-->\s*([\d:.]+)", lines[time_idx])
        if not m:
            continue
        start = _parse_vtt_time(m.group(1))
        end = _parse_vtt_time(m.group(2))
        text = " ".join(lines[time_idx + 1 :])
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text and end > start:
            # Skip duplicate rolling caption lines
            if segments and segments[-1]["text"] == text:
                continue
            segments.append({"text": text, "start": round(start, 2), "duration": round(max(0.5, end - start), 2)})
    return segments


def _parse_json3_to_segments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    segs: list[dict[str, Any]] = []
    for ev in payload.get("events", []):
        t_ms = ev.get("tStartMs")
        d_ms = ev.get("dDurationMs", 2000)
        parts = ev.get("segs") or []
        txt = "".join(p.get("utf8", "") for p in parts)
        txt = re.sub(r"\s+", " ", txt).strip()
        if t_ms is not None and txt:
            segs.append({
                "text": txt,
                "start": round(t_ms / 1000.0, 2),
                "duration": round(max(0.5, d_ms / 1000.0), 2),
            })
    return segs


def _get_transcript_via_innertube(video_id: str) -> tuple[list[dict[str, Any]], str]:
    """Mobile InnerTube (ANDROID / IOS) transcript fallback — bypasses WEB HTML bot walls on some IPs."""
    clients = [
        (
            "ANDROID",
            {"clientName": "ANDROID", "clientVersion": "20.10.38", "androidSdkVersion": 30, "hl": "en", "gl": "US"},
            "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip",
        ),
        (
            "IOS",
            {"clientName": "IOS", "clientVersion": "20.10.4", "deviceMake": "Apple", "deviceModel": "iPhone16,2", "hl": "en", "gl": "US"},
            "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)",
        ),
    ]
    opener = urllib.request.build_opener()
    _active = get_active_proxy()
    if _active:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": _active, "https": _active})
        )

    last_err = "InnerTube returned no caption tracks."
    for name, ctx, ua in clients:
        try:
            body = json.dumps({"context": {"client": ctx}, "videoId": video_id}).encode("utf-8")
            req = urllib.request.Request(
                "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": ua},
            )
            with opener.open(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))

            tracks = (
                data.get("captions", {})
                .get("playerCaptionsTracklistRenderer", {})
                .get("captionTracks", [])
            )
            if not tracks:
                reason = data.get("playabilityStatus", {}).get("reason", "")
                last_err = f"{name}: {reason or 'no captions'}"
                continue

            # Prefer English, then Hindi, then first available
            chosen = None
            for pref in ("en", "en-US", "en-GB", "en-IN", "hi", "hi-IN"):
                chosen = next((t for t in tracks if t.get("languageCode") == pref), None)
                if chosen:
                    break
            if not chosen:
                chosen = tracks[0]

            lang = chosen.get("languageCode") or "auto"
            base_url = chosen.get("baseUrl", "")
            if not base_url:
                continue
            sep = "&" if "?" in base_url else "?"
            tr_req = urllib.request.Request(f"{base_url}{sep}fmt=json3", headers={"User-Agent": ua})
            with opener.open(tr_req, timeout=15) as tr_resp:
                payload = json.loads(tr_resp.read().decode("utf-8", errors="ignore"))
            segs = _parse_json3_to_segments(payload)
            if segs:
                return segs, lang
        except Exception as exc:
            last_err = f"{name}: {exc}"
    raise RuntimeError(last_err)


def _get_transcript_via_ytdlp(video_id: str, url: str) -> tuple[list[dict[str, Any]], str]:
    """Authenticated yt-dlp subtitle fetcher using yt-dlp's own cookie/proxy session (avoids 429 on tlang tracks)."""
    has_cookies = bool(YOUTUBE_COOKIE_FILE and Path(YOUTUBE_COOKIE_FILE).exists())
    ydl_opts = {
        "skip_download": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        **get_ytdlp_js_opts(),
    }
    if not has_cookies:
        ydl_opts["extractor_args"] = {"youtube": {"player_client": ["ios", "android", "tv", "web"]}}
    target_url = url or f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(target_url, download=False)
        subs = (info or {}).get("subtitles") or {}
        auto_subs = (info or {}).get("automatic_captions") or {}

        # Avoid YouTube's rate-limited auto-translated URLs (tlang=) by preferring manual subs, then original auto-subs!
        def is_original(formats: list[dict[str, Any]]) -> bool:
            return any(f.get("url") and "tlang=" not in f["url"] for f in formats)

        candidate_langs: list[tuple[str, list[dict[str, Any]]]] = []
        # 1. Manual subtitles (prefer en, hi, then any)
        for pref in ("en", "en-US", "en-GB", "en-IN", "hi", "hi-IN"):
            if pref in subs:
                candidate_langs.append((pref, subs[pref]))
        for k, v in subs.items():
            if not any(x[0] == k for x in candidate_langs):
                candidate_langs.append((k, v))

        # 2. Original (non-translated) automatic captions first
        for k, v in auto_subs.items():
            if is_original(v) and not any(x[0] == k for x in candidate_langs):
                candidate_langs.append((k, v))

        # 3. Translated automatic captions only as last resort
        for pref in ("en", "hi"):
            if pref in auto_subs and not any(x[0] == pref for x in candidate_langs):
                candidate_langs.append((pref, auto_subs[pref]))

        if not candidate_langs:
            raise RuntimeError("No subtitles or auto-captions found for this video.")

        last_err = "Subtitle stream could not be fetched."
        for chosen_lang, formats in candidate_langs[:3]:
            json3_entry = next((f for f in formats if f.get("ext") == "json3" and f.get("url")), None)
            vtt_entry = next((f for f in formats if f.get("ext") == "vtt" and f.get("url")), None)
            for kind, entry in (("json3", json3_entry), ("vtt", vtt_entry)):
                if not entry:
                    continue
                try:
                    # Use yt-dlp's own urlopen so cookies + proxy + headers are sent automatically!
                    raw_bytes = ydl.urlopen(entry["url"]).read()
                    text_body = raw_bytes.decode("utf-8", errors="ignore")
                    if kind == "json3":
                        segs = _parse_json3_to_segments(json.loads(text_body))
                    else:
                        segs = _parse_vtt_to_segments(text_body)
                    if segs:
                        return segs, chosen_lang
                except Exception as exc:
                    last_err = f"{chosen_lang}/{kind}: {exc}"
        raise RuntimeError(last_err)


def _fetch_transcript_primary_api(video_id: str) -> tuple[list[dict[str, Any]], str]:
    proxy_cfg = None
    if get_active_proxy():
        try:
            from youtube_transcript_api.proxies import GenericProxyConfig

            class RotatingProxyConfig(GenericProxyConfig):
                @property
                def prevent_keeping_connections_alive(self) -> bool:
                    return True

                @property
                def retries_when_blocked(self) -> int:
                    return 5

            _active = get_active_proxy()
            proxy_cfg = RotatingProxyConfig(http_url=_active, https_url=_active)
        except Exception:
            proxy_cfg = None

    api = YouTubeTranscriptApi(proxy_config=proxy_cfg) if proxy_cfg else YouTubeTranscriptApi()
    if hasattr(api, "list"):
        transcript_list = api.list(video_id)
    else:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

    try:
        transcript = transcript_list.find_transcript(["en", "en-US", "en-GB", "en-IN"])
    except Exception:
        transcript = next(iter(transcript_list))

    lang = getattr(transcript, "language_code", "auto")
    fetched = transcript.fetch()
    segments: list[dict[str, Any]] = []
    for s in fetched:
        if isinstance(s, dict):
            text = str(s.get("text", "")).strip()
            start = _safe_float(s.get("start", 0.0))
            dur = _safe_float(s.get("duration", 2.0), 2.0)
        else:
            text = str(getattr(s, "text", "")).strip()
            start = _safe_float(getattr(s, "start", 0.0))
            dur = _safe_float(getattr(s, "duration", 2.0), 2.0)
        if text:
            segments.append({"text": text, "start": round(start, 2), "duration": round(dur, 2)})
    if not segments:
        raise RuntimeError("Transcript was empty.")
    return segments, lang


def get_transcript(video_id: str, url: str = "") -> tuple[list[dict[str, Any]], str]:
    """Fetch transcript with multi-layer cloud fallback: cookies yt-dlp -> primary API -> InnerTube mobile -> yt-dlp."""
    has_cookies = bool(YOUTUBE_COOKIE_FILE and Path(YOUTUBE_COOKIE_FILE).exists())
    errors: list[str] = []

    # Build ordered strategies: if cookies are configured (without proxy), use authenticated yt-dlp FIRST!
    strategies: list[tuple[str, Any]] = []
    if has_cookies and not get_active_proxy():
        strategies.append(("yt-dlp (cookies)", lambda: _get_transcript_via_ytdlp(video_id, url)))
        strategies.append(("innertube-mobile", lambda: _get_transcript_via_innertube(video_id)))
        strategies.append(("youtube-transcript-api", lambda: _fetch_transcript_primary_api(video_id)))
    else:
        strategies.append(("youtube-transcript-api", lambda: _fetch_transcript_primary_api(video_id)))
        strategies.append(("innertube-mobile", lambda: _get_transcript_via_innertube(video_id)))
        strategies.append(("yt-dlp", lambda: _get_transcript_via_ytdlp(video_id, url)))

    for label, fn in strategies:
        try:
            segs, lang = fn()
            if segs:
                print(f"[TRANSCRIPT] OK via {label} ({lang}, {len(segs)} segments)")
                return segs, lang
        except Exception as exc:
            msg = str(exc).strip()
            errors.append(f"{label}: {msg[:140]}")
            print(f"[TRANSCRIPT] {label} failed -> {msg[:140]}")

    combined = " | ".join(errors)
    low = combined.lower()
    if any(k in low for k in ("blocking", "bot", "sign in", "ip", "429", "forbidden", "login_required")):
        detail = f" [Details: {errors[0]}]" if has_cookies and errors else ""
        raise RuntimeError(
            "YouTube blocked this cloud server's IP. Easy fix: click 'Upload Video (MP4)' right above (100% free & never blocked), or refresh cookies.txt in Render Secret Files!" + detail
        )
    raise RuntimeError(errors[0] if errors else "Could not fetch YouTube transcript.")


STOP_WORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "your", "you",
    "are", "was", "were", "have", "has", "had", "not", "but", "what", "when",
    "where", "who", "why", "how", "all", "can", "will", "just", "first", "part",
    "video", "full", "episode", "official", "new", "2025", "2026", "my", "our",
}

VIRAL_HOOK_PREFIXES = [
    "Wait for this moment:",
    "Unbelievable moment —",
    "You won't believe this:",
    "Peak moment:",
    "Watch till the end:",
    "Wildest part:",
    "Best highlight:",
    "Crazy twist:",
    "Don't miss this:",
    "Pure chaos:",
]


def extract_topic_hashtags(video_title: str, lang: str = "en") -> list[str]:
    """Generate clean, relevant hashtags from the video title and language."""
    tags: list[str] = []
    seen: set[str] = set()

    # Keep alphanumeric phrases like GTA6, iPhone16, etc.
    title_clean = re.sub(r"[^\w\s]", " ", video_title)
    # Detect common multi-word combos like "GTA 6" -> #GTA6
    m_gta = re.search(r"\b(gta)\s*(\d+)\b", title_clean, re.IGNORECASE)
    if m_gta:
        tag = f"#{m_gta.group(1).upper()}{m_gta.group(2)}"
        seen.add(tag.lower())
        tags.append(tag)

    for word in title_clean.split():
        w = re.sub(r"[^A-Za-z0-9]", "", word)
        if len(w) < 3 or w.lower() in STOP_WORDS or w.isdigit():
            continue
        tag = f"#{w.capitalize()}"
        if tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
        if len(tags) >= 4:
            break

    defaults = ["#Shorts", "#ViralClips", "#Reels", "#Trending", "#YouTubeShorts"]
    if str(lang).lower().startswith("hi"):
        defaults.insert(1, "#HindiShorts")
    for d in defaults:
        if d.lower() not in seen:
            seen.add(d.lower())
            tags.append(d)
    return tags[:8]


# ---------------- VIRAL TITLE / DESCRIPTION / HASHTAGS ENGINE ----------------
DOWNLOAD_JUNK = re.compile(
    r"(?i)\b(?:www\.)?[a-z0-9][a-z0-9-]{1,30}\.(?:com|in|net|org|co|io|me|link|site|xyz|top|club|vip|"
    r"app|tv|cc|info|biz|ru|to|ws|fm|ly|gg|sh|online|store|pro|live)\b"
)
SITE_MARKERS = re.compile(
    r"(?i)\b(?:vidssave|savefrom|ssyoutube|y2mate|yt5s|snaptik|ssstik|9xbuddy|"
    r"ytmp3|y2down|download|downloaded|mp3converter|converter|hdhub|9xmovies|filmyzilla)\b"
)
RESOLUTION_TAG = re.compile(r"(?i)\b(?:144|240|360|480|540|720|1080|1440|2160|4k|8k)\s*p?\b")
BRACKET_TAG = re.compile(r"[\[\(\{][^\]\)\}]{0,24}[\]\)\}]")
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U00002B00-\U00002BFF]+",
    flags=re.UNICODE,
)
TITLE_NOISE = {
    "video", "videos", "full", "hd", "hq", "mp4", "mkv", "new", "latest", "official",
    "watch", "download", "free", "song", "movie", "whatsapp", "status", "clip", "clips",
}
TAG_NOISE = TITLE_NOISE | {
    "tested", "moment", "viral", "highlight", "going", "part", "reality", "upcoming",
    "this", "that", "they", "them", "then", "with", "from", "have", "will", "just",
    "like", "what", "when", "your", "about", "into", "very", "much", "even", "only",
}

# Optional free AI writer (Groq / Gemini). Without a key the template engine is used.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL_NAME = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip()
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT_SEC", "25"))

TDH_SYSTEM_PROMPT = (
    "You are a YouTube Shorts and Instagram Reels growth strategist who writes "
    "metadata that gets real views. You always answer with a single JSON object."
)


def llm_tdh_enabled() -> bool:
    return bool(GROQ_API_KEY or GEMINI_API_KEY)


def llm_backend() -> str:
    if GROQ_API_KEY:
        return "groq"
    if GEMINI_API_KEY:
        return "gemini"
    return "none"


def clean_source_topic(raw: str) -> str:
    """Turn a messy filename or video title into a clean topic.

    'vidssave.com I Tested 25 Lakh Defender Reality 480P' -> 'I Tested 25 Lakh Defender Reality'
    """
    if not raw:
        return "This clip"
    text = str(raw)
    text = re.sub(r"\.(?:mp4|mov|mkv|webm|m4v|avi|flv|ts)$", " ", text, flags=re.I)
    text = DOWNLOAD_JUNK.sub(" ", text)
    text = SITE_MARKERS.sub(" ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = BRACKET_TAG.sub(" ", text)
    text = RESOLUTION_TAG.sub(" ", text)
    text = EMOJI_RE.sub(" ", text)
    text = text.replace("_", " ").replace("|", " ").replace("\u2022", " ")
    text = re.sub(r"\s{2,}", " ", text).strip(" -\u2013\u2014:.|,")
    words = [w for w in text.split() if w.lower().strip(".,!?") not in TITLE_NOISE]
    text = " ".join(words).strip()
    text = re.sub(r"\bi\b", "I", text)
    if not text or re.fullmatch(r"[\d\s.\-_]+", text):
        return "This clip"
    return text[:90].strip() or "This clip"


def _topic_keywords(text: str, limit: int = 4) -> list[str]:
    """Pull the most meaningful words out of a topic or spoken line."""
    if not text:
        return []
    words = re.findall(r"[A-Za-z\u0900-\u097F][\w'\u0900-\u097F]{2,}", str(text))
    counts: dict[str, int] = {}
    order: list[str] = []
    for w in words:
        low = w.lower()
        if low in STOP_WORDS or low in TAG_NOISE or low.isdigit():
            continue
        if low not in counts:
            order.append(w)
        counts[low] = counts.get(low, 0) + 1
    ranked = sorted(order, key=lambda w: (-counts[w.lower()], -len(w), order.index(w)))
    return ranked[:limit]


def _clean_speech(text: str, max_words: int = 12) -> str:
    txt = re.sub(r"\s+", " ", EMOJI_RE.sub(" ", str(text or ""))).strip()
    txt = re.sub(r"\b(?:um+|uh+|like|you know|so yeah)\b[, ]*", " ", txt, flags=re.I)
    words = txt.split()
    if not words:
        return ""
    return " ".join(words[:max_words]).strip(" ,;:-")


def _speech_sentences(spoken: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", str(spoken or "").strip())
    return [p.strip(" ,;:-") for p in parts if len(p.split()) >= 4]


def _build_hashtags(topic: str, spoken: str, lang: str, limit: int = 12) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()

    def _push(word: str) -> None:
        clean = re.sub(r"[^\w\u0900-\u097F]", "", word)
        if len(clean) < 3 or clean.isdigit():
            return
        tag = "#" + clean.title()
        if tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)

    for w in _topic_keywords(topic, 4):
        _push(w)
    for w in _topic_keywords(spoken, 4):
        _push(w)

    platform = ["#Shorts", "#Reels", "#Viral", "#Trending", "#Explore", "#FYP",
                "#ViralClips", "#ShortsFeed", "#InstaReels", "#DailyShorts"]
    if str(lang).lower().startswith("hi"):
        platform.insert(1, "#HindiShorts")
    for p in platform:
        if p.lower() not in seen:
            seen.add(p.lower())
            tags.append(p)
    return tags[:limit]


def _llm_json_call(user_prompt: str, description: bool = True) -> dict[str, Any] | None:
    """One JSON-mode LLM call (Groq or Gemini). Returns the parsed dict or None."""
    if not llm_tdh_enabled():
        return None
    system = TDH_SYSTEM_PROMPT if description else (
        "You name videos for a shorts app. Answer with a single JSON object."
    )
    try:
        if GROQ_API_KEY:
            r = httpx.post(
                f"{GROQ_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": GROQ_MODEL_NAME,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user_prompt}],
                    "temperature": 0.8, "max_tokens": 700,
                    "response_format": {"type": "json_object"},
                },
                timeout=LLM_TIMEOUT,
            )
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"]
        else:
            r = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL_NAME}:generateContent",
                params={"key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": system + "\n\n" + user_prompt}]}],
                      "generationConfig": {"temperature": 0.85, "maxOutputTokens": 900,
                                           "responseMimeType": "application/json"}},
                timeout=LLM_TIMEOUT,
            )
            r.raise_for_status()
            raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        raw = (raw or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
        i, j = raw.find("{"), raw.rfind("}")
        if i >= 0 and j > i:
            raw = raw[i:j + 1]
        return json.loads(raw)
    except Exception as exc:
        print(f"[TDH] LLM call failed ({type(exc).__name__}: {str(exc)[:120]})")
        return None


def _is_generic_title(title: str) -> bool:
    t = (title or "").strip()
    return (
        not t
        or t.lower() in ("this clip", "uploaded video", "video", "untitled")
        or bool(re.fullmatch(r"[\d\s._-]+", t))
    )


def _llm_video_title(spoken: str) -> str | None:
    """Name a whole video from what is actually said in it."""
    if not spoken:
        return None
    data = _llm_json_call(
        "Give this video one catchy title for a shorts app.\n"
        f'Transcript: """{spoken[:900]}"""\n'
        "Rules: max 60 characters, no quotes, no hashtags, describe the actual subject.\n"
        'Return ONLY JSON: {"title": "..."}',
        description=False,
    )
    if not data:
        return None
    title = str(data.get("title", "")).strip().strip('"').replace("\n", " ")
    if len(title) < 4:
        return None
    return title[:80]


def _llm_tdh(topic: str, spoken: str, duration_sec: float, lang: str) -> dict[str, Any] | None:
    """Ask a free LLM (Groq/Gemini) for real, human-quality metadata."""
    if not llm_tdh_enabled():
        return None
    lang_name = "Hindi (write hashtags/keywords in English roman script, title may mix Hindi+English)" \
        if str(lang).lower().startswith("hi") else "English"
    user_prompt = (
        "Write metadata for ONE vertical short clip.\n"
        f"Clip length: {int(round(duration_sec))} seconds.\n"
        f"Original video context: {topic}\n"
        f"Spoken content of THIS clip (automatic transcript, may contain small errors):\n"
        f"\"\"\"{spoken[:1200] or '(no speech - silent or music-only clip)'}\"\"\"\n"
        f"Language of the speech: {lang_name}\n\n"
        "Rules:\n"
        "- title: max 80 characters, a real hook that matches what is actually said. No lies, no ALL CAPS spam, 0-2 emojis max.\n"
        "- description: 3-5 short lines. Line 1 = hook. Then 1-2 lines explaining what happens in the clip "
        "(specific, mention the actual subject). Then a call to action (comment/share/follow). Then a blank line. "
        "Keep it under 600 characters.\n"
        "- hashtags: 10-12 relevant tags, each starting with #, mixing niche topic tags with "
        "#Shorts #Reels #Viral #Trending. camelCase words, no spaces.\n"
        'Return ONLY JSON: {"title": "...", "description": "...", "hashtags": ["#a", "#b"]}'
    )
    data = _llm_json_call(user_prompt)
    if not data:
        return None

    title = str(data.get("title", "")).strip()
    desc = str(data.get("description", "")).strip()
    tags_in = data.get("hashtags") or []
    if isinstance(tags_in, str):
        tags_in = re.split(r"[,\s]+", tags_in)
    tags: list[str] = []
    seen: set[str] = set()
    for t in tags_in:
        t = re.sub(r"[^\w\u0900-\u097F#]", "", str(t)).lstrip("#")
        if len(t) < 3:
            continue
        tag = "#" + t[0].upper() + t[1:]
        if tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    if not title or not desc or len(tags) < 6:
        return None
    if len(title) > 100:
        title = title[:97].rstrip(" ,;:-") + "\u2026"
    print(f"[TDH] {llm_backend()} wrote: {title[:60]}")
    return {"viral_title": title, "description": desc, "hashtags": tags[:12]}

def build_viral_tdh(
    topic: str,
    spoken_text: str = "",
    rank: int = 1,
    duration_sec: float = 30.0,
    lang: str = "en",
    use_llm: bool = True,
) -> dict[str, Any]:
    """Human-quality Title / Description / Hashtags for a Short or Reel."""
    clean_topic = clean_source_topic(topic)
    spoken = re.sub(r"\s+", " ", str(spoken_text or "")).strip()

    if use_llm and spoken and llm_tdh_enabled():
        ai = _llm_tdh(clean_topic, spoken, duration_sec, lang)
        if ai:
            return ai

    short_topic = clean_topic
    if len(short_topic) > 52:
        short_topic = short_topic[:52].rsplit(" ", 1)[0].rstrip(" ,;:-")
    short_topic = (short_topic[:1].upper() + short_topic[1:]) if short_topic else "This clip"

    kw = _topic_keywords(clean_topic, 4) or _topic_keywords(spoken, 4)
    specific = [w for w in kw if len(w) >= 4]
    focus = max(specific, key=len).title() if specific else (kw[0].title() if kw else "Viral")
    quote = _clean_speech(spoken, 9)

    quote_templates = [
        '"{q}" \U0001F92F',
        "Wait for it\u2026 \u201c{q}\u201d \U0001F631",
        'He actually said: "{q}" \U0001F525',
        '"{q}" \u2014 this is going viral',
        'Nobody expected this: "{q}" \U0001F62E',
    ]
    topic_templates = [
        "Wait for it\u2026 {t} \U0001F631",
        "{t} \u2014 you won't believe this \U0001F525",
        "Nobody expected this: {t} \U0001F62E",
        "{t} \U0001F92F (watch till the end)",
        "You have to see this: {t} \U0001F440",
    ]
    if quote and len(quote.split()) >= 3:
        title = quote_templates[(rank - 1) % len(quote_templates)].format(q=quote)
    else:
        title = topic_templates[(rank - 1) % len(topic_templates)].format(t=short_topic)
    title = re.sub(r"\s{2,}", " ", title).strip()
    if len(title) > 95:
        title = title[:92].rstrip(" ,;:-") + "\u2026"

    hashtags = _build_hashtags(clean_topic, spoken, lang, limit=12)

    # Description: hook, what happens, why it matters, CTA, tags.
    lines: list[str] = [title]
    sentences = _speech_sentences(spoken)
    if sentences:
        recap = sentences[0]
        for s in sentences[1:]:
            if len(recap) + len(s) < 220:
                recap += " " + s
            else:
                break
        lines.append("")
        lines.append("\U0001F3AC " + recap[:230].strip())
        if len(sentences) > 1 and len(lines[-1]) < 120:
            extra = sentences[-1]
            if extra not in recap:
                lines.append("\U0001F4A5 " + extra[:180].strip())
        lines.append("")
        lines.append("Why watch: this is the " + str(int(round(duration_sec))) + "s version you can actually finish \u2014 save it before it's gone.")
    else:
        lines.append("")
        lines.append("\U0001F3AC " + short_topic + " \u2014 the " + str(int(round(duration_sec))) + "s cut everyone is sharing.")
        lines.append("No filler, straight to the best part.")
        lines.append("")
        lines.append("Why watch: short, sharp and impossible to scroll past.")
    lines.append("")
    lines.append("\u27A1\uFE0F Follow for more " + focus + " content")
    lines.append("\U0001F4CC Save this & comment your take below")
    lines.append("")
    lines.append(" ".join(hashtags[:10]))
    description = "\n".join(lines)[:900]

    return {"viral_title": title, "description": description, "hashtags": hashtags}

def generate_clip_metadata(
    video_title: str,
    snippet_text: str,
    rank: int,
    duration_sec: float,
    lang: str = "en",
) -> dict[str, Any]:
    """Generate a ready-to-post viral Title, Description, and Hashtags for every clip."""
    return build_viral_tdh(
        topic=video_title,
        spoken_text="" if snippet_text.lower().startswith("high-energy highlight") else snippet_text,
        rank=rank,
        duration_sec=duration_sec,
        lang=lang,
    )


def _legacy_generate_clip_metadata(
    video_title: str,
    snippet_text: str,
    rank: int,
    duration_sec: float,
    lang: str = "en",
) -> dict[str, Any]:
    clean_snip = re.sub(r"\s+", " ", snippet_text).strip()
    clean_vtitle = re.sub(r"\s+", " ", video_title).strip()
    short_vtitle = clean_vtitle[:54].rstrip(" |-:")

    # Pull first ~9 words of the clip speech for authentic context
    snip_words = clean_snip.split()
    quote = " ".join(snip_words[:9]) + ("..." if len(snip_words) > 9 else "")
    prefix = VIRAL_HOOK_PREFIXES[(rank - 1) % len(VIRAL_HOOK_PREFIXES)]

    if quote and len(quote) >= 12:
        viral_title = f"{prefix} {quote}"
    else:
        viral_title = f"{prefix} {short_vtitle} (Clip #{rank})"
    if len(viral_title) > 92:
        viral_title = viral_title[:89].rstrip() + "..."

    hashtags = extract_topic_hashtags(clean_vtitle, lang=lang)
    tag_line = " ".join(hashtags[:5])

    excerpt = " ".join(snip_words[:22]) + ("..." if len(snip_words) > 22 else "")
    description = (
        f"🔥 {prefix} Watch this {int(round(duration_sec))}s highlight from \"{clean_vtitle}\".\n"
        f"💬 Moment: \"{excerpt}\"\n"
        f"👇 Comment what you think & subscribe for more daily viral clips!\n\n"
        f"{tag_line}"
    )

    return {
        "viral_title": viral_title,
        "description": description,
        "hashtags": hashtags,
    }


def score_transcript_moments(
    segments: list[dict[str, Any]],
    clip_len: int = CLIP_DURATION,
    max_clips: int = 5,
    video_title: str = "Video",
    lang: str = "en",
) -> list[dict[str, Any]]:
    """Find top non-overlapping high-energy viral moments with Title, Description & Hashtags."""
    if not segments:
        return []

    total_end = max(s["start"] + s["duration"] for s in segments)
    window_len = min(float(clip_len), max(10.0, total_end))
    candidates: list[dict[str, Any]] = []

    step = max(4.0, window_len / 4.0)
    t = 0.0
    while t < total_end:
        start = round(t, 2)
        end = round(min(total_end, start + window_len), 2)
        if end - start < min(8.0, total_end):
            break

        win_segs = [
            s for s in segments
            if (s["start"] + s["duration"]) > start and s["start"] < end
        ]
        if not win_segs:
            t += step
            continue

        joined = " ".join(s["text"] for s in win_segs)
        words = re.findall(r"[\w']+", joined.lower())
        word_count = len(words)
        pace = word_count / max(1.0, end - start)  # words/sec

        hook_hits = sum(1 for w in words if w in HOOK_WORDS)
        exclamations = joined.count("!") + joined.count("?")

        pace_score = min(40, int(pace * 14))
        hook_score = min(35, hook_hits * 7 + exclamations * 5)
        density_score = min(24, int((len(win_segs) / max(1.0, (end - start) / 4.0)) * 12))
        total_score = max(62, min(99, 45 + int((pace_score + hook_score + density_score) * 0.55)))

        clean_preview = re.sub(r"\s+", " ", joined).strip()

        candidates.append(
            {
                "start": start,
                "end": end,
                "duration": round(end - start, 1),
                "score": total_score,
                "preview": clean_preview[:220],
                "reason": (
                    f"{word_count} words · {pace:.1f} w/s pace"
                    + (f" · {hook_hits} hook triggers" if hook_hits else "")
                ),
            }
        )
        if end >= total_end:
            break
        t += step

    # Greedy non-overlapping top-K selection
    candidates.sort(key=lambda c: c["score"], reverse=True)
    selected: list[dict[str, Any]] = []
    for cand in candidates:
        overlaps = any(
            not (cand["end"] <= s["start"] + 2.0 or cand["start"] >= s["end"] - 2.0)
            for s in selected
        )
        if not overlaps:
            selected.append(cand)
        if len(selected) >= max_clips:
            break

    selected.sort(key=lambda c: c["score"], reverse=True)
    for idx, item in enumerate(selected, start=1):
        meta = generate_clip_metadata(
            video_title=video_title,
            snippet_text=item["preview"],
            rank=idx,
            duration_sec=item["duration"],
            lang=lang,
        )
        item["rank"] = idx
        item["title"] = meta["viral_title"]
        item["description"] = meta["description"]
        item["hashtags"] = meta["hashtags"]
    return selected


# ============================================================
# ACCURATE CLIP CUTTING & PREMIUM STYLED ASS CAPTIONS
# ============================================================
# ASS colors are &HAABBGGRR (Blue-Green-Red!)
CAPTION_THEMES = {
    "gold": {
        "label": "Premium Gold",
        "primary": "&H00F8FAFC",      # Crisp pearl white
        "accent": "&H0026D7FF",       # Warm luxe gold (#FFD726)
        "outline": "&H00120E1A",      # Deep obsidian outline
        "shadow": "&H88000000",       # Soft drop shadow
    },
    "violet": {
        "label": "Neon Violet",
        "primary": "&H00F8FAFC",
        "accent": "&H00FF85B8",       # Electric violet (#B885FF)
        "outline": "&H00180C26",
        "shadow": "&H88000000",
    },
    "mint": {
        "label": "Mint Glow",
        "primary": "&H00F8FAFC",
        "accent": "&H0086EF4A",       # Emerald mint (#4AEF86)
        "outline": "&H000D1A14",
        "shadow": "&H88000000",
    },
}


def fmt_ass_time(seconds: float) -> str:
    cs = int(round(max(0.0, seconds) * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def has_indic_chars(text: str) -> bool:
    return bool(re.search(r"[\u0900-\u0D7F]", text))


def escape_ass_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def style_premium_line(words: list[str], accent_bgr: str) -> str:
    """Highlight the strongest/longest keyword in the phrase with premium accent color."""
    if not words:
        return ""
    if len(words) == 1:
        return f"{{\\c{accent_bgr}}}{escape_ass_text(words[0])}{{\\r}}"
    # Pick the most impactful word (longest non-trivial word) to highlight in premium accent color
    best_idx = max(range(len(words)), key=lambda i: (len(words[i]), i))
    styled = []
    for i, w in enumerate(words):
        esc = escape_ass_text(w)
        if i == best_idx:
            styled.append(f"{{\\c{accent_bgr}}}{esc}{{\\r}}")
        else:
            styled.append(esc)
    return " ".join(styled)


def build_synced_ass(
    transcript: list[dict[str, Any]],
    clip_start: float,
    clip_end: float,
    ass_path: Path,
    theme_key: str = "gold",
) -> tuple[bool, str | None]:
    """
    Build a sleek, compact, creator-grade ASS subtitle file:
    - Shifts every timestamp to start at 0:00:00.00 relative to clip_start
    - Splits long transcript lines into neat 4-5 word bite-sized lines
    - Uses compact premium font sizing (40px on 1920x1080 canvas) + gold/violet/mint keyword color
    - Auto-selects Nirmala UI for Hindi/Indic text and Segoe UI / Bahnschrift for English
    """
    clip_dur = max(1.0, clip_end - clip_start)
    theme = CAPTION_THEMES.get(theme_key, CAPTION_THEMES["gold"])

    raw_items: list[tuple[float, float, str, list]] = []
    full_sample = []
    for seg in transcript:
        seg_start = _safe_float(seg["start"])
        seg_end = seg_start + max(0.5, _safe_float(seg.get("duration", 2.0), 2.0))
        if seg_end <= clip_start or seg_start >= clip_end:
            continue
        rel_start = max(0.0, seg_start - clip_start)
        rel_end = min(clip_dur, seg_end - clip_start)
        if rel_end - rel_start < 0.25:
            continue
        text = re.sub(r"\s+", " ", str(seg["text"])).strip()
        if not text:
            continue
        word_times: list[tuple[str, float, float]] = []
        for w in seg.get("words") or []:
            try:
                w_text = str(w.get("w", "")).strip()
                w_start = _safe_float(w.get("s"), seg_start)
                w_end = _safe_float(w.get("e"), w_start + 0.25)
            except Exception:
                continue
            if w_text and w_end > clip_start and w_start < clip_end:
                word_times.append((w_text, w_start, w_end))
        raw_items.append((rel_start, rel_end, text, word_times))
        full_sample.append(text)

    if not raw_items:
        return False, None

    use_indic = has_indic_chars(" ".join(full_sample))
    font_file, font_name = FONTS["indic"] if use_indic else FONTS["latin"]
    # Bigger, clearly readable captions (env-tunable without code changes).
    default_size = int(os.getenv("CAPTION_FONT_SIZE", "52"))
    font_size = default_size if not use_indic else default_size + 2

    # IMPORTANT: Spacing MUST be 0! Non-zero letter spacing breaks Hindi/Devanagari
    # matra shaping in libass and causes dotted circles (◌) between letters.
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Premium,{font_name},{font_size},{theme['primary']},{theme['accent']},{theme['outline']},{theme['shadow']},-1,0,0,0,100,100,0,0,1,{os.getenv("CAPTION_OUTLINE", "3.4")},{os.getenv("CAPTION_SHADOW", "2.0")},2,70,70,70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    cues: list[list[Any]] = []
    max_words_per_chunk = 5

    for rel_start, rel_end, text, word_times in raw_items:
        # Preferred: exact per-word timings from the ASR (captions land on the beat).
        word_times = [
            w for w in word_times
            if (seg_start - 1.0) <= w[1] <= (seg_end + 1.0)
        ]
        if len(word_times) >= 2:
            for i in range(0, len(word_times), max_words_per_chunk):
                group = word_times[i : i + max_words_per_chunk]
                if not group:
                    continue
                ch_start = max(0.0, group[0][1] - clip_start)
                ch_end = min(clip_dur, group[-1][2] - clip_start)
                if ch_end - ch_start < 0.2:
                    continue
                styled = style_premium_line([g[0] for g in group], theme["accent"])
                cues.append([ch_start, ch_end, styled])
            continue

        words = text.split()
        if len(words) <= max_words_per_chunk:
            chunks = [words]
        else:
            chunks = [
                words[i : i + max_words_per_chunk]
                for i in range(0, len(words), max_words_per_chunk)
            ]

        total_dur = max(0.3, rel_end - rel_start)
        total_chars = max(1, sum(sum(len(w) for w in ch) for ch in chunks))
        cur_t = rel_start
        for ch in chunks:
            ch_chars = max(1, sum(len(w) for w in ch))
            ch_dur = total_dur * (ch_chars / total_chars)
            ch_end = min(rel_end, cur_t + max(0.25, ch_dur))
            styled_text = style_premium_line(ch, theme["accent"])
            cues.append([cur_t, ch_end, styled_text])
            cur_t = ch_end

    # Clamp overlapping YouTube auto-caption timestamps so ONLY ONE crisp line shows at a time
    cues.sort(key=lambda x: x[0])
    dialogues: list[str] = []
    for i, (st, en, txt) in enumerate(cues):
        if i + 1 < len(cues) and en > cues[i + 1][0] - 0.02:
            en = max(st + 0.2, cues[i + 1][0] - 0.02)
        if en > st:
            dialogues.append(
                f"Dialogue: 0,{fmt_ass_time(st)},{fmt_ass_time(en)},Premium,,0,0,0,,{txt}"
            )

    ass_path.write_text(header + "\n".join(dialogues) + "\n", encoding="utf-8")
    return True, font_file


def escape_ffmpeg_subtitle_path(path: Path) -> str:
    """Escape path safely for FFmpeg subtitles/ass filter on both Windows (C\\:/...) and Linux."""
    s = path.resolve().as_posix()
    s = s.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return s


def build_ass_vf(ass_path: Path, font_file: str | None) -> str:
    escaped = escape_ffmpeg_subtitle_path(ass_path)
    # shaping=complex enables HarfBuzz Indic/Devanagari conjunct & matra shaping!
    if font_file:
        fonts_dir = escape_ffmpeg_subtitle_path(Path(font_file).parent)
        return f"ass='{escaped}':fontsdir='{fonts_dir}':shaping=complex"
    return f"ass='{escaped}':shaping=complex"


def encode_smooth_clip(
    raw_video: Path,
    out_video: Path,
    ass_path: Path | None = None,
    font_file: str | None = None,
) -> tuple[bool, str]:
    """
    Local smooth-playback encoder:
    Fixes 15s-30s lag/freeze completely by normalizing CFR 30fps, yuv420p,
    H.264 High@L4.1, and async audio resampling from a local downloaded file.
    """
    if not FFMPEG_BIN:
        return False, "FFmpeg is not available"

    cmd = [
        FFMPEG_BIN,
        "-y",
        "-fflags", "+genpts",
        "-i", str(raw_video),
    ]
    if ass_path and ass_path.exists():
        cmd += ["-vf", build_ass_vf(ass_path, font_file)]

    cmd += [
        "-fps_mode", "cfr",
        "-r", "30",
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level", "4.1",
        "-pix_fmt", "yuv420p",
        "-preset", ENCODE_PRESET,
        "-crf", str(ENCODE_CRF),
        # On tiny cloud CPUs, spawning a thread per host core causes thrashing.
        "-threads", str(2 if LOW_CPU else max(1, (os.cpu_count() or 1))),
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-af", "aresample=async=1:first_pts=0",
        "-avoid_negative_ts", "make_zero",
        "-shortest",
        "-movflags", "+faststart",
        str(out_video),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=ENCODE_TIMEOUT)
    if res.returncode == 0 and out_video.exists() and out_video.stat().st_size > 10_000:
        return True, ""
    err = (res.stderr or "")[-500:]
    if ass_path:
        print(f"CAPTION BURN FAILED: {err}")
    return False, err


def get_ytdlp_js_opts() -> dict[str, Any]:
    """Common yt-dlp options: IPv4 + Node.js runtime + optional YouTube cookies + optional proxy."""
    opts: dict[str, Any] = {
        "source_address": "0.0.0.0",  # Force IPv4 (YouTube flags cloud IPv6 ranges much harder)
    }
    if shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}
    if YOUTUBE_COOKIE_FILE and Path(YOUTUBE_COOKIE_FILE).exists():
        opts["cookiefile"] = YOUTUBE_COOKIE_FILE
    _active = get_active_proxy()
    if _active:
        opts["proxy"] = _active
        if not PROXY_URL:
            # Free public proxies are slow: allow much longer socket timeouts.
            opts["socket_timeout"] = 45
    return opts


def cut_youtube_section(
    url: str,
    video_id: str,
    start: float,
    end: float,
    transcript: list[dict[str, Any]],
    with_captions: bool = True,
    caption_theme: str = "gold",
    progress: Any = None,
) -> tuple[Path, bool, str | None]:
    """
    Zero-lag 2-stage 1080p clip pipeline:
    Stage 1: Fast stream-copy (-c copy) downloads ONLY the 30s section to local disk (~4-8s, no network stall!).
    Stage 2: Re-encode locally at smooth CFR 30fps + yuv420p + Premium styled ASS captions (~6-12s).
    """
    if not FFMPEG_BIN:
        raise RuntimeError("FFmpeg is not ready on this machine. Check FFMPEG CHECK in backend terminal.")

    def _p(msg: str) -> None:
        if callable(progress):
            try:
                progress(msg)
            except Exception:
                pass

    # Adaptive profile: free public proxies are slow & flaky, so pull less data,
    # wait longer, and try more of them before giving up.
    free_mode = _using_free_pool()
    max_h = 720 if free_mode else MAX_HEIGHT
    copy_timeout = 330 if free_mode else 150
    attempt_heights = [max_h, max_h, 480, 480] if free_mode else [max_h, max_h, max_h]
    if free_mode:
        print(f"[CLIP] Free public proxy mode: {max_h}p cap, {copy_timeout}s per attempt")

    start_i = max(0, int(math.floor(start)))
    end_i = max(start_i + 5, int(math.ceil(end)))
    clip_dur = max(3.0, end - start)
    theme_slug = caption_theme if caption_theme in CAPTION_THEMES else "gold"
    want_cc = bool(with_captions and CAPTIONS and transcript)
    tag = f"{video_id}_{start_i}_{end_i}_{max_h}p_{theme_slug}_v3"
    raw_path = CLIPS_DIR / f"{tag}_raw.mp4"
    final_name = f"{tag}_cc.mp4" if want_cc else f"{tag}.mp4"
    final_path = CLIPS_DIR / final_name

    if final_path.exists() and final_path.stat().st_size > 10_000:
        print(f"[CLIP] Serving cached smooth clip: {final_path.name}")
        return final_path, want_cc, None

    ass_path = CLIPS_DIR / f"{tag}.ass"
    has_ass, used_font_file = (
        build_synced_ass(transcript, start, end, ass_path, theme_key=theme_slug)
        if want_cc
        else (False, None)
    )

    def _fmt_for(h: int) -> str:
        return (
            f"bestvideo[height<={h}][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={h}][ext=mp4]/best[height<={h}]/best"
        )

    # ---- STAGE 1: Fast raw download to local disk (NO encoding while reading HTTP!) ----
    print(f"[CLIP] Stage 1/2: Fast-fetching {clip_dur:.0f}s section ({start_i}s-{end_i}s, {max_h}p)...")
    _p(f"Downloading {clip_dur:.0f}s section from YouTube...")
    if raw_path.exists():
        raw_path.unlink(missing_ok=True)

    downloaded_raw = False

    def _fast_copy_once(height: int | None = None) -> bool:
        """Extract fresh stream URLs and range-copy ONLY this clip section."""
        info_opts = {
            "format": _fmt_for(height or max_h),
            "skip_download": True,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 20,
            **get_ytdlp_js_opts(),
        }
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        req_formats = info.get("requested_formats") or []
        if len(req_formats) >= 2:
            v_url = req_formats[0].get("url")
            a_url = req_formats[1].get("url")
            hdrs = req_formats[0].get("http_headers") or info.get("http_headers") or {}
        else:
            v_url = info.get("url")
            a_url = None
            hdrs = info.get("http_headers") or {}

        if not v_url:
            return False

        ua = hdrs.get("User-Agent", "Mozilla/5.0")
        # Grab 1s extra padding on the copy so the final trim is frame-clean
        pad_start = max(0.0, start - 0.5)
        pad_dur = clip_dur + 1.0
        ff_copy = [
            FFMPEG_BIN, "-y",
            "-user_agent", ua,
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-ss", f"{pad_start:.2f}",
            "-i", v_url,
        ]
        if a_url:
            ff_copy += [
                "-user_agent", ua,
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-ss", f"{pad_start:.2f}",
                "-i", a_url,
                "-map", "0:v:0", "-map", "1:a:0",
            ]
        ff_copy += [
            "-t", f"{pad_dur:.2f}",
            "-rw_timeout", "45000000",   # bail out of a stalled socket instead of hanging
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(raw_path),
        ]
        # FFmpeg must ALSO exit through the residential proxy, otherwise
        # googlevideo.com rejects the datacenter IP (403) and the copy stalls.
        ff_env = None
        _active = get_active_proxy()
        if _active:
            ff_env = {
                **os.environ,
                "http_proxy": _active,
                "https_proxy": _active,
                "HTTP_PROXY": _active,
                "HTTPS_PROXY": _active,
                "ALL_PROXY": _active,
            }
        try:
            r_copy = subprocess.run(
                ff_copy, capture_output=True, text=True, timeout=copy_timeout, env=ff_env
            )
        except subprocess.TimeoutExpired:
            print(f"[CLIP] Copy timed out after {copy_timeout}s (proxy too slow) — rotating proxy")
            return False
        ok = r_copy.returncode == 0 and raw_path.exists() and raw_path.stat().st_size > 20_000
        if not ok:
            print(f"[CLIP] Fast copy attempt failed: {(r_copy.stderr or '')[-220:]}")
        return ok

    # Signed YouTube URLs expire and transient failures happen -> retry a few times,
    # rotating to the next free proxy and stepping the quality down if needed.
    total_attempts = len(attempt_heights)
    for attempt, h_try in enumerate(attempt_heights, start=1):
        if attempt > 1 and not PROXY_URL:
            nxt = _rotate_free_proxy()
            print(f"[CLIP] Rotating to next free proxy: {nxt or '(none left)'}")
        try:
            if _fast_copy_once(h_try):
                downloaded_raw = True
                break
        except Exception as exc:
            print(f"[CLIP] Fast copy attempt {attempt} error: {exc}")
        if attempt < total_attempts:
            print(f"[CLIP] Retrying fast copy ({attempt}/{total_attempts}) at {attempt_heights[attempt]}p...")
            time.sleep(1.0 * attempt)

    if not downloaded_raw:
        # SAFE FALLBACK: only progressive (single-file) formats honour ranged
        # downloads. DASH separate video/audio tracks would pull the WHOLE file
        # (2-3 GB -> hours) which is what used to freeze the page on "Cutting...".
        _p("Fast path failed — trying a safer fallback download...")
        fallback_fmt = (
            f"best[height<={480}][ext=mp4][protocol^=http]/"
            f"best[height<={480}][ext=mp4]/best[ext=mp4]/best"
        )
        ydl_opts = {
            "format": fallback_fmt,
            "outtmpl": str(raw_path),
            "merge_output_format": "mp4",
            "ffmpeg_location": str(FFMPEG_DIR) if FFMPEG_DIR.exists() else None,
            "download_sections": yt_dlp.utils.download_range_func(None, [(start, end)]),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 25,
            # Hard safety caps: never let a stuck download run for hours.
            "max_filesize": 200 * 1024 * 1024,
            "retries": 2,
            "noprogress": True,
            **get_ytdlp_js_opts(),
        }
        ydl_opts = {k: v for k, v in ydl_opts.items() if v is not None}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as exc:
            ass_path.unlink(missing_ok=True)
            raise RuntimeError(
                "Could not download this clip section from YouTube. "
                "The server was blocked or the proxy is failing — "
                "open Settings → Free Proxy Pool → Refresh, or set your own PROXY_URL. "
                f"(detail: {str(exc)[:180]})"
            )
        if not raw_path.exists() or raw_path.stat().st_size < 20_000:
            matches = [
                m for m in CLIPS_DIR.glob(f"{tag}_raw.*") if m.stat().st_size > 20_000
            ]
            if matches:
                raw_path = matches[0]
            else:
                ass_path.unlink(missing_ok=True)
                raise RuntimeError(
                    "YouTube download produced no usable video data. "
                    "Please try again, or switch Captions OFF for a faster export."
                )

    # ---- STAGE 2: Local smooth CFR 30fps re-encode + Premium Captions ----
    print(f"[CLIP] Stage 2/2: Encoding smooth 30fps {MAX_HEIGHT}p MP4" + (f" + {theme_slug} captions..." if has_ass else "..."))
    _p(
        f"Encoding {MAX_HEIGHT}p 30fps" + (" + burning captions" if has_ass else "")
        + (" (render is still running — free tier CPU is slow, please wait)" if LOW_CPU else " — almost done...")
    )
    caption_note: str | None = None
    if has_ass:
        ok, _err = encode_smooth_clip(raw_path, final_path, ass_path=ass_path, font_file=used_font_file)
        ass_path.unlink(missing_ok=True)
        if ok:
            raw_path.unlink(missing_ok=True)
            print(f"[CLIP] Done! Saved smooth clip: {final_path.name}")
            return final_path, True, None
        caption_note = "Clip saved without captions (FFmpeg caption burn failed — check backend terminal)."
    else:
        ass_path.unlink(missing_ok=True)

    no_cc_path = CLIPS_DIR / f"{tag}.mp4"
    ok_plain, err_plain = encode_smooth_clip(raw_path, no_cc_path)
    raw_path.unlink(missing_ok=True)
    if not ok_plain:
        raise RuntimeError(f"Failed encoding smooth clip: {err_plain[-250:]}")
    print(f"[CLIP] Done! Saved smooth clip (no CC): {no_cc_path.name}")
    return no_cc_path, False, caption_note


def create_demo_project_for_user(user_id: int) -> dict[str, Any]:
    """Generate a real local demo video & transcript so the user can test cutting + captions even offline/in sandbox."""
    demo_vid = "demo_viralcut"
    demo_src = CLIPS_DIR / f"{demo_vid}_source.mp4"
    if not demo_src.exists() and FFMPEG_BIN:
        subprocess.run(
            [
                FFMPEG_BIN,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=1280x720:rate=24:duration=45",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=45,volume=0.25",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(demo_src),
            ],
            capture_output=True,
            timeout=60,
        )

    transcript = [
        {"start": 0.5, "duration": 4.2, "text": "Wait! Nobody talks about this biggest secret for viral shorts."},
        {"start": 5.0, "duration": 4.5, "text": "Most creators make the exact same mistake in the first 3 seconds!"},
        {"start": 10.0, "duration": 4.8, "text": "Here is how you actually hook viewers and double your watch time."},
        {"start": 15.5, "duration": 4.5, "text": "Listen closely: pacing, bold captions, and energy win every single time."},
        {"start": 21.0, "duration": 5.0, "text": "Look at this crazy jump in retention when you cut the boring pauses!"},
        {"start": 27.0, "duration": 5.0, "text": "Why does this trick work so fast? Because viewers decide in two seconds."},
        {"start": 33.0, "duration": 5.5, "text": "Finally, export in crisp 1080p with synced captions and post consistently!"},
        {"start": 39.0, "duration": 5.0, "text": "Try this on your next video and watch what happens to your views."},
    ]
    demo_title = "ViralCut AI Demo — How Top Creators Hook Viewers in 3 Seconds"
    suggestions = score_transcript_moments(
        transcript,
        clip_len=15,
        max_clips=3,
        video_title=demo_title,
        lang="en",
    )
    top_score = max((s["score"] for s in suggestions), default=92)
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO projects (user_id, video_id, url, title, thumbnail, duration, language, top_score, clip_length, max_clips, suggestions_json, transcript_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                demo_vid,
                "demo://viralcut-sample",
                demo_title,
                None,
                45.0,
                "en",
                top_score,
                15,
                3,
                json.dumps(suggestions),
                json.dumps(transcript),
                now,
            ),
        )
        proj_id = cur.lastrowid



    return {
        "id": proj_id,
        "video_id": demo_vid,
        "url": "demo://viralcut-sample",
        "title": demo_title,
        "thumbnail": None,
        "duration": 45.0,
        "language": "en",
        "top_score": top_score,
        "clip_length": 15,
        "max_clips": 3,
        "suggestions": suggestions,
        "created_at": now,
    }


def cut_demo_section(
    start: float,
    end: float,
    transcript: list[dict[str, Any]],
    with_captions: bool = True,
    caption_theme: str = "gold",
) -> tuple[Path, bool, str | None]:
    demo_src = CLIPS_DIR / "demo_viralcut_source.mp4"
    if not demo_src.exists() or not FFMPEG_BIN:
        raise RuntimeError("Demo source video or FFmpeg not ready.")

    start_i = max(0, int(math.floor(start)))
    end_i = max(start_i + 3, int(math.ceil(end)))
    theme_slug = caption_theme if caption_theme in CAPTION_THEMES else "gold"
    tag = f"demo_{start_i}_{end_i}_{MAX_HEIGHT}p_{theme_slug}_v3"
    raw_path = CLIPS_DIR / f"{tag}_raw.mp4"
    final_path = CLIPS_DIR / (f"{tag}_cc.mp4" if (with_captions and CAPTIONS) else f"{tag}.mp4")

    subprocess.run(
        [
            FFMPEG_BIN,
            "-y",
            "-ss", f"{start:.2f}",
            "-i", str(demo_src),
            "-t", f"{max(2.0, end - start):.2f}",
            "-c", "copy",
            str(raw_path),
        ],
        capture_output=True,
        timeout=60,
        check=True,
    )

    caption_note = None
    if with_captions and CAPTIONS and transcript:
        ass_path = CLIPS_DIR / f"{tag}.ass"
        has_ass, used_font_file = build_synced_ass(transcript, start, end, ass_path, theme_key=theme_slug)
        if has_ass:
            ok, _ = encode_smooth_clip(raw_path, final_path, ass_path=ass_path, font_file=used_font_file)
            ass_path.unlink(missing_ok=True)
            if ok:
                raw_path.unlink(missing_ok=True)
                return final_path, True, None
            caption_note = "Clip saved without captions."
        else:
            ass_path.unlink(missing_ok=True)

    no_cc = CLIPS_DIR / f"{tag}.mp4"
    encode_smooth_clip(raw_path, no_cc)
    raw_path.unlink(missing_ok=True)
    return no_cc, False, caption_note


# ============================================================
# FASTAPI APP & ROUTES
# ============================================================
app = FastAPI(title="ViralCut AI Backend", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class AnalyzeRequest(BaseModel):
    url: str
    clip_length: int = 30   # 30, 45, or 60 seconds
    max_clips: int = 5      # 5, 10, or 20 clips


class CutClipRequest(BaseModel):
    auto_text: bool = True
    project_id: int
    start: float
    end: float
    title: str
    description: str = ""
    hashtags: list[str] = []
    score: int = 85
    captions: bool = True
    caption_theme: str = "gold"


def _test_proxy() -> dict[str, Any]:
    """Live proxy check on whichever proxy is currently in use (paid or free pool)."""
    _p = get_active_proxy()
    if not _p:
        return {
            "ok": False,
            "detail": "No proxy available yet. The free pool may still be testing — check /api/proxy/pool.",
        }
    try:
        with httpx.Client(proxy=_p, timeout=12, follow_redirects=True) as client:
            res = client.get("https://ipinfo.io/json")
        data = res.json()
        return {
            "ok": res.status_code == 200,
            "status_code": res.status_code,
            "exit_ip": data.get("ip"),
            "country": data.get("country"),
            "org": data.get("org"),
        }
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}


def _mask_proxy(url: str) -> str:
    """Hide credentials when echoing a proxy back to the browser."""
    try:
        parts = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
        if parts.username or parts.password:
            netloc = f"{parts.hostname}:{parts.port or ''}"
            return f"{parts.scheme}://***:***@{netloc}"
    except Exception:
        pass
    return url if len(url) < 24 else url[:12] + "..."


class ProxyCheckRequest(BaseModel):
    proxy_url: str | None = None


@app.post("/api/proxy/check")
def proxy_check(body: ProxyCheckRequest, user: dict[str, Any] = Depends(get_current_user)):
    """Test ANY proxy line against ipinfo + YouTube, right from the app.
    Accepts http://user:pass@host:port, host:port:user:pass, or user:pass@host:port."""
    candidate = (body.proxy_url or PROXY_URL or "").strip()
    if not candidate:
        return {"ok": False, "detail": "No proxy given and PROXY_URL is not set."}

    # Normalise host:port:user:pass or user:pass@host:port into a URL
    if "://" not in candidate:
        bits = candidate.split(":")
        if len(bits) == 4 and not candidate.count("@"):
            host, port, usr, pwd = bits
            candidate = f"http://{urllib.parse.quote(usr, safe='')}:{urllib.parse.quote(pwd, safe='')}@{host}:{port}"
        else:
            candidate = "http://" + candidate

    out: dict[str, Any] = {
        "proxy": _mask_proxy(candidate),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ipinfo": {"ok": False, "detail": "not run"},
        "youtube": {"ok": False, "detail": "not run"},
    }

    try:
        with httpx.Client(proxy=candidate, timeout=15, follow_redirects=True) as client:
            res = client.get("https://ipinfo.io/json")
        data = res.json()
        out["ipinfo"] = {
            "ok": res.status_code == 200,
            "status_code": res.status_code,
            "exit_ip": data.get("ip"),
            "country": data.get("country"),
            "city": data.get("city"),
            "org": data.get("org"),
        }
    except Exception as exc:
        out["ipinfo"] = {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}

    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 20,
            "proxy": candidate,
            "source_address": "0.0.0.0",
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("https://www.youtube.com/watch?v=Dz0naoD1QWo", download=False)
        out["youtube"] = {
            "ok": True,
            "formats": len(info.get("formats") or []),
            "title": (info.get("title") or "")[:60],
        }
    except Exception as exc:
        out["youtube"] = {"ok": False, "detail": str(exc)[:200]}

    out["ok"] = bool(out["ipinfo"].get("ok") and out["youtube"].get("ok"))
    print(f"[PROXY CHECK] ok={out['ok']} ipinfo={out['ipinfo'].get('ok')} youtube={out['youtube'].get('ok')}")
    return out


@app.get("/api/proxy/pool")
def proxy_pool_status():
    with POOL_LOCK:
        proxies = list(PROXY_POOL.get("proxies") or [])
        snapshot = {
            "enabled": FREE_POOL_ENABLED and not bool(PROXY_URL),
            "status": PROXY_POOL.get("status"),
            "working": len(proxies),
            "proxies": proxies,
            "candidates": PROXY_POOL.get("candidates"),
            "reachable": PROXY_POOL.get("reachable"),
            "updated_at": PROXY_POOL.get("updated_at"),
            "age_sec": int(time.time() - float(PROXY_POOL.get("updated_at") or 0)),
            "last_error": PROXY_POOL.get("last_error"),
            "paid_proxy_set": bool(PROXY_URL),
        }
    return snapshot


@app.post("/api/proxy/pool/refresh")
def proxy_pool_refresh(user: dict[str, Any] = Depends(get_current_user)):
    if PROXY_POOL.get("status") == "testing":
        return {"status": "already testing", "message": "A refresh is already running (10-60s)."}
    threading.Thread(target=refresh_free_pool, daemon=True).start()
    return {
        "status": "started",
        "message": "Searching free proxy lists and testing them against YouTube. Takes 20-90 seconds.",
    }


@app.on_event("startup")
def _start_proxy_pool() -> None:
    _pool_load()
    if FREE_POOL_ENABLED and not PROXY_URL:
        threading.Thread(target=_pool_keeper, daemon=True).start()
        print("[FREE POOL] keeper started (auto-discovers free proxies every 30 min)")


@app.get("/api/health")
def health(proxy: int = 0):
    payload: dict[str, Any] = {
        "status": "ok",
        "service": "ViralCut AI Backend",
        "ffmpeg_ready": bool(FFMPEG_BIN),
        "font": FONT_NAME,
        "max_height": MAX_HEIGHT,
        "captions_enabled": CAPTIONS,
        "upload_enabled": True,
        "youtube_cookies_loaded": bool(YOUTUBE_COOKIE_FILE and Path(YOUTUBE_COOKIE_FILE).exists()),
        "youtube_cookies_count": YOUTUBE_COOKIES_COUNT,
        "youtube_auth_cookies_ok": YOUTUBE_AUTH_COOKIES_OK,
        "db_mode": _db_mode(),
        "db_warning": DB_WARNING or (None if not _PG_FAILED else "Postgres unreachable — using temporary SQLite."),
        "auto_captions": _whisper_available(),
        "whisper_model": WHISPER_MODEL_SIZE,
        "whisper_model_non_en": WHISPER_MODEL_NON_EN,
        "asr_forced_language": WHISPER_FORCE_LANG or "auto",
        "analyze_asr": os.getenv("ANALYZE_ASR", "1").strip().lower() in ("1", "true", "yes"),
        "llm_tdh": llm_backend(),
        "asr_backend": asr_backend(),
        "proxy_configured": bool(PROXY_URL),
        "active_proxy": _mask_proxy(get_active_proxy()) if get_active_proxy() else None,
        "free_proxy_pool": {
            "enabled": FREE_POOL_ENABLED and not bool(PROXY_URL),
            "status": PROXY_POOL.get("status"),
            "working": len(PROXY_POOL.get("proxies") or []),
            "candidates": PROXY_POOL.get("candidates"),
            "reachable": PROXY_POOL.get("reachable"),
            "age_sec": int(time.time() - float(PROXY_POOL.get("updated_at") or 0)),
            "last_error": PROXY_POOL.get("last_error"),
        },
        "encode_preset": ENCODE_PRESET,
        "encode_crf": ENCODE_CRF,
        "low_cpu_mode": LOW_CPU,
        "max_upload_mb": MAX_UPLOAD_MB,
        "disk_free_mb": round(_free_mb(), 1),
        "email_verification": "on (gmail otp)" if smtp_ready() else "off - set SMTP_APP_PASSWORD on Render",
        "build": BUILD_TAG,
    }
    if proxy:
        payload["proxy_test"] = _test_proxy()
    return payload


# ---------- AUTH ROUTES ----------

# ---------------- SIGN-UP EMAIL POLICY ----------------
# Only real Gmail addresses. Anyone can type "@gmail.com", so a domain check is the
# strongest gate that needs no email round-trip (set SMTP_APP_PASSWORD + the verify
# flow for full proof of ownership).
ALLOWED_EMAIL_DOMAINS = {
    d.strip().lower()
    for d in os.getenv("ALLOWED_EMAIL_DOMAINS", "gmail.com,googlemail.com").split(",")
    if d.strip()
}
BLOCKED_EMAIL_DOMAINS = {
    "example.com", "example.org", "test.com", "localhost", "mailinator.com",
    "yopmail.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "trashmail.com", "sharklasers.com", "getnada.com",
    "dispostable.com", "fakeinbox.com", "maildrop.cc", "mintemail.com",
    "spam4.me", "grr.la", "throwawaymail.com", "mailnesia.com", "mohmal.com",
    "emailondeck.com", "moakt.com", "tempr.email", "discard.email", "33mail.com",
    "mailcatch.com", "inboxkitten.com", "mailsac.com", "tempail.com",
}


def validate_signup_email(email: str) -> str:
    """Raise a friendly 400 unless this looks like a real Gmail address."""
    email = (email or "").strip().lower()
    if not re.match(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$", email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    domain = email.rsplit("@", 1)[-1]
    if domain in BLOCKED_EMAIL_DOMAINS:
        raise HTTPException(
            status_code=400,
            detail="Temporary or disposable email addresses are not allowed. Please use your Gmail account.",
        )
    if domain not in ALLOWED_EMAIL_DOMAINS:
        raise HTTPException(
            status_code=400,
            detail="Sign-up is Gmail only. Please register with a @gmail.com address.",
        )
    local = email.split("@")[0]
    # Gmail's own rules: 6-30 chars, starts with a letter, letters/digits/dots only.
    if (
        not re.match(r"^[a-z][a-z0-9.]{5,29}$", local)
        or ".." in local
        or local.endswith(".")
    ):
        raise HTTPException(
            status_code=400,
            detail="That Gmail address doesn't look valid. Gmail addresses are 6-30 characters and start with a letter.",
        )
    return email



# ---------------- GMAIL OTP VERIFICATION ----------------
VERIFY_CODE_TTL_MIN = int(os.getenv("VERIFY_CODE_TTL_MIN", "10"))
VERIFY_MAX_ATTEMPTS = int(os.getenv("VERIFY_MAX_ATTEMPTS", "5"))
VERIFY_RESEND_SECONDS = int(os.getenv("VERIFY_RESEND_SECONDS", "60"))


def smtp_ready() -> bool:
    """True when a Gmail app password is configured (needed to actually email an OTP)."""
    return bool(SMTP_EMAIL.strip() and SMTP_APP_PASSWORD.replace(" ", "").strip())


def _gen_otp() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


def send_verify_email(to_email: str, user_name: str, code: str) -> None:
    """Email the 6-digit sign-up verification code via Gmail SMTP (SSL 465)."""
    app_pw = SMTP_APP_PASSWORD.replace(" ", "").strip()
    sender = SMTP_EMAIL.strip()
    if not sender or not app_pw:
        raise RuntimeError(
            "Gmail SMTP is not configured. Add a 16-character Google App Password "
            "to the SMTP_APP_PASSWORD env var on Render."
        )
    msg = EmailMessage()
    msg["Subject"] = f"{code} is your ViralCut AI verification code"
    msg["From"] = f"ViralCut AI <{sender}>"
    msg["To"] = to_email
    msg.set_content(
        f"Hi {user_name},\n\n"
        f"Your ViralCut AI verification code is: {code}\n\n"
        f"Enter it on the sign-up screen to activate your account. "
        f"The code expires in {VERIFY_CODE_TTL_MIN} minutes.\n\n"
        f"If you did not sign up, ignore this email - no account will be activated.\n\n"
        f"- ViralCut AI"
    )
    msg.add_alternative(
        f"""
        <div style="font-family:Inter,Arial,sans-serif;background:#090912;color:#f5f3ff;padding:32px;border-radius:14px;max-width:480px;margin:auto;">
          <h2 style="margin:0 0 8px;color:#ffffff;">ViralCut <span style="color:#a66aff;">AI</span></h2>
          <p style="color:#b8b5cf;font-size:14px;">Hi {user_name},</p>
          <p style="color:#d6d3eb;font-size:14px;line-height:1.6;">
            Confirm this is your Gmail address to activate your ViralCut AI account.
            The code expires in <strong>{VERIFY_CODE_TTL_MIN} minutes</strong>.
          </p>
          <div style="margin:22px 0;padding:16px;text-align:center;background:#17152a;border:1px solid #4c2f8a;border-radius:12px;font-size:32px;font-weight:800;letter-spacing:8px;color:#ffd726;">
            {code}
          </div>
          <p style="color:#8f8ca8;font-size:12px;">If you didn't sign up for ViralCut AI, you can safely ignore this email.</p>
        </div>
        """,
        subtype="html",
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
        smtp.login(sender, app_pw)
        smtp.send_message(msg)


def _issue_verify_code(uid: int, email: str, name: str) -> str:
    """Create + store a fresh OTP and email it. Raises HTTPException if the mail fails."""
    code = _gen_otp()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=VERIFY_CODE_TTL_MIN)).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET verify_code = ?, verify_expires = ?, verify_attempts = 0, verify_sent_at = ? WHERE id = ?",
            (code, expires, datetime.now(timezone.utc).isoformat(), uid),
        )
    try:
        send_verify_email(email, name, code)
    except Exception as exc:
        print(f"[OTP] send failed for {email}: {exc}")
        raise HTTPException(
            status_code=502,
            detail="We could not send the verification email right now. Please try 'Resend code' in a minute.",
        )
    print(f"[OTP] verification code sent to {email}")
    return code


class VerifyEmailRequest(BaseModel):
    email: str
    code: str


class EmailOnlyRequest(BaseModel):
    email: str


@app.post("/api/auth/verify-email")
def verify_email(body: VerifyEmailRequest):
    email = body.email.strip().lower()
    code = re.sub(r"\D", "", str(body.code or ""))[:6]
    if len(code) != 6:
        raise HTTPException(status_code=400, detail="Enter the full 6-digit code from your Gmail.")

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, email, is_admin, created_at, verified, verify_code, verify_expires, verify_attempts "
            "FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No account found for that email. Please sign up again.")

        uid = row["id"]
        if int(row["verified"] or 0) == 1:
            token = create_token(uid, row["email"], row["name"])
            return {"token": token, "user": serialize_user(row), "message": "Email already verified."}

        attempts = int(row["verify_attempts"] or 0)
        if attempts >= VERIFY_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=429,
                detail="Too many wrong codes. Tap 'Resend code' to get a fresh one.",
            )

        expired = True
        try:
            exp = datetime.fromisoformat(str(row["verify_expires"]))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            expired = datetime.now(timezone.utc) > exp
        except Exception:
            expired = True
        if expired or not row["verify_code"]:
            raise HTTPException(
                status_code=400,
                detail="That code has expired. Tap 'Resend code' to get a new one.",
            )

        if code != str(row["verify_code"]):
            left = VERIFY_MAX_ATTEMPTS - attempts - 1
            conn.execute("UPDATE users SET verify_attempts = ? WHERE id = ?", (attempts + 1, uid))
            raise HTTPException(
                status_code=400,
                detail=f"Incorrect code. {left} attempt(s) left before you must resend."
                if left > 0
                else "Incorrect code. Tap 'Resend code' to get a new one.",
            )

        conn.execute(
            "UPDATE users SET verified = 1, verify_code = NULL, verify_expires = NULL, "
            "verify_attempts = 0, verify_sent_at = NULL WHERE id = ?",
            (uid,),
        )
        row = conn.execute(
            "SELECT id, name, email, is_admin, created_at FROM users WHERE id = ?", (uid,)
        ).fetchone()

    print(f"[OTP] {email} verified")
    token = create_token(uid, row["email"], row["name"])
    return {"token": token, "user": serialize_user(row), "message": "Gmail verified. Welcome aboard!"}


@app.post("/api/auth/resend-code")
def resend_verify_code(body: EmailOnlyRequest):
    email = body.email.strip().lower()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, verified, verify_sent_at FROM users WHERE email = ?", (email,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No account found for that email.")
    if int(row["verified"] or 0) == 1:
        raise HTTPException(status_code=400, detail="This email is already verified - please just log in.")
    try:
        last = datetime.fromisoformat(str(row["verify_sent_at"]))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        waited = (datetime.now(timezone.utc) - last).total_seconds()
        if waited < VERIFY_RESEND_SECONDS:
            raise HTTPException(
                status_code=429,
                detail=f"Please wait {int(VERIFY_RESEND_SECONDS - waited)}s before requesting another code.",
            )
    except HTTPException:
        raise
    except Exception:
        pass
    _issue_verify_code(row["id"], email, row["name"])
    return {"message": f"A new 6-digit code is on its way to {email}."}


@app.post("/api/auth/register")
def register(body: RegisterRequest):
    name = body.name.strip()
    email = body.email.strip().lower()
    password = body.password

    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Please enter your full name (at least 2 characters).")
    email = validate_signup_email(email)
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    now = datetime.now(timezone.utc).isoformat()
    pw_hash = hash_password(password)
    try:
        with get_db() as conn:
            existing_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            is_admin = 1 if (existing_count == 0 or email in {e.lower() for e in ADMIN_EMAILS}) else 0
            needs_otp = smtp_ready() and is_admin == 0
            cur = conn.execute(
                "INSERT INTO users (name, email, password_hash, is_admin, created_at, verified) VALUES (?, ?, ?, ?, ?, ?)",
                (name, email, pw_hash, is_admin, now, 0 if needs_otp else 1),
            )
            uid = cur.lastrowid
            row = conn.execute(
                "SELECT id, name, email, is_admin, created_at FROM users WHERE id = ?",
                (uid,),
            ).fetchone()
    except Exception as exc:
        if not _is_duplicate_error(exc):
            raise
        raise HTTPException(status_code=409, detail="An account with this email already exists. Please log in.")

    user_obj = serialize_user(row)

    if needs_otp:
        _issue_verify_code(uid, email, name)
        return {
            "needs_verification": True,
            "email": email,
            "user": user_obj,
            "message": f"We emailed a 6-digit code to {email}. Enter it to activate your account.",
        }

    if not smtp_ready():
        print("[OTP] SMTP not configured - account activated without email verification")
    token = create_token(uid, email, name)
    return {"token": token, "user": user_obj}


@app.post("/api/auth/login")
def login(body: LoginRequest):
    email = body.email.strip().lower()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, email, password_hash, is_admin, created_at, blocked, verified FROM users WHERE email = ?",
            (email,),
        ).fetchone()

    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    if int(row["verified"] or 0) == 0 and smtp_ready():
        recent = False
        try:
            with get_db() as conn2:
                v = conn2.execute(
                    "SELECT verify_code, verify_sent_at FROM users WHERE id = ?", (row["id"],)
                ).fetchone()
            if v and v["verify_code"]:
                last = datetime.fromisoformat(str(v["verify_sent_at"]))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                recent = (datetime.now(timezone.utc) - last).total_seconds() < VERIFY_RESEND_SECONDS
        except Exception:
            recent = False
        if not recent:
            try:
                _issue_verify_code(row["id"], row["email"], row["name"])
            except Exception:
                pass
        return {
            "needs_verification": True,
            "email": row["email"],
            "message": f"Verify your Gmail first - we just sent a fresh 6-digit code to {row['email']}.",
        }
    try:
        if int(row["blocked"] or 0) == 1:
            raise HTTPException(
                status_code=403,
                detail="This account has been blocked by the admin. Contact rahulgoudapatil7@gmail.com.",
            )
    except HTTPException:
        raise
    except Exception:
        pass

    user_obj = serialize_user(row)
    token = create_token(row["id"], row["email"], row["name"])
    return {"token": token, "user": user_obj}


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str


@app.get("/api/auth/me")
def me(user: dict[str, Any] = Depends(get_current_user)):
    return {"user": user}


def send_reset_email(to_email: str, user_name: str, code: str) -> None:
    """Send the 6-digit password reset code securely via Gmail SMTP (SSL port 465)."""
    app_pw = SMTP_APP_PASSWORD.replace(" ", "").strip()
    sender = SMTP_EMAIL.strip()
    if not sender or not app_pw:
        raise RuntimeError(
            "Gmail SMTP is not configured yet. Add your 16-character Google App Password to SMTP_APP_PASSWORD in Backend/main.py."
        )

    msg = EmailMessage()
    msg["Subject"] = f"{code} is your ViralCut AI password reset code"
    msg["From"] = f"ViralCut AI <{sender}>"
    msg["To"] = to_email
    msg.set_content(
        f"Hi {user_name},\n\n"
        f"We received a request to reset your ViralCut AI password.\n\n"
        f"Your 6-digit verification code is: {code}\n\n"
        f"This code expires in 15 minutes. If you did not request this, you can safely ignore this email.\n\n"
        f"— ViralCut AI"
    )
    msg.add_alternative(
        f"""
        <div style="font-family:Inter,Arial,sans-serif;background:#090912;color:#f5f3ff;padding:32px;border-radius:14px;max-width:480px;margin:auto;">
          <h2 style="margin:0 0 8px;color:#ffffff;">✂ ViralCut <span style="color:#a66aff;">AI</span></h2>
          <p style="color:#b8b5cf;font-size:14px;">Hi {user_name},</p>
          <p style="color:#d6d3eb;font-size:14px;line-height:1.6;">
            Use the 6-digit code below to reset your ViralCut AI password. This code expires in <strong>15 minutes</strong>.
          </p>
          <div style="margin:22px 0;padding:16px;text-align:center;background:#17152a;border:1px solid #4c2f8a;border-radius:12px;font-size:32px;font-weight:800;letter-spacing:8px;color:#ffd726;">
            {code}
          </div>
          <p style="color:#8f8ca8;font-size:12px;">
            If you didn't request a password reset, you can safely ignore this email — your account is still secure.
          </p>
        </div>
        """,
        subtype="html",
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
        smtp.login(sender, app_pw)
        smtp.send_message(msg)


@app.post("/api/auth/forgot-password")
def forgot_password(body: ForgotPasswordRequest):
    email = body.email.strip().lower()
    with get_db() as conn:
        row = conn.execute("SELECT id, name FROM users WHERE email = ?", (email,)).fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail="No account found with this email. Please register first.",
            )

        code = f"{secrets.randbelow(900000) + 100000}"  # 6-digit numeric code
        expires_at = int(time.time()) + 15 * 60  # valid for 15 minutes
        conn.execute("DELETE FROM password_resets WHERE email = ?", (email,))
        conn.execute(
            "INSERT INTO password_resets (email, code, expires_at, attempts, used) VALUES (?, ?, ?, 0, 0)",
            (email, code, expires_at),
        )

    try:
        send_reset_email(email, row["name"], code)
    except Exception as exc:
        print(f"[EMAIL SEND FAILED] {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"Could not send reset email: {exc}",
        )

    # SECURITY: Never return the reset code in the API response!
    return {
        "sent": True,
        "message": f"A 6-digit verification code has been sent to {email}. Check your Gmail inbox (and Spam folder).",
    }


@app.post("/api/auth/reset-password")
def reset_password(body: ResetPasswordRequest):
    email = body.email.strip().lower()
    code = body.code.strip()
    new_password = body.new_password

    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters.")

    now_ts = int(time.time())
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, code, expires_at, attempts, used
            FROM password_resets
            WHERE email = ? AND used = 0
            ORDER BY id DESC LIMIT 1
            """,
            (email,),
        ).fetchone()

        if not row:
            raise HTTPException(status_code=400, detail="No active reset request found. Please request a new code.")
        if row["expires_at"] < now_ts:
            raise HTTPException(status_code=400, detail="Reset code has expired. Please request a new code.")
        if (row["attempts"] or 0) >= 5:
            conn.execute("UPDATE password_resets SET used = 1 WHERE id = ?", (row["id"],))
            conn.commit()
            raise HTTPException(status_code=429, detail="Too many wrong attempts. Please request a new reset code.")

        if not hmac.compare_digest(row["code"], code):
            conn.execute("UPDATE password_resets SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
            conn.commit()
            raise HTTPException(status_code=400, detail="Invalid 6-digit code. Please check your Gmail and try again.")

        pw_hash = hash_password(new_password)
        conn.execute("UPDATE users SET password_hash = ? WHERE email = ?", (pw_hash, email))
        conn.execute("UPDATE password_resets SET used = 1 WHERE id = ?", (row["id"],))

    return {"reset": True, "message": "Password updated successfully! You can now log in with your new password."}


# ---------- ADMIN ROUTE (SEE ALL REGISTERED USERS) ----------

@app.post("/api/admin/users/{user_id}/block")
def admin_block_user(user_id: int, body: dict[str, Any] | None = None,
                     admin: dict[str, Any] = Depends(require_admin)):
    """ADMIN ONLY: block (or unblock) an account. Blocked users cannot log in and lose live sessions."""
    if int(user_id) == int(admin["id"]):
        raise HTTPException(status_code=400, detail="You cannot block your own admin account.")
    want_blocked = True if not body else bool(body.get("blocked", True))
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        row = conn.execute("SELECT id, email, is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="That account no longer exists.")
        if int(row["is_admin"] or 0) == 1 and want_blocked:
            raise HTTPException(status_code=400, detail="That account is an admin and cannot be blocked.")
        conn.execute(
            "UPDATE users SET blocked = ?, blocked_at = ? WHERE id = ?",
            (1 if want_blocked else 0, now if want_blocked else None, user_id),
        )
    print(f"[ADMIN] {'blocked' if want_blocked else 'unblocked'} user {user_id} ({row['email']})")
    return {"user_id": user_id, "email": row["email"], "blocked": want_blocked}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, admin: dict[str, Any] = Depends(require_admin)):
    """ADMIN ONLY: remove an account and everything it created."""
    if int(user_id) == int(admin["id"]):
        raise HTTPException(status_code=400, detail="You cannot remove your own admin account.")
    with get_db() as conn:
        target = conn.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="That account no longer exists.")
        files: list[str] = []
        for row in conn.execute("SELECT filename FROM clips WHERE user_id = ?", (user_id,)).fetchall():
            if row["filename"]:
                files.append(row["filename"])
        uploads: list[str] = []
        for row in conn.execute("SELECT url, id FROM projects WHERE user_id = ?", (user_id,)).fetchall():
            if str(row["url"] or "").startswith("upload://"):
                uploads.append(str(row["url"]).replace("upload://", "", 1))
            try:
                conn.execute("DELETE FROM project_thumbs WHERE project_id = ?", (row["id"],))
            except Exception:
                pass
        conn.execute("DELETE FROM clips WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM projects WHERE user_id = ?", (user_id,))
        for table in ("password_resets",):
            try:
                conn.execute(f"DELETE FROM {table} WHERE email = ?", (target["email"],))
            except Exception:
                pass
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    # Best-effort file cleanup (the DB rows are already gone).
    for name in files:
        try:
            (CLIPS_DIR / Path(name).name).unlink(missing_ok=True)
        except Exception:
            pass
    for name in uploads:
        try:
            (UPLOADS_DIR / Path(name).name).unlink(missing_ok=True)
        except Exception:
            pass
    print(f"[ADMIN] removed user {user_id} ({target['email']}), {len(files)} clips, {len(uploads)} uploads")
    return {"deleted": True, "user_id": user_id, "email": target["email"],
            "clips_removed": len(files), "uploads_removed": len(uploads)}


@app.get("/api/admin/users")
def admin_list_users(_admin: dict[str, Any] = Depends(require_admin)):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                u.id,
                u.name,
                u.email,
                u.is_admin,
                u.blocked,
                u.created_at,
                (SELECT COUNT(*) FROM projects p WHERE p.user_id = u.id) AS projects_count,
                (SELECT COUNT(*) FROM clips c WHERE c.user_id = u.id) AS clips_count
            FROM users u
            ORDER BY u.id DESC
            """
        ).fetchall()

    users = []
    for r in rows:
        users.append(
            {
                **serialize_user(r),
                "blocked": bool(int(r["blocked"] or 0)),
                "projects_count": r["projects_count"],
                "clips_count": r["clips_count"],
            }
        )

    return {
        "summary": {
            "total_users": len(users),
            "total_projects": sum(u["projects_count"] for u in users),
            "total_clips": sum(u["clips_count"] for u in users),
        },
        "users": users,
    }


# ---------- WORKSPACE DATA (STRICTLY USER-SCOPED, ZERO FAKE CLIPS) ----------
@app.get("/api/workspace")
def get_workspace(user: dict[str, Any] = Depends(get_current_user)):
    with get_db() as conn:
        proj_rows = conn.execute(
            """
            SELECT id, video_id, url, title, thumbnail, duration, language, top_score, clip_length, max_clips, suggestions_json, created_at
            FROM projects
            WHERE user_id = ?
            ORDER BY id DESC
            """,
            (user["id"],),
        ).fetchall()

        clip_rows = conn.execute(
            """
            SELECT id, project_id, video_id, title, description, hashtags_json, start_time, end_time, score, filename, has_captions, quality, created_at,
                   CASE WHEN video_b64 IS NULL THEN 0 ELSE 1 END AS has_db_copy
            FROM clips
            WHERE user_id = ?
            ORDER BY id DESC
            """,
            (user["id"],),
        ).fetchall()

    projects = []
    for p in proj_rows:
        projects.append(
            {
                "id": p["id"],
                "video_id": p["video_id"],
                "url": p["url"],
                "title": p["title"],
                "thumbnail": p["thumbnail"] or _project_thumbnail_url(p),
                "duration": p["duration"],
                "language": p["language"],
                "top_score": p["top_score"],
                "clip_length": p["clip_length"] or 30,
                "max_clips": p["max_clips"] or 5,
                "suggestions": json.loads(p["suggestions_json"]),
                "created_at": p["created_at"],
            }
        )

    # Return every clip the user cut. A missing file (Render restart) is flagged
    # rather than hidden — the history and metadata must survive.
    clips = []
    for c in clip_rows:
        file_path = CLIPS_DIR / c["filename"]
        try:
            db_copy = bool(c["has_db_copy"])
        except Exception:
            db_copy = False
        has_file = file_path.exists() or db_copy
        try:
            tags = json.loads(c["hashtags_json"] or "[]")
        except Exception:
            tags = []
        clips.append(
            {
                "id": c["id"],
                "project_id": c["project_id"],
                "video_id": c["video_id"],
                "title": c["title"],
                "description": c["description"] or "",
                "hashtags": tags,
                "start": c["start_time"],
                "end": c["end_time"],
                "duration": round(c["end_time"] - c["start_time"], 1),
                "score": c["score"],
                "filename": c["filename"],
                "has_captions": bool(c["has_captions"]),
                "quality": c["quality"],
                "video_url": f"/api/clips/file/{c['filename']}",
                "download_url": f"/api/clips/file/{c['filename']}?download=1",
                "created_at": c["created_at"],
                "available": has_file,
            }
        )

    top_score = max(
        [c["score"] for c in clips] + [p["top_score"] for p in projects],
        default=None,
    )
    time_saved_min = len(clips) * 12 + len(projects) * 5

    return {
        "stats": {
            "projects_count": len(projects),
            "clips_count": len(clips),
            "top_score": top_score,
            "time_saved_min": time_saved_min,
        },
        "projects": projects,
        "clips": clips,
    }


# ---------- ANALYZE VIDEO ----------
@app.post("/api/analyze")
def analyze_video(body: AnalyzeRequest, user: dict[str, Any] = Depends(get_current_user)):
    url = body.url.strip()
    video_id = extract_video_id(url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Please paste a valid YouTube video URL.")

    clip_length = body.clip_length if body.clip_length in (30, 45, 60) else 30
    max_clips = body.max_clips if body.max_clips in (5, 10, 20) else 5

    try:
        segments, lang = get_transcript(video_id, url=url)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                "YouTube links need a working residential proxy, which is unreliable on free hosting. "
                "Use the Upload Video (MP4) button instead — it is 100% free and never blocked. "
                f"(details: {str(exc)[:160]})"
            ),
        )

    meta = fetch_video_metadata(url, video_id)
    if not meta["duration"] and segments:
        meta["duration"] = round(max(s["start"] + s["duration"] for s in segments), 1)

    suggestions = score_transcript_moments(
        segments,
        clip_len=clip_length,
        max_clips=max_clips,
        video_title=meta["title"],
        lang=lang,
    )
    if not suggestions:
        raise HTTPException(status_code=400, detail="Transcript was too short to detect viral moments.")


    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO projects (user_id, video_id, url, title, thumbnail, duration, language, top_score, clip_length, max_clips, suggestions_json, transcript_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                video_id,
                url,
                meta["title"],
                meta["thumbnail"],
                meta["duration"],
                lang,
                top_score,
                clip_length,
                max_clips,
                json.dumps(suggestions),
                json.dumps(segments),
                now,
            ),
        )
        proj_id = cur.lastrowid

    return {
        "project": {
            "id": proj_id,
            "video_id": video_id,
            "url": url,
            "title": meta["title"],
            "thumbnail": meta["thumbnail"],
            "duration": meta["duration"],
            "language": lang,
            "top_score": top_score,
            "clip_length": clip_length,
            "max_clips": max_clips,
            "suggestions": suggestions,
            "created_at": now,
        }
    }


# ---------------- AUTO CAPTIONS (offline Whisper) ----------------
# Uploads rarely come with .srt files, so the "Captions: ON" toggle had nothing to
# burn. We now transcribe the clip's own audio (30-60s) at cut time with
# faster-whisper on CPU. English is fine on 'tiny'; Hindi/other languages need a
# bigger model and an explicit language, otherwise auto-detect guesses wrong and the
# captions come out as gibberish in the wrong script.
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL", "tiny")
WHISPER_MODEL_NON_EN = os.getenv("WHISPER_MODEL_NON_EN", "base")
WHISPER_FORCE_LANG = os.getenv("ASR_LANGUAGE", "").strip().lower()  # e.g. "hi" or "en"
# Free hosting has 512 MB RAM, so by default one model is kept in memory at a time.
WHISPER_KEEP_LOADED = os.getenv("WHISPER_KEEP_LOADED", "0").strip().lower() in ("1", "true", "yes")

WHISPER_HINTS = {
    "hi": "यह वीडियो हिंदी में है। हिंदी में ही लिखें।",
    "mr": "हे व्हिडिओ मराठीत आहे.",
    "ta": "இந்த வீடியோ தமிழில் உள்ளது.",
    "te": "ఈ వీడియో తెలుగులో ఉంది.",
    "bn": "এই ভিডিওটি বাংলায়।",
    "gu": "આ વિડિયો ગુજરાતીમાં છે.",
    "kn": "ಈ ವೀಡಿಯೊ ಕನ್ನಡದಲ್ಲಿದೆ.",
    "ml": "ഈ വീഡിയോ മലയാളത്തിലാണ്.",
    "pa": "ਇਹ ਵੀਡੀਓ ਪੰਜਾਬੀ ਵਿੱਚ ਹੈ।",
}

_WHISPER_CACHE: dict[str, Any] = {}
_WHISPER_locked_off = False
_WHISPER_LOCK = threading.Lock()


def _whisper_model(size: str | None = None):
    """Load (and cache) one Whisper model; older models are dropped to save RAM."""
    global _WHISPER_locked_off
    size = (size or WHISPER_MODEL_SIZE or "tiny").strip()
    with _WHISPER_LOCK:
        if size in _WHISPER_CACHE:
            return _WHISPER_CACHE[size]
        if _WHISPER_locked_off:
            return None
        # Only one model in RAM at a time (512 MB box).
        if _WHISPER_CACHE:
            _WHISPER_CACHE.clear()
            import gc
            gc.collect()
        try:
            from faster_whisper import WhisperModel

            cache = DATA_DIR / "whisper"
            cache.mkdir(parents=True, exist_ok=True)
            print(f"[ASR] loading whisper '{size}' (first time downloads the model)...")
            model = WhisperModel(
                size,
                device="cpu",
                compute_type="int8",
                cpu_threads=2,
                download_root=str(cache),
            )
            _WHISPER_CACHE[size] = model
            print(f"[ASR] whisper '{size}' ready")
            return model
        except Exception as exc:
            print(f"[ASR] auto-captions unavailable ({exc}); .srt upload still works")
            if size == WHISPER_MODEL_SIZE:
                _WHISPER_locked_off = True
            return None


def _unload_whisper() -> None:
    with _WHISPER_LOCK:
        if _WHISPER_CACHE:
            _WHISPER_CACHE.clear()
            import gc
            gc.collect()
            print("[ASR] model unloaded to free RAM")


def _whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _model_for_lang(lang: str) -> str:
    lang = (lang or "").lower()
    if lang.startswith("en") or not lang:
        return WHISPER_MODEL_SIZE
    return WHISPER_MODEL_NON_EN


def _detect_speech_language(audio) -> tuple[str, float]:
    """Ask Whisper which language this audio is in (first ~25 s only — cheap)."""
    if WHISPER_FORCE_LANG:
        return WHISPER_FORCE_LANG, 1.0
    model = _whisper_model(WHISPER_MODEL_NON_EN if WHISPER_MODEL_NON_EN != WHISPER_MODEL_SIZE else WHISPER_MODEL_SIZE)
    if model is None:
        return "", 0.0
    try:
        probe = audio[: 16000 * 25]
        if len(probe) < 16000:
            return "", 0.0
        segments, info = model.transcribe(
            probe, beam_size=1, vad_filter=True, without_timestamps=True, task="transcribe"
        )
        for _ in segments:  # drain so detection completes
            pass
        lang = getattr(info, "language", "") or ""
        prob = float(getattr(info, "language_probability", 0.0) or 0.0)
        print(f"[ASR] detected language={lang or '?'} (p={prob:.2f})")
        return lang, prob
    except Exception as exc:
        print(f"[ASR] language detection failed ({exc}) — falling back to auto")
        return "", 0.0


# ---------------- CLOUD ASR (free Groq Whisper) ----------------
# Local tiny/base models are fine for English but produce garbage on Hindi inside a
# 512 MB container. Groq's free tier runs whisper-large-v3-turbo (8 h/day free), so
# when GROQ_API_KEY is set we transcribe there; otherwise we fall back to local.
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
GROQ_ASR_MODEL = os.getenv("GROQ_ASR_MODEL", "whisper-large-v3")
GROQ_ASR_MAX_SEC = float(os.getenv("GROQ_ASR_MAX_SEC", "600"))  # 25 MB cap -> ~13 min at 16 kHz


def asr_backend() -> str:
    if GROQ_API_KEY:
        return "groq-" + GROQ_ASR_MODEL
    return "local-" + WHISPER_MODEL_NON_EN


def _pcm_to_wav(raw: bytes, rate: int = 16000) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(raw)
    return buf.getvalue()


def _groq_transcribe_pcm(raw: bytes, lang: str = "") -> list[dict[str, Any]] | None:
    """Transcribe 16 kHz mono PCM via Groq. Returns None if the call fails."""
    if not GROQ_API_KEY or not raw:
        return None
    chunk_bytes = int(GROQ_ASR_MAX_SEC * 16000 * 2)
    out: list[dict[str, Any]] = []
    detected = ""
    for offset_bytes in range(0, len(raw), chunk_bytes):
        piece = raw[offset_bytes: offset_bytes + chunk_bytes]
        if len(piece) < 32000:  # under 1 s — nothing useful
            continue
        offset = offset_bytes / 32000.0
        data = {
            "model": GROQ_ASR_MODEL,
            "response_format": "verbose_json",
            "temperature": "0",
            # real word timings -> captions land exactly when the words are spoken
            "timestamp_granularities[]": ["word", "segment"],
        }
        if lang:
            data["language"] = lang
        wav = _pcm_to_wav(piece)
        try:
            r = httpx.post(
                f"{GROQ_BASE_URL}/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                data=data,
                files={"file": ("audio.wav", wav, "audio/wav")},
                timeout=240,
            )
            if r.status_code == 400 and "timestamp_granularities[]" in data:
                # Model/plan may not support word timings — retry without them.
                data.pop("timestamp_granularities[]", None)
                r = httpx.post(
                    f"{GROQ_BASE_URL}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    data=data,
                    files={"file": ("audio.wav", wav, "audio/wav")},
                    timeout=240,
                )
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:
            print(f"[ASR] groq failed ({type(exc).__name__}: {str(exc)[:140]})")
            return None
        detected = payload.get("language") or detected
        segs = payload.get("segments") or []
        all_words: list[dict[str, Any]] = []
        for w in payload.get("words") or []:
            try:
                w_text = str(w.get("word") or w.get("text") or "").strip()
                w_start = float(w.get("start", 0.0))
                w_end = float(w.get("end", w_start + 0.2))
            except Exception:
                continue
            if w_text:
                all_words.append({"w": w_text, "s": offset + w_start, "e": offset + w_end})
        for s in segs:
            text = str(s.get("text", "")).strip()
            if not text:
                continue
            try:
                s_start = float(s.get("start", 0.0))
                s_end = float(s.get("end", s_start + 1.0))
            except Exception:
                continue
            seg_words = [
                {"w": w["w"], "s": round(w["s"], 3), "e": round(w["e"], 3)}
                for w in all_words
                if (s_start - 0.2 + offset) <= ((w["s"] + w["e"]) / 2.0) <= (s_end + 0.2 + offset)
            ]
            out.append(
                {
                    "start": round(offset + s_start, 2),
                    "duration": round(max(0.4, s_end - s_start), 2),
                    "text": text,
                    **({"words": seg_words} if seg_words else {}),
                }
            )
        if not segs and payload.get("text"):
            out.append(
                {
                    "start": round(offset, 2),
                    "duration": round(max(0.4, len(piece) / 32000.0), 2),
                    "text": str(payload["text"]).strip(),
                }
            )
    if out:
        print(f"[ASR] groq transcribed {len(raw)/32000.0:.0f}s -> {len(out)} segments (lang={detected or '?'})")
    return out or None


def transcribe_video_section(video_path: Path, start: float, end: float) -> list[dict[str, Any]]:
    """Transcribe [start, end] of a local video -> transcript segments (absolute times).

    Works for English AND Indian languages: detects the spoken language first, then
    re-runs with that language forced so Hindi never comes back as Urdu/English.
    """
    if not FFMPEG_BIN or not video_path.exists():
        return []

    lead_in = float(os.getenv("ASR_LEAD_IN_SEC", "2.5"))
    lead_out = float(os.getenv("ASR_LEAD_OUT_SEC", "1.0"))
    base = max(0.0, float(start) - lead_in)
    dur = max(1.0, (float(end) + lead_out) - base)
    try:
        raw = subprocess.run(
            [
                FFMPEG_BIN, "-v", "quiet", "-nostdin",
                "-ss", f"{base:.2f}", "-t", f"{dur:.2f}",
                "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
                "-f", "s16le", "-",
            ],
            capture_output=True, timeout=600,
        ).stdout
    except Exception as exc:
        print(f"[ASR] audio extract failed: {exc}")
        return []
    if not raw or len(raw) < 8000:
        print("[ASR] no audio track found in this section")
        return []

    # 1) Cloud ASR first when available (far better on Hindi, zero RAM cost).
    if GROQ_API_KEY:
        cloud = _groq_transcribe_pcm(raw, WHISPER_FORCE_LANG)
        if cloud:
            shifted: list[dict[str, Any]] = []
            for seg in cloud:
                words = seg.get("words") or []
                item: dict[str, Any] = {
                    "start": round(base + seg["start"], 2),
                    "duration": seg["duration"],
                    "text": seg["text"],
                }
                if words:
                    # Word times are relative to the audio we sent -> add the same
                    # `base` offset as the segment, or every caption runs early.
                    item["words"] = [
                        {
                            "w": w["w"],
                            "s": round(base + float(w["s"]), 3),
                            "e": round(base + float(w["e"]), 3),
                        }
                        for w in words
                    ]
                shifted.append(item)
            return shifted

    try:
        import numpy as np
    except Exception as exc:
        print(f"[ASR] numpy missing, cannot use local whisper: {exc}")
        return []
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    # 2) Local fallback: which language are they speaking?
    lang, prob = _detect_speech_language(audio)

    # 2) Transcribe with the right model + language, using accuracy-first settings.
    model = _whisper_model(_model_for_lang(lang))
    if model is None:
        return []
    if lang and prob < 0.45:
        print(f"[ASR] low confidence on '{lang}' ({prob:.2f}) — keeping it anyway")

    kwargs: dict[str, Any] = {
        "vad_filter": True,
        "beam_size": 5,
        "word_timestamps": True,
        "condition_on_previous_text": False,  # stops runaway hallucination loops
        "temperature": [0.0, 0.2],
        "no_speech_threshold": 0.6,
        "compression_ratio_threshold": 2.4,
        "task": "transcribe",  # never translate Indian languages into English
    }
    if lang:
        kwargs["language"] = lang
        # No Hindi script prompt: small models echo it straight into the captions.

    try:
        segments, info = model.transcribe(audio, **kwargs)
        out: list[dict[str, Any]] = []
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            item: dict[str, Any] = {
                "start": round(base + float(seg.start), 2),
                "duration": round(max(0.4, float(seg.end) - float(seg.start)), 2),
                "text": text,
            }
            seg_words = []
            for w in (getattr(seg, "words", None) or []):
                w_text = (getattr(w, "word", "") or "").strip()
                if not w_text:
                    continue
                seg_words.append(
                    {
                        "w": w_text,
                        "s": round(base + float(getattr(w, "start", seg.start)), 3),
                        "e": round(base + float(getattr(w, "end", seg.end)), 3),
                    }
                )
            if seg_words:
                item["words"] = seg_words
            out.append(item)
        used = getattr(info, "language", lang) or lang
        print(f"[ASR] transcribed {dur:.0f}s -> {len(out)} segments (lang={used}, model={_model_for_lang(lang)})")
        return out
    except Exception as exc:
        print(f"[ASR] transcribe failed: {exc}")
        return []
    finally:
        if not WHISPER_KEEP_LOADED:
            _unload_whisper()

def probe_local_duration(video_path: Path) -> float:
    """Duration via header scan, then fall back to a real decode if the header is missing."""
    if not FFMPEG_BIN or not video_path.exists():
        return 0.0
    try:
        res = subprocess.run(
            [FFMPEG_BIN, "-i", str(video_path)], capture_output=True, text=True, timeout=60
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr or "")
        if m:
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            if dur > 0:
                return round(dur, 2)
    except Exception as exc:
        print(f"[PROBE] header scan failed: {exc}")

    # Header missing (common with some MKV/WebM streams) -> decode and read the last pts.
    try:
        res = subprocess.run(
            [
                FFMPEG_BIN, "-i", str(video_path), "-map", "0:v:0?",
                "-f", "null", "-benchmark", "-",
            ],
            capture_output=True, text=True, timeout=300,
        )
        text = (res.stderr or "")
        matches = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if matches:
            h, mm, ss = matches[-1]
            dur = _safe_float(h) * 3600 + _safe_float(mm) * 60 + _safe_float(ss)
            if dur > 0:
                return round(dur, 2)
    except Exception as exc:
        print(f"[PROBE] decode scan failed: {exc}")
    return 0.0


def _even_suggestions(
    duration: float, clip_len: int, max_clips: int, title: str
) -> list[dict[str, Any]]:
    """Last-resort clip plan: evenly spaced windows across the video."""
    seg = float(min(clip_len, max(5, duration)))
    step = max(seg, (duration - seg) / max(1, max_clips - 1)) if duration > seg else seg
    out: list[dict[str, Any]] = []
    t = 0.0
    while len(out) < max_clips and t + 3 <= duration:
        end = min(duration, t + seg)
        out.append(
            {
                "start": round(t, 2),
                "end": round(end, 2),
                "score": 78,
                "title": f"Highlight from {title}"[:80],
                "description": f"A {int(end - t)}s highlight picked from {title}."[:200],
                "hashtags": ["#Shorts", "#ViralClips", "#Reels", "#Trending"],
                "reason": "evenly spaced pick",
            }
        )
        t += step
    return out


def detect_uploaded_video_moments(
    video_path: Path,
    duration: float,
    clip_len: int,
    max_clips: int,
    video_title: str,
) -> list[dict[str, Any]]:
    """Detect high-energy moments on uploaded videos using FFmpeg audio loudness peaks (works 100% offline/cloud!)."""
    if duration <= 3:
        return []
    window_len = min(float(clip_len), max(5.0, duration))
    bucket_sec = max(2, min(5, int(duration // 20) or 2))
    num_buckets = max(1, min(180, int(math.ceil(duration / bucket_sec))))

    energy: list[float] = []
    if FFMPEG_BIN:
        # Analyze at most the first ~25 minutes, mono 8 kHz -> fast even on 0.1 CPU.
        analyze_cap = min(float(duration), 25 * 60.0)
        try:
            res = subprocess.run(
                [
                FFMPEG_BIN, "-i", str(video_path), "-vn",
                "-t", f"{analyze_cap:.1f}",
                "-ac", "1", "-ar", "8000",
                "-af", "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
                "-f", "null", "-",
            ],
                capture_output=True, text=True, timeout=240,
            )
            raw = (res.stdout or "") + "\n" + (res.stderr or "")
        except Exception as exc:
            # Video without an audio track, or the scan timed out -> synthetic energy curve.
            print(f"[ANALYZE] audio scan skipped ({exc}); using adaptive fallback")
            raw = ""
        buckets: list[list[float]] = [[] for _ in range(num_buckets)]
        cur_t = 0.0
        try:
            for line in raw.splitlines():
                mt = re.search(r"pts_time:([-0-9.]+)", line)
                if mt:
                    cur_t = _safe_float(mt.group(1), cur_t)
                mv = re.search(r"RMS_level=([-0-9.]+)", line)
                if mv:
                    db = _safe_float(mv.group(1), -91.0)
                    norm = max(0.05, min(1.0, (db + 55.0) / 48.0))
                    idx = min(num_buckets - 1, max(0, int(cur_t // bucket_sec)))
                    buckets[idx].append(norm)
        except Exception as exc:
            print(f"[ANALYZE] metric parse skipped ({exc}); using what we have")
        if any(buckets):
            energy = [sum(b) / len(b) if b else 0.45 for b in buckets]

    if not energy:
        energy = [0.6 + 0.35 * abs(math.sin((i + 1) * 1.1)) for i in range(num_buckets)]

    step = max(3.0, window_len / 3.0)
    candidates: list[dict[str, Any]] = []
    t = 0.0
    while t < duration:
        start = round(t, 2)
        end = round(min(duration, start + window_len), 2)
        if end - start < min(5.0, duration):
            break
        bs = min(len(energy) - 1, max(0, int(start // bucket_sec)))
        be = min(len(energy) - 1, max(bs, int(end // bucket_sec)))
        win = energy[bs : be + 1]
        avg_e = sum(win) / max(1, len(win))
        peak_e = max(win) if win else avg_e
        score = max(68, min(99, int(round(58 + (0.55 * avg_e + 0.45 * peak_e) * 40))))
        candidates.append({
            "start": start,
            "end": end,
            "duration": round(end - start, 1),
            "score": score,
            "reason": f"Peak audio energy ({int(peak_e * 100)}%) · High engagement segment",
        })
        if end >= duration:
            break
        t += step

    candidates.sort(key=lambda c: c["score"], reverse=True)
    selected: list[dict[str, Any]] = []
    for cand in candidates:
        overlaps = any(
            not (cand["end"] <= s["start"] + 1.5 or cand["start"] >= s["end"] - 1.5)
            for s in selected
        )
        if not overlaps:
            selected.append(cand)
        if len(selected) >= max_clips:
            break

    selected.sort(key=lambda c: c["score"], reverse=True)
    for idx, item in enumerate(selected, start=1):
        meta = generate_clip_metadata(
            video_title=video_title,
            snippet_text=f"High-energy highlight #{idx} from {video_title}",
            rank=idx,
            duration_sec=item["duration"],
            lang="en",
        )
        item["rank"] = idx
        item["title"] = meta["viral_title"]
        item["description"] = meta["description"]
        item["hashtags"] = meta["hashtags"]
    return selected


def _guess_lang(transcript: list[dict[str, Any]]) -> str:
    """Cheap language guess: Devanagari characters => Hindi."""
    sample = " ".join(str(s.get("text", "")) for s in transcript[:250])
    if re.search(r"[\u0900-\u097F]", sample):
        return "hi"
    return "en"


SUBS_TS_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def parse_subtitles(raw: str) -> list[dict[str, Any]]:
    """Parse .srt / .vtt text into [{'start','duration','text'}] segments."""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    segs: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        m = SUBS_TS_RE.search(lines[i])
        if not m:
            i += 1
            continue
        start = (
            int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            + int(m.group(4).ljust(3, "0")) / 1000
        )
        end = (
            int(m.group(5)) * 3600 + int(m.group(6)) * 60 + int(m.group(7))
            + int(m.group(8).ljust(3, "0")) / 1000
        )
        i += 1
        buf: list[str] = []
        while i < len(lines) and lines[i].strip() and not SUBS_TS_RE.search(lines[i]):
            txt = re.sub(r"<[^>]+>", "", lines[i])
            txt = re.sub(r"\{[^}]*\}", "", txt).strip()
            if txt:
                buf.append(txt)
            i += 1
        text = " ".join(buf).strip()
        if text and end > start:
            segs.append({"start": round(start, 3), "duration": round(end - start, 3), "text": text})
    return segs


def cut_uploaded_section(
    source_path: Path,
    video_id: str,
    start: float,
    end: float,
    transcript: list[dict[str, Any]] | None = None,
    with_captions: bool = True,
    caption_theme: str = "gold",
    progress: Any = None,
) -> tuple[Path, bool, str | None]:
    """Cuts a clip from an UPLOADED file and can burn captions from an uploaded .srt/.vtt."""
    if not FFMPEG_BIN or not source_path.exists():
        raise RuntimeError("Uploaded source video not found on server.")

    def _p(msg: str) -> None:
        if callable(progress):
            try:
                progress(msg)
            except Exception:
                pass

    start_i = max(0, int(math.floor(start)))
    end_i = max(start_i + 3, int(math.ceil(end)))
    transcript = transcript or []
    want_cc = bool(with_captions and CAPTIONS and transcript)
    theme_slug = caption_theme if caption_theme in CAPTION_THEMES else "gold"
    tag = f"{video_id}_{start_i}_{end_i}_{MAX_HEIGHT}p_{theme_slug}_v3"
    raw_path = CLIPS_DIR / f"{tag}_raw.mp4"
    final_path = CLIPS_DIR / (f"{tag}_cc.mp4" if want_cc else f"{tag}.mp4")
    if final_path.exists() and final_path.stat().st_size > 10_000:
        print(f"[CLIP] Serving cached clip: {final_path.name}")
        return final_path, want_cc, None

    ass_path = CLIPS_DIR / f"{tag}.ass"
    has_ass, used_font_file = (
        build_synced_ass(transcript, start, end, ass_path, theme_key=theme_slug)
        if want_cc
        else (False, None)
    )

    _p("Cutting section from your uploaded video...")
    subprocess.run(
        [
            FFMPEG_BIN, "-y",
            "-ss", f"{max(0.0, start - 0.3):.2f}",
            "-i", str(source_path),
            "-t", f"{max(2.0, end - start) + 0.6:.2f}",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(raw_path),
        ],
        capture_output=True, timeout=150, check=True,
    )

    _p(f"Encoding {MAX_HEIGHT}p 30fps" + (" + burning captions" if has_ass else "") + "...")
    caption_note: str | None = None
    if has_ass:
        ok, _err = encode_smooth_clip(raw_path, final_path, ass_path=ass_path, font_file=used_font_file)
        ass_path.unlink(missing_ok=True)
        if ok:
            raw_path.unlink(missing_ok=True)
            return final_path, True, None
        caption_note = "Clip saved without captions (FFmpeg caption burn failed)."
    else:
        ass_path.unlink(missing_ok=True)

    plain_path = CLIPS_DIR / f"{tag}.mp4"
    ok2, err2 = encode_smooth_clip(raw_path, plain_path)
    raw_path.unlink(missing_ok=True)
    if not ok2:
        raise RuntimeError(f"Failed encoding uploaded clip: {err2[-200:]}")
    return plain_path, False, caption_note


def _safe_float(value: Any, default: float = 0.0) -> float:
    """float() that never explodes on FFmpeg junk like '.', '-', 'N/A'."""
    try:
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except (TypeError, ValueError):
        return default


def _free_mb() -> float:
    try:
        return shutil.disk_usage(str(DATA_DIR)).free / (1024 * 1024)
    except Exception:
        return -1.0


def _make_room(need_mb: float) -> bool:
    """Delete old clips/uploads until `need_mb` fits (Render's disk is tiny)."""
    need_mb = max(50.0, min(float(need_mb), 4096.0))
    keep = 250.0
    if _free_mb() < 0:
        return True
    if _free_mb() >= need_mb + keep // 2:
        return True
    candidates = sorted(
        [p for p in list(CLIPS_DIR.glob("*.mp4")) + list(UPLOADS_DIR.glob("*")) if p.is_file()],
        key=lambda p: p.stat().st_mtime,
    )
    for path in candidates:
        if _free_mb() >= need_mb + 60:
            return True
        try:
            path.unlink(missing_ok=True)
            print(f"[DISK] Removed old file to make room: {path.name}")
        except Exception:
            pass
    return _free_mb() >= need_mb + 30


# ---------------- CHUNKED (RESUMABLE) UPLOAD FOR BIG FILES ----------------
# A single 1 GB POST dies on free hosting, so the browser sends 8 MB slices and
# we append them to one temp file. Same job pipeline afterwards.
CHUNK_SIZE = 8 * 1024 * 1024
CHUNK_DIR = UPLOADS_DIR / "_parts"
CHUNK_LOCK = threading.Lock()


class UploadInitRequest(BaseModel):
    filename: str
    size: int
    clip_length: int = 30
    max_clips: int = 5


@app.post("/api/upload/init")
def upload_init(body: UploadInitRequest, user: dict[str, Any] = Depends(get_current_user)):
    ext = Path(body.filename or "video.mp4").suffix.lower()
    if ext not in (".mp4", ".mov", ".webm", ".mkv", ".m4v"):
        raise HTTPException(status_code=400, detail="Please upload an MP4, MOV, WebM, or MKV video file.")
    if body.size > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"That file is {body.size / 1048576:.0f} MB but the limit is {MAX_UPLOAD_MB} MB.",
        )
    needed_mb = body.size / 1048576 * 1.35 + 200  # upload + clip outputs headroom
    if not _make_room(needed_mb):
        raise HTTPException(
            status_code=507,
            detail=(
                f"Not enough free disk on the server for a {body.size / 1048576:.0f} MB video "
                f"({_free_mb():.0f} MB free). Open My Clips and delete a few old clips, then retry."
            ),
        )
    CHUNK_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = secrets.token_hex(10)
    part = CHUNK_DIR / f"{upload_id}.part"
    part.touch()
    meta = {
        "filename": body.filename,
        "ext": ext,
        "size": body.size,
        "clip_length": body.clip_length if body.clip_length in (30, 45, 60) else 30,
        "max_clips": body.max_clips if body.max_clips in (5, 10, 20) else 5,
        "received": 0,
        "user_id": user["id"],
    }
    (CHUNK_DIR / f"{upload_id}.json").write_text(json.dumps(meta))
    print(f"[UPLOAD] chunked upload started {upload_id} ({body.size/1048576:.0f} MB)")
    return {
        "upload_id": upload_id,
        "chunk_size": CHUNK_SIZE,
        "max_upload_mb": MAX_UPLOAD_MB,
        "free_mb": round(_free_mb()),
    }


@app.put("/api/upload/chunk/{upload_id}")
async def upload_chunk(
    upload_id: str,
    index: int = 0,
    request: Request = None,
    user: dict[str, Any] = Depends(get_current_user),
):
    meta_path = CHUNK_DIR / f"{upload_id}.json"
    part_path = CHUNK_DIR / f"{upload_id}.part"
    if not meta_path.exists() or not part_path.exists():
        raise HTTPException(status_code=404, detail="Upload session expired. Please pick the file again.")
    meta = json.loads(meta_path.read_text())
    if meta.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not your upload.")

    with CHUNK_LOCK:
        received = part_path.stat().st_size
        expected_offset = index * CHUNK_SIZE
        if expected_offset > received:
            raise HTTPException(
                status_code=409,
                detail=f"Chunk {index} arrived out of order (server has {received} bytes).",
            )
        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail="Empty chunk.")
        if expected_offset + len(body) > MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"File exceeds the {MAX_UPLOAD_MB} MB limit.")
        with part_path.open("r+b") as f:
            f.seek(expected_offset)
            f.truncate(expected_offset)
            f.write(body)
        received = part_path.stat().st_size
        meta["received"] = received
        meta_path.write_text(json.dumps(meta))

    return {"received": received, "total": meta["size"], "percent": round(received * 100 / max(1, meta["size"]), 1)}


class UploadFinishRequest(BaseModel):
    srt_text: str | None = None


@app.post("/api/upload/finish/{upload_id}")
def upload_finish(
    upload_id: str,
    body: UploadFinishRequest | None = None,
    user: dict[str, Any] = Depends(get_current_user),
):
    meta_path = CHUNK_DIR / f"{upload_id}.json"
    part_path = CHUNK_DIR / f"{upload_id}.part"
    if not meta_path.exists() or not part_path.exists():
        raise HTTPException(status_code=404, detail="Upload session expired. Please pick the file again.")
    meta = json.loads(meta_path.read_text())
    if meta.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Not your upload.")

    received = part_path.stat().st_size
    if received < meta["size"] * 0.98:
        raise HTTPException(
            status_code=400,
            detail=f"Upload incomplete ({received/1048576:.0f} of {meta['size']/1048576:.0f} MB). Please retry.",
        )

    vid_id = f"up_{secrets.token_hex(5)}"
    saved_path = UPLOADS_DIR / f"{vid_id}{meta['ext']}"
    part_path.replace(saved_path)
    meta_path.unlink(missing_ok=True)

    transcript: list[dict[str, Any]] = []
    srt_text = (body.srt_text if body else None) or ""
    if srt_text.strip():
        try:
            transcript = parse_subtitles(srt_text)
            print(f"[UPLOAD] Parsed {len(transcript)} subtitle segments (chunked upload)")
        except Exception as exc:
            print(f"[UPLOAD] Subtitle parse failed: {exc}")

    job_id = _job_create(kind="upload")
    threading.Thread(
        target=_run_upload_job,
        args=(
            job_id, user, saved_path, meta["filename"],
            meta["clip_length"], meta["max_clips"], transcript,
        ),
        daemon=True,
    ).start()
    print(f"[UPLOAD] chunked upload complete: {saved_path.name} ({received/1048576:.0f} MB)")
    return {"job_id": job_id, "status": "queued", "message": "Upload complete — analysing audio..."}


@app.post("/api/upload")
async def upload_video(
    file: UploadFile = File(...),
    subs: UploadFile | None = File(None),
    clip_length: int = Form(30),
    max_clips: int = Form(5),
    sync: int = 0,
    job: int = 0,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Upload any MP4/MOV/WebM/MKV directly — works 100% on Render with ZERO YouTube IP blocks!"""
    orig_name = file.filename or "uploaded_video.mp4"
    ext = Path(orig_name).suffix.lower()
    if ext not in (".mp4", ".mov", ".webm", ".mkv", ".m4v"):
        raise HTTPException(status_code=400, detail="Please upload an MP4, MOV, WebM, or MKV video file.")

    clip_length = clip_length if clip_length in (30, 45, 60) else 30
    max_clips = max_clips if max_clips in (5, 10, 20) else 5

    if not _make_room(300.0):
        raise HTTPException(
            status_code=507,
            detail="The server is out of free disk space. Delete a few saved clips/projects and try again.",
        )

    vid_id = f"up_{secrets.token_hex(5)}"
    saved_path = UPLOADS_DIR / f"{vid_id}{ext}"
    total_bytes = 0
    with saved_path.open("wb") as out_f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_UPLOAD_MB * 1024 * 1024:
                out_f.close()
                saved_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"That file is bigger than {MAX_UPLOAD_MB} MB, which the free server can't accept. "
                        "Fix: download it in 360p/720p (y2mate lets you pick), or cut it into a shorter "
                        "piece first — clips are only 30-60s anyway."
                    ),
                )
            out_f.write(chunk)

    # Optional .srt / .vtt subtitles -> real synced captions for uploaded videos!
    transcript: list[dict[str, Any]] = []
    if subs is not None and subs.filename:
        try:
            raw_subs = (await subs.read()).decode("utf-8", errors="ignore")
            transcript = parse_subtitles(raw_subs)
            print(f"[UPLOAD] Parsed {len(transcript)} subtitle segments from {subs.filename}")
        except Exception as exc:
            print(f"[UPLOAD] Subtitle parse failed: {exc}")


    # Non-job callers (older frontends) get the classic immediate response.
    if sync or not job:
        try:
            project = _perform_upload(
                user, saved_path, orig_name, clip_length, max_clips, transcript
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"project": project}

    # Heavy audio analysis runs in a BACKGROUND JOB (0.1 CPU can take a while).
    job_id = _job_create(kind="upload")
    threading.Thread(
        target=_run_upload_job,
        args=(job_id, user, saved_path, orig_name, clip_length, max_clips, transcript),
        daemon=True,
    ).start()
    return {
        "job_id": job_id,
        "status": "queued",
        "message": "Upload received — analysing audio for viral moments...",
        "poll_url": f"/api/clips/job/{job_id}",
    }


@app.post("/api/demo")
def create_demo(user: dict[str, Any] = Depends(get_current_user)):
    project = create_demo_project_for_user(user["id"])
    return {"project": project}


# ---------- CUT & SAVE REAL CLIP (ONLY THESE APPEAR IN "MY CLIPS") ----------
# Cuts run as a BACKGROUND JOB so the browser request can never hang while a
# 1080p clip encodes. (Render Free = 0.1 CPU, a real clip can take 1-4 minutes.)
CUT_JOBS: dict[str, dict[str, Any]] = {}
CUT_JOBS_LOCK = threading.Lock()
CUT_JOB_TTL = 60 * 60  # keep finished jobs around for 1 hour


def _job_create(kind: str = "cut", message: str = "Queued...") -> str:
    job_id = secrets.token_hex(8)
    now = time.time()
    with CUT_JOBS_LOCK:
        for stale in [
            k for k, v in CUT_JOBS.items() if now - v.get("updated_at", now) > CUT_JOB_TTL
        ]:
            CUT_JOBS.pop(stale, None)
        CUT_JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "message": message,
            "started_at": now,
            "updated_at": now,
            "clip": None,
            "project": None,
            "note": None,
            "error": None,
        }
    return job_id


def _job_update(job_id: str, **fields: Any) -> None:
    with CUT_JOBS_LOCK:
        job = CUT_JOBS.get(job_id)
        if job:
            job.update(fields)
            job["updated_at"] = time.time()


def _needs_word_upgrade(transcript: list[dict[str, Any]], start: float, end: float) -> bool:
    """True when the cached transcript has no word timings for this window.

    Older projects were saved before word timings existed; without them captions
    fall back to estimated spacing and appear too early/late.
    """
    if not transcript:
        return False
    overlapping = []
    for seg in transcript:
        try:
            s_start = float(seg.get("start", 0.0))
            s_end = s_start + float(seg.get("duration", 0.0))
        except Exception:
            continue
        if s_start < end and s_end > start:
            overlapping.append(seg)
    if not overlapping:
        return False
    return not any(seg.get("words") for seg in overlapping)


def _transcript_covers(transcript: list[dict[str, Any]], start: float, end: float) -> bool:
    """True if we already have subtitle lines overlapping [start, end]."""
    if not transcript:
        return False
    hits = 0
    for seg in transcript:
        try:
            s_start = float(seg.get("start", 0.0))
            s_dur = float(seg.get("duration", 0.0))
        except Exception:
            continue
        if s_start < end and (s_start + s_dur) > start:
            hits += 1
        if hits >= 2:
            return True
    return hits >= 1


def _spoken_for_range(
    transcript: list[dict[str, Any]], start: float, end: float, max_chars: int = 400
) -> str:
    """Join the spoken words that fall inside [start, end]."""
    if not transcript:
        return ""
    parts: list[str] = []
    for seg in transcript:
        try:
            s_start = float(seg.get("start", 0.0))
        except Exception:
            continue
        if start - 0.5 <= s_start <= end + 0.5:
            text = str(seg.get("text", "")).strip()
            if text:
                parts.append(text)
        if sum(len(p) for p in parts) > max_chars:
            break
    return re.sub(r"\s+", " ", " ".join(parts))[:max_chars].strip()


def _perform_cut(
    body: "CutClipRequest",
    user: dict[str, Any],
    proj: Any,
    progress: Any = None,
) -> tuple[dict[str, Any], str | None]:
    transcript = json.loads(proj["transcript_json"])
    try:
        if proj["video_id"] == "demo_viralcut":
            out_path, has_cc, note = cut_demo_section(
                body.start,
                body.end,
                transcript,
                with_captions=body.captions,
                caption_theme=body.caption_theme,
            )
        elif str(proj["url"]).startswith("upload://"):
            fname = str(proj["url"]).replace("upload://", "", 1)
            local_src = UPLOADS_DIR / Path(fname).name
            # Captions ON but no .srt uploaded? Transcribe the clip's own audio.
            # Also re-do older transcripts that have no word timings (they would
            # otherwise burn captions with estimated spacing = early captions).
            need_asr = (not _transcript_covers(transcript, body.start, body.end)) or _needs_word_upgrade(
                transcript, body.start, body.end
            )
            if need_asr and (GROQ_API_KEY or _whisper_available()):
                if callable(progress):
                    progress("Syncing captions to the voice...")
                extra = transcribe_video_section(local_src, body.start, body.end)
                if extra:
                    pad = 3.0
                    kept = [
                        s for s in (transcript or [])
                        if not (
                            float(s.get("start", 0.0)) + float(s.get("duration", 0.0)) > body.start - pad
                            and float(s.get("start", 0.0)) < body.end + pad
                        )
                    ]
                    transcript = sorted(kept + extra, key=lambda s: float(s.get("start", 0.0)))
                    try:
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE projects SET transcript_json = ? WHERE id = ?",
                                (json.dumps(transcript), proj["id"]),
                            )
                        print(f"[ASR] transcript upgraded with word timings for project {proj['id']}")
                    except Exception as exc:
                        print(f"[ASR] could not save upgraded transcript: {exc}")
            out_path, has_cc, note = cut_uploaded_section(
                local_src,
                proj["video_id"],
                body.start,
                body.end,
                transcript=transcript,
                with_captions=body.captions,
                caption_theme=body.caption_theme,
                progress=progress,
            )
        else:
            out_path, has_cc, note = cut_youtube_section(
                proj["url"],
                proj["video_id"],
                body.start,
                body.end,
                transcript,
                with_captions=body.captions,
                caption_theme=body.caption_theme,
                progress=progress,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise RuntimeError(str(exc))

    # ---- Rebuild Title/Description/Hashtags from THIS clip's own speech ----
    try:
        if bool(getattr(body, "auto_text", True)):
            spoken = _spoken_for_range(transcript, body.start, body.end)
            if spoken:
                meta = build_viral_tdh(
                    topic=proj["title"] or "",
                    spoken_text=spoken,
                    rank=1,
                    duration_sec=max(1.0, body.end - body.start),
                    lang=proj["language"] or "en",
                )
                body.title = meta["viral_title"]
                body.description = meta["description"]
                body.hashtags = meta["hashtags"]
                print(f"[TDH] clip text from speech: {body.title[:70]}")
    except Exception as exc:
        print(f"[TDH] rewrite skipped: {exc}")

    now = datetime.now(timezone.utc).isoformat()
    quality = f"{MAX_HEIGHT}p"
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO clips (user_id, project_id, video_id, title, description, hashtags_json, start_time, end_time, score, filename, has_captions, quality, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                proj["id"],
                proj["video_id"],
                body.title,
                body.description,
                json.dumps(body.hashtags or []),
                body.start,
                body.end,
                body.score,
                out_path.name,
                1 if has_cc else 0,
                quality,
                now,
            ),
        )
        clip_id = cur.lastrowid

    # Keep the MP4 in Postgres too (files vanish on every Render restart).
    try:
        cap_mb = float(os.getenv("CLIP_DB_MAX_MB", "12"))
        if out_path.exists() and out_path.stat().st_size <= cap_mb * 1024 * 1024:
            import base64 as _b64

            payload = _b64.b64encode(out_path.read_bytes()).decode("ascii")
            with get_db() as conn:
                conn.execute("UPDATE clips SET video_b64 = ? WHERE id = ?", (payload, clip_id))
                keep = int(os.getenv("CLIP_DB_KEEP", "20"))
                conn.execute(
                    "UPDATE clips SET video_b64 = NULL WHERE video_b64 IS NOT NULL AND id NOT IN "
                    "(SELECT id FROM clips WHERE video_b64 IS NOT NULL ORDER BY id DESC LIMIT ?)",
                    (keep,),
                )
            print(f"[CLIP] stored in DB for durability ({out_path.stat().st_size/1048576:.1f} MB)")
        else:
            print("[CLIP] too big for DB storage — file only")
    except Exception as exc:
        print(f"[CLIP] db storage skipped: {exc}")

    return (
        {
            "id": clip_id,
            "project_id": proj["id"],
            "video_id": proj["video_id"],
            "title": body.title,
            "description": body.description,
            "hashtags": body.hashtags or [],
            "start": body.start,
            "end": body.end,
            "duration": round(body.end - body.start, 1),
            "score": body.score,
            "filename": out_path.name,
            "has_captions": has_cc,
            "quality": quality,
            "video_url": f"/api/clips/file/{out_path.name}",
            "download_url": f"/api/clips/file/{out_path.name}?download=1",
            "created_at": now,
        },
        note,
    )


def _run_cut_job(job_id: str, body: "CutClipRequest", user: dict[str, Any], proj: Any) -> None:
    try:
        _job_update(job_id, status="running", message="Preparing clip...")
        clip, note = _perform_cut(
            body, user, proj, progress=lambda msg: _job_update(job_id, message=msg)
        )
        _job_update(
            job_id,
            status="done",
            message="Clip ready!",
            clip=clip,
            note=note,
            finished_at=time.time(),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[CUT JOB {job_id}] FAILED: {exc}")
        _job_update(
            job_id,
            status="error",
            message="Clip cutting failed.",
            error=str(exc) or "Unknown error while cutting the clip.",
            finished_at=time.time(),
        )


def _perform_upload(
    user: dict[str, Any],
    saved_path: Path,
    orig_name: str,
    clip_length: int,
    max_clips: int,
    transcript: list[dict[str, Any]],
    progress: Any = None,
) -> dict[str, Any]:
    """Analyse an uploaded video and create the project. Returns the project dict."""
    def _p(msg: str) -> None:
        if callable(progress):
            try:
                progress(msg)
            except Exception:
                pass

    _p("Reading video duration...")
    duration = probe_local_duration(saved_path)
    if duration <= 2:
        saved_path.unlink(missing_ok=True)
        raise RuntimeError("Could not read the video duration — is the file a valid MP4/MOV?")

    _p(f"Analysing {duration/60:.1f} min of audio for high-energy moments...")
    clean_title = clean_source_topic(Path(orig_name).stem) or "Uploaded Video"
    try:
        suggestions = detect_uploaded_video_moments(
            saved_path, duration, clip_length, max_clips, clean_title
        )
    except Exception as exc:
        # A weird codec/metadata value must never kill the upload.
        print(f"[UPLOAD] moment detection failed ({exc}); using evenly spaced clips")
        suggestions = _even_suggestions(duration, clip_length, max_clips, clean_title)
    if not suggestions:
        raise RuntimeError("Video is too short to generate clips (need at least ~5 seconds).")

    # ---- Listen to the video so every clip's Title/Description/Hashtags is real ----
    _analyse_asr = os.getenv("ANALYZE_ASR", "1").strip().lower() in ("1", "true", "yes")
    if not transcript and _analyse_asr and (_whisper_available() or GROQ_API_KEY):
        last_clip_end = max(float(s["end"]) for s in suggestions)
        cap = min(duration, last_clip_end + 8.0, float(os.getenv("ANALYZE_ASR_MAX_SEC", "1500")))
        _p(f"Listening to the audio to write accurate titles ({cap/60:.1f} min)...")
        try:
            heard = transcribe_video_section(saved_path, 0, cap)
        except Exception as exc:
            print(f"[UPLOAD] analyse-ASR failed ({exc}); titles stay template-based")
            heard = []
        if heard:
            transcript = heard
            speech_lang = _guess_lang(heard)
            for idx, sug in enumerate(suggestions, start=1):
                spoken = _spoken_for_range(heard, sug["start"], sug["end"])
                if not spoken:
                    continue
                meta_tdh = build_viral_tdh(
                    clean_title, spoken, rank=idx,
                    duration_sec=max(1.0, sug["end"] - sug["start"]), lang=speech_lang,
                    use_llm=idx <= int(os.getenv("LLM_MAX_CALLS", "10")),
                )
                sug["title"] = meta_tdh["viral_title"]
                sug["description"] = meta_tdh["description"]
                sug["hashtags"] = meta_tdh["hashtags"]
            if _is_generic_title(clean_title):
                better = _llm_video_title(_spoken_for_range(heard, 0, cap))
                if better:
                    print(f"[UPLOAD] video renamed: {clean_title!r} -> {better!r}")
                    clean_title = better
            print(f"[UPLOAD] TDH grounded in speech for {len(suggestions)} clips")
        else:
            _p("Could not hear clear speech — using topic-based titles.")

    top_score = max(s["score"] for s in suggestions)
    now = datetime.now(timezone.utc).isoformat()
    src_url = f"upload://{saved_path.name}"
    lang = "auto" if not transcript else _guess_lang(transcript)

    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO projects (user_id, video_id, url, title, thumbnail, duration, language, top_score, clip_length, max_clips, suggestions_json, transcript_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"], saved_path.stem, src_url, clean_title, None, duration, lang,
                top_score, clip_length, max_clips, json.dumps(suggestions),
                json.dumps(transcript), now,
            ),
        )
        proj_id = cur.lastrowid

    # Poster frame -> Postgres, so the card still shows a picture after a restart.
    try:
        poster = THUMBS_DIR / f"p{proj_id}.jpg"
        if _extract_poster(saved_path, poster, _poster_time_for_project({"suggestions_json": json.dumps(suggestions), "duration": duration})):
            import base64 as _b64
            b64 = _b64.b64encode(poster.read_bytes()).decode("ascii")
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO project_thumbs (project_id, jpeg_b64) VALUES (?, ?) "
                    "ON CONFLICT (project_id) DO UPDATE SET jpeg_b64 = ?",
                    (proj_id, b64, b64),
                )
                conn.execute(
                    "UPDATE projects SET thumbnail = ? WHERE id = ?",
                    (f"/api/poster/project/{proj_id}", proj_id),
                )
            print(f"[THUMB] poster saved for project {proj_id} ({len(b64)} b64 chars)")
    except Exception as exc:
        print(f"[THUMB] poster skipped: {exc}")

    thumb_url = f"/api/poster/project/{proj_id}" if (THUMBS_DIR / f"p{proj_id}.jpg").exists() else None

    return {
        "id": proj_id,
        "video_id": saved_path.stem,
        "url": src_url,
        "title": clean_title,
        "thumbnail": thumb_url,
        "duration": duration,
        "language": lang,
        "top_score": top_score,
        "clip_length": clip_length,
        "max_clips": max_clips,
        "suggestions": suggestions,
        "captions_available": bool(transcript),
        "caption_segments": len(transcript),
        "created_at": now,
    }


def _run_upload_job(
    job_id: str,
    user: dict[str, Any],
    saved_path: Path,
    orig_name: str,
    clip_length: int,
    max_clips: int,
    transcript: list[dict[str, Any]],
) -> None:
    """Analyse an uploaded video for viral moments in the background."""
    try:
        _job_update(job_id, status="running", message="Reading video duration...")
        project = _perform_upload(
            user, saved_path, orig_name, clip_length, max_clips, transcript,
            progress=lambda msg: _job_update(job_id, message=msg),
        )
        _job_update(job_id, status="done", message="Analysis ready!", project=project,
                    finished_at=time.time())
    except Exception as exc:  # noqa: BLE001
        print(f"[UPLOAD JOB {job_id}] FAILED: {exc}")
        _job_update(job_id, status="error", message="Upload analysis failed.",
                    error=str(exc) or "Unknown error while analysing the video.",
                    finished_at=time.time())
        return



@app.post("/api/clips/cut")
def cut_clip(
    body: CutClipRequest,
    sync: int = 0,
    job: int = 0,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Starts a background cut job and returns immediately with a job id (or runs sync)."""
    with get_db() as conn:
        proj = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (body.project_id, user["id"]),
        ).fetchone()
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found.")

    if sync or not job:
        try:
            clip, note = _perform_cut(body, user, dict(proj))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return {"clip": clip, "note": note}

    job_id = _job_create()
    threading.Thread(
        target=_run_cut_job,
        args=(job_id, body, user, dict(proj)),
        daemon=True,
    ).start()
    return {
        "job_id": job_id,
        "status": "queued",
        "message": "Queued...",
        "poll_url": f"/api/clips/job/{job_id}",
    }


@app.get("/api/clips/job/{job_id}")
def get_cut_job(job_id: str, user: dict[str, Any] = Depends(get_current_user)):
    """Poll this to follow a cut job: queued -> running -> done / error."""
    with CUT_JOBS_LOCK:
        job = CUT_JOBS.get(job_id)
        snapshot = dict(job) if job else None
    if not snapshot:
        raise HTTPException(
            status_code=404,
            detail="This clip job expired (the server restarted). Please click Cut & Download again.",
        )
    elapsed = round(time.time() - snapshot.get("started_at", time.time()), 1)
    snapshot["elapsed"] = elapsed
    return snapshot


@app.delete("/api/clips/{clip_id}")
def delete_clip(clip_id: int, user: dict[str, Any] = Depends(get_current_user)):
    with get_db() as conn:
        row = conn.execute(
            "SELECT filename FROM clips WHERE id = ? AND user_id = ?",
            (clip_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Clip not found.")
        conn.execute("DELETE FROM clips WHERE id = ? AND user_id = ?", (clip_id, user["id"]))
        # Remove file if no other clip references it
        other = conn.execute(
            "SELECT 1 FROM clips WHERE filename = ? LIMIT 1",
            (row["filename"],),
        ).fetchone()
        if not other:
            (CLIPS_DIR / row["filename"]).unlink(missing_ok=True)
    return {"deleted": True}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: int, user: dict[str, Any] = Depends(get_current_user)):
    with get_db() as conn:
        row = conn.execute(
            "SELECT url FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user["id"]),
        ).fetchone()
        conn.execute("DELETE FROM projects WHERE id = ? AND user_id = ?", (project_id, user["id"]))
        # Free the disk: remove the uploaded source video too (Render's disk is tiny).
        if row and str(row["url"]).startswith("upload://"):
            fname = str(row["url"]).replace("upload://", "", 1)
            still_used = conn.execute(
                "SELECT 1 FROM projects WHERE url = ? LIMIT 1", (row["url"],)
            ).fetchone()
            if not still_used:
                (UPLOADS_DIR / Path(fname).name).unlink(missing_ok=True)
    return {"deleted": True}


def _extract_poster(src: Path, out: Path, at_sec: float) -> bool:
    """Grab one JPEG frame from a video file (fast seek). True if it worked."""
    if not FFMPEG_BIN or not src.exists():
        return False
    for t in (max(0.0, at_sec), max(0.0, at_sec / 2), 0.5):
        try:
            subprocess.run(
                [
                    FFMPEG_BIN, "-v", "error", "-nostdin",
                    "-ss", f"{t:.2f}", "-i", str(src),
                    "-frames:v", "1", "-vf", "scale=480:-2",
                    "-q:v", "4", str(out), "-y",
                ],
                capture_output=True, timeout=90,
            )
        except Exception as exc:
            print(f"[THUMB] extract failed: {exc}")
            continue
        if out.exists() and out.stat().st_size > 1500:
            return True
    return False


def _placeholder_jpg() -> Path:
    """A dark 480x270 frame so a card never shows a broken image."""
    out = THUMBS_DIR / "_placeholder.jpg"
    if out.exists() and out.stat().st_size > 500:
        return out
    if FFMPEG_BIN:
        try:
            subprocess.run(
                [
                    FFMPEG_BIN, "-v", "error", "-f", "lavfi",
                    "-i", "color=c=0x14121f:s=480x270",
                    "-frames:v", "1", "-q:v", "6", str(out), "-y",
                ],
                capture_output=True, timeout=45,
            )
        except Exception:
            pass
    if not out.exists():
        # 1x1 dark JPEG
        out.write_bytes(
            bytes.fromhex(
                "ffd8ffe000104a46494600010100000100010000ffdb00430008060607060508070"
                "709070707090a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720"
                "222c231c1c28372c2c30313434341f27393d38323c2e333432ffc0000b0800010"
                "0010101220002ffc4001f00000105010101010101000000000000000001020304"
                "05060708090a0bffc40014100100000000000000000000000000000000ffda0008"
                "01010000013fffc40014100100000000000000000000000000000000ffda00080"
                "1010000013fffc40014100100000000000000000000000000000000ffda000801"
                "010002013fffc40014100100000000000000000000000000000000ffda0008010"
                "10000013fffc40014100100000000000000000000000000000000ffda00080106"
                "00000101"
            )
        )
    return out


def _project_source_file(proj: Any) -> Path | None:
    """Local file behind a project (uploads only)."""
    try:
        url = str(proj["url"] or "")
    except Exception:
        return None
    if not url.startswith("upload://"):
        return None
    fname = url.replace("upload://", "", 1)
    path = UPLOADS_DIR / Path(fname).name
    return path if path.exists() else None


def _project_thumbnail_url(proj: Any) -> str | None:
    """URL for a project's poster frame, or None when there is no local video."""
    if _project_source_file(proj) is None:
        return None
    return f"/api/poster/project/{proj['id']}"


def _poster_time_for_project(proj: Any) -> float:
    """Poster at the start of the best clip (falls back to 10% in)."""
    try:
        suggestions = json.loads(proj["suggestions_json"] or "[]")
        if suggestions:
            return max(0.0, float(suggestions[0]["start"]) + 1.5)
    except Exception:
        pass
    try:
        return max(0.5, float(proj["duration"] or 0) * 0.1)
    except Exception:
        return 1.0


@app.get("/api/poster/project/{project_id}")
def project_poster(project_id: int):
    """JPEG poster frame for a project card (cached on disk)."""
    from fastapi.responses import FileResponse

    import base64 as _b64
    from fastapi import Response

    out = THUMBS_DIR / f"p{project_id}.jpg"
    # 1) Stored in the database -> survives restarts and disk wipes.
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT jpeg_b64 FROM project_thumbs WHERE project_id = ?", (project_id,)
            ).fetchone()
        if row and row["jpeg_b64"]:
            return Response(content=_b64.b64decode(row["jpeg_b64"]), media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})
    except Exception as exc:
        print(f"[THUMB] db lookup failed: {exc}")

    # 2) Already on disk.
    if out.exists() and out.stat().st_size > 1500:
        return FileResponse(out, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, url, duration, suggestions_json FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
    src = _project_source_file(row) if row else None
    if src is None:
        return FileResponse(_placeholder_jpg(), media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=600"})
    at = _poster_time_for_project(row)
    if _extract_poster(src, out, at):
        return FileResponse(out, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})
    print(f"[THUMB] could not extract poster for project {project_id}")
    return FileResponse(_placeholder_jpg(), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=600"})


@app.get("/api/clips/file/{filename}")
def serve_clip_file(filename: str, download: int = 0):
    safe_name = Path(filename).name
    path = CLIPS_DIR / safe_name
    if not path.exists():
        # Render wiped the disk -> hand back the copy stored in Postgres.
        import base64 as _b64

        from fastapi import Response

        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT video_b64 FROM clips WHERE filename = ? AND video_b64 IS NOT NULL LIMIT 1",
                    (safe_name,),
                ).fetchone()
        except Exception as exc:
            print(f"[CLIP] db read failed: {exc}")
            row = None
        if row and row["video_b64"]:
            headers = {}
            if download:
                headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
            return Response(content=_b64.b64decode(row["video_b64"]), media_type="video/mp4",
                            headers=headers)
        raise HTTPException(status_code=404, detail="Clip file not found.")
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    return FileResponse(path, media_type="video/mp4", headers=headers)


# ---------- SERVE BUILT REACT FRONTEND IN PRODUCTION ----------
if (FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")


@app.get("/")
def serve_root():
    index_file = FRONTEND_DIST / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return health()


@app.get("/{full_path:path}")
def serve_spa_fallback(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API endpoint not found.")
    candidate = FRONTEND_DIST / full_path
    if candidate.is_file():
        return FileResponse(candidate)
    index_file = FRONTEND_DIST / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    raise HTTPException(status_code=404, detail="Not found.")

