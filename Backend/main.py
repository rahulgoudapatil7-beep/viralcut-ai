import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import smtplib
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import imageio_ffmpeg
import yt_dlp
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi

# ============================================================
# SETTINGS (Easy to edit!)
# ============================================================
MAX_HEIGHT = 1080       # 1080 (best compatibility) or 1440
CAPTIONS = True         # Set False to skip burning captions
CLIP_DURATION = 30      # Default clip length in seconds
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days

# Add your admin email(s) here (the very first registered account is also auto-admin!)
ADMIN_EMAILS = {"rahulgoudapatil7@gmail.com"}

# REAL GMAIL EMAIL SENDING FOR FORGOT PASSWORD (100% secure — code is NEVER shown on the website!)
# Create a 16-character Google App Password at: https://myaccount.google.com/apppasswords
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "rahulgoudapatil7@gmail.com")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")  # Paste your 16-char Gmail App Password here (no spaces)

BASE_DIR = Path(__file__).resolve().parent
CLIPS_DIR = BASE_DIR / "clips"
FFMPEG_DIR = BASE_DIR / "ffmpeg_bin"
DB_PATH = BASE_DIR / "viralcut.db"
SECRET_PATH = BASE_DIR / ".auth_secret"

CLIPS_DIR.mkdir(parents=True, exist_ok=True)
FFMPEG_DIR.mkdir(parents=True, exist_ok=True)


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
    if SECRET_PATH.exists():
        return SECRET_PATH.read_bytes()
    key = secrets.token_bytes(32)
    SECRET_PATH.write_bytes(key)
    return key


SECRET_KEY = get_secret_key()


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
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
            "SELECT id, name, email, is_admin, created_at FROM users WHERE id = ?",
            (payload["uid"],),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="User account not found.")
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


def get_transcript(video_id: str) -> tuple[list[dict[str, Any]], str]:
    """Fetch English transcript if available, otherwise fall back to any available language (e.g., Hindi)."""
    api = YouTubeTranscriptApi()
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
            start = float(s.get("start", 0.0))
            dur = float(s.get("duration", 2.0))
        else:
            text = str(getattr(s, "text", "")).strip()
            start = float(getattr(s, "start", 0.0))
            dur = float(getattr(s, "duration", 2.0))
        if text:
            segments.append({"text": text, "start": round(start, 2), "duration": round(dur, 2)})
    return segments, lang


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


def generate_clip_metadata(
    video_title: str,
    snippet_text: str,
    rank: int,
    duration_sec: float,
    lang: str = "en",
) -> dict[str, Any]:
    """Generate a ready-to-post viral Title, Description, and Hashtags for every clip."""
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

    raw_items: list[tuple[float, float, str]] = []
    full_sample = []
    for seg in transcript:
        seg_start = float(seg["start"])
        seg_end = seg_start + max(0.5, float(seg.get("duration", 2.0)))
        if seg_end <= clip_start or seg_start >= clip_end:
            continue
        rel_start = max(0.0, seg_start - clip_start)
        rel_end = min(clip_dur, seg_end - clip_start)
        if rel_end - rel_start < 0.25:
            continue
        text = re.sub(r"\s+", " ", str(seg["text"])).strip()
        if not text:
            continue
        raw_items.append((rel_start, rel_end, text))
        full_sample.append(text)

    if not raw_items:
        return False, None

    use_indic = has_indic_chars(" ".join(full_sample))
    font_file, font_name = FONTS["indic"] if use_indic else FONTS["latin"]
    font_size = 34 if not use_indic else 36  # Compact, sleek creator sizing on 1920x1080 canvas

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
Style: Premium,{font_name},{font_size},{theme['primary']},{theme['accent']},{theme['outline']},{theme['shadow']},-1,0,0,0,100,100,0,0,1,2.0,1.4,2,90,90,54,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    cues: list[list[Any]] = []
    max_words_per_chunk = 5

    for rel_start, rel_end, text in raw_items:
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
        "-preset", "veryfast",
        "-crf", "21",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-af", "aresample=async=1:first_pts=0",
        "-avoid_negative_ts", "make_zero",
        "-shortest",
        "-movflags", "+faststart",
        str(out_video),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode == 0 and out_video.exists() and out_video.stat().st_size > 10_000:
        return True, ""
    err = (res.stderr or "")[-500:]
    if ass_path:
        print(f"CAPTION BURN FAILED: {err}")
    return False, err


def get_ytdlp_js_opts() -> dict[str, Any]:
    """Enable Node.js for yt-dlp if installed so YouTube 1080p formats work cleanly."""
    if shutil.which("node"):
        return {"js_runtimes": {"node": {}}}
    return {}


def cut_youtube_section(
    url: str,
    video_id: str,
    start: float,
    end: float,
    transcript: list[dict[str, Any]],
    with_captions: bool = True,
    caption_theme: str = "gold",
) -> tuple[Path, bool, str | None]:
    """
    Zero-lag 2-stage 1080p clip pipeline:
    Stage 1: Fast stream-copy (-c copy) downloads ONLY the 30s section to local disk (~4-8s, no network stall!).
    Stage 2: Re-encode locally at smooth CFR 30fps + yuv420p + Premium styled ASS captions (~6-12s).
    """
    if not FFMPEG_BIN:
        raise RuntimeError("FFmpeg is not ready on this machine. Check FFMPEG CHECK in backend terminal.")

    start_i = max(0, int(math.floor(start)))
    end_i = max(start_i + 5, int(math.ceil(end)))
    clip_dur = max(3.0, end - start)
    theme_slug = caption_theme if caption_theme in CAPTION_THEMES else "gold"
    want_cc = bool(with_captions and CAPTIONS and transcript)
    tag = f"{video_id}_{start_i}_{end_i}_{MAX_HEIGHT}p_{theme_slug}_v3"
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

    fmt = (
        f"bestvideo[height<={MAX_HEIGHT}][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={MAX_HEIGHT}][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[height<={MAX_HEIGHT}][ext=mp4]/best[height<={MAX_HEIGHT}]/best"
    )

    # ---- STAGE 1: Fast raw download to local disk (NO encoding while reading HTTP!) ----
    print(f"[CLIP] Stage 1/2: Fast-fetching {clip_dur:.0f}s section ({start_i}s-{end_i}s, {MAX_HEIGHT}p)...")
    if raw_path.exists():
        raw_path.unlink(missing_ok=True)

    downloaded_raw = False
    try:
        info_opts = {
            "format": fmt,
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

        if v_url:
            ua = hdrs.get("User-Agent", "Mozilla/5.0")
            # Grab 2s extra padding on copy so final trim is frame-clean with zero audio gap
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
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                str(raw_path),
            ]
            r_copy = subprocess.run(ff_copy, capture_output=True, text=True, timeout=90)
            if r_copy.returncode == 0 and raw_path.exists() and raw_path.stat().st_size > 20_000:
                downloaded_raw = True
            else:
                print(f"[CLIP] Fast copy fallback needed: {(r_copy.stderr or '')[-200:]}")
    except Exception as exc:
        print(f"[CLIP] Fast copy error ({exc}), using yt-dlp fallback...")

    if not downloaded_raw:
        ydl_opts = {
            "format": fmt,
            "outtmpl": str(raw_path),
            "merge_output_format": "mp4",
            "ffmpeg_location": str(FFMPEG_DIR),
            "download_sections": yt_dlp.utils.download_range_func(None, [(start, end)]),
            "noplaylist": True,
            "quiet": False,
            "socket_timeout": 25,
            **get_ytdlp_js_opts(),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        if not raw_path.exists():
            matches = list(CLIPS_DIR.glob(f"{tag}_raw.*"))
            if matches:
                raw_path = matches[0]
            else:
                ass_path.unlink(missing_ok=True)
                raise RuntimeError("Could not download clip section from YouTube.")

    # ---- STAGE 2: Local smooth CFR 30fps re-encode + Premium Captions ----
    print(f"[CLIP] Stage 2/2: Encoding smooth 30fps {MAX_HEIGHT}p MP4" + (f" + {theme_slug} captions..." if has_ass else "..."))
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
    project_id: int
    start: float
    end: float
    title: str
    description: str = ""
    hashtags: list[str] = []
    score: int = 85
    captions: bool = True
    caption_theme: str = "gold"


@app.get("/")
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": "ViralCut AI Backend",
        "ffmpeg_ready": bool(FFMPEG_BIN),
        "font": FONT_NAME,
        "max_height": MAX_HEIGHT,
        "captions_enabled": CAPTIONS,
    }


# ---------- AUTH ROUTES ----------
@app.post("/api/auth/register")
def register(body: RegisterRequest):
    name = body.name.strip()
    email = body.email.strip().lower()
    password = body.password

    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Please enter your full name (at least 2 characters).")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    now = datetime.now(timezone.utc).isoformat()
    pw_hash = hash_password(password)
    try:
        with get_db() as conn:
            existing_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            is_admin = 1 if (existing_count == 0 or email in {e.lower() for e in ADMIN_EMAILS}) else 0
            cur = conn.execute(
                "INSERT INTO users (name, email, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, email, pw_hash, is_admin, now),
            )
            uid = cur.lastrowid
            row = conn.execute(
                "SELECT id, name, email, is_admin, created_at FROM users WHERE id = ?",
                (uid,),
            ).fetchone()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="An account with this email already exists. Please log in.")

    user_obj = serialize_user(row)
    token = create_token(uid, email, name)
    return {"token": token, "user": user_obj}


@app.post("/api/auth/login")
def login(body: LoginRequest):
    email = body.email.strip().lower()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, email, password_hash, is_admin, created_at FROM users WHERE email = ?",
            (email,),
        ).fetchone()

    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

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
            SELECT id, project_id, video_id, title, description, hashtags_json, start_time, end_time, score, filename, has_captions, quality, created_at
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
                "thumbnail": p["thumbnail"],
                "duration": p["duration"],
                "language": p["language"],
                "top_score": p["top_score"],
                "clip_length": p["clip_length"] or 30,
                "max_clips": p["max_clips"] or 5,
                "suggestions": json.loads(p["suggestions_json"]),
                "created_at": p["created_at"],
            }
        )

    # ONLY return clips that YOU actually cut AND whose file exists on disk!
    clips = []
    for c in clip_rows:
        file_path = CLIPS_DIR / c["filename"]
        if not file_path.exists():
            continue
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
        segments, lang = get_transcript(video_id)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"No transcript available for this video ({str(exc)[:140]}). Try a video with captions enabled, or click 'Try Demo Video'.",
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

    top_score = max(s["score"] for s in suggestions)
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


@app.post("/api/demo")
def create_demo(user: dict[str, Any] = Depends(get_current_user)):
    project = create_demo_project_for_user(user["id"])
    return {"project": project}


# ---------- CUT & SAVE REAL CLIP (ONLY THESE APPEAR IN "MY CLIPS") ----------
@app.post("/api/clips/cut")
def cut_clip(body: CutClipRequest, user: dict[str, Any] = Depends(get_current_user)):
    with get_db() as conn:
        proj = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (body.project_id, user["id"]),
        ).fetchone()
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found.")

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
        else:
            out_path, has_cc, note = cut_youtube_section(
                proj["url"],
                proj["video_id"],
                body.start,
                body.end,
                transcript,
                with_captions=body.captions,
                caption_theme=body.caption_theme,
            )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

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

    return {
        "clip": {
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
        "note": note,
    }


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
        conn.execute("DELETE FROM projects WHERE id = ? AND user_id = ?", (project_id, user["id"]))
    return {"deleted": True}


@app.get("/api/clips/file/{filename}")
def serve_clip_file(filename: str, download: int = 0):
    safe_name = Path(filename).name
    path = CLIPS_DIR / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Clip file not found.")
    if download:
        return FileResponse(path, media_type="video/mp4", filename=safe_name)
    return FileResponse(path, media_type="video/mp4")
