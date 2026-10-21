"""
Trending Shots — Ingestion Pipeline (GitHub Actions edition)

Ye script sirf INGESTION + PREP karta hai — koi YouTube/Facebook upload khud
nahi karta (wo ab distributor.py ka kaam hai, jo roz sirf 5 parts hi bahar
bhejta hai, YT+FB dono par, chahe kitni bhi series queue me ho).

Har run:
  1. Agar PartsQueue me already kaafi "buffer" (>=5 parts) pending hai, to kuch
     nahi karta (naya ingestion skip) — taaki queue bematlab na badhe.
  2. Warna Queue (Google Sheet "Queue" tab) se 1 link leta hai.
  3. Download karta hai, halka edit (sirf speed-up, mirror NAHI, koi color-
     grade/watermark nahi — wo ab part-level par hota hai).
  4. Pehle 10 min transcribe (Groq Whisper) + Groq LLM se ENGLISH SEO metadata
     (title/description/tags/hashtags — ab Hinglish nahi, pure English).
  5. Poora video ko 10-MINUTE parts me kaatta hai (zoom + color-grade + "Part
     X/Y" overlay ke saath, IG/YT/FB-ready). Aakhri bacha hua tukda last part
     me hi jud jata hai (alag se chhota part nahi banta).
  6. Saare parts ek GitHub "artifact" (parts-<series_id>) me save karta hai.
  7. "PartsQueue" tab me ek row daalta hai taaki distributor.py inhe roz 5-5
     karke YouTube (PUBLIC) + Facebook Page par bhejta rahe.
  8. Success/fail Queue se link delete, History me log, Telegram notify.
"""
import os
import re
import sys
import json
import time
import uuid
import shutil
import subprocess
from datetime import datetime, timezone, timedelta

import requests
import gspread
from groq import Groq

from seo_utils import clean_hashtags, clean_tags
from video_utils import (make_part, probe_dimensions, probe_fps, add_recap_outro,
                         fetch_overlay_image)

# ---------------- CONFIG ----------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "openai/gpt-oss-120b"
WHISPER_MODEL = "whisper-large-v3-turbo"
WHISPER_LANG = os.environ.get("WHISPER_LANG", "")      # khaali = auto-detect

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
SHEET_ID = os.environ.get("SHEET_ID", "")

SPEED_FACTOR = float(os.environ.get("SPEED_FACTOR", "1.2"))
TRANSCRIBE_MAX_SECONDS = 600
PART_SECONDS = 600            # 10-minute parts
BUFFER_THRESHOLD = 5          # itne parts pending rahte hai to naya ingestion skip
MAX_ATTEMPTS = 3
MIN_FREE_DISK_GB = 1.5        # isse kam free disk par naye parts banana rok dete hai
# Vertical videos par 1%/3% opacity wali overlay image (video_utils me opacity set hai)
OVERLAY_IMAGE_URL = os.environ.get(
    "OVERLAY_IMAGE_URL",
    "https://i.ibb.co/bRdS6hqQ/Chat-GPT-Image-Aug-23-2026-10-15-32-AM.png",
)

WORK = "work"
ARTIFACT_DIR = "artifact_out"   # WORK ke bahar — wipe_work() isse nahi chhuta
os.makedirs(WORK, exist_ok=True)
os.makedirs(ARTIFACT_DIR, exist_ok=True)
IST = timezone(timedelta(hours=5, minutes=30))


class FatalError(Exception):
    """Systemic problem (token/quota) — link delete nahi hoga, run ruk jayega."""


def now_ist():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now_ist()}] {msg}", flush=True)


def tg(msg):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": msg[:4000], "disable_web_page_preview": True},
            timeout=20,
        )
    except Exception as e:
        log(f"Telegram send failed: {e}")


# ---------------- SHEET ----------------
def open_sheet():
    info = json.loads(os.environ["GCP_SA_JSON"])
    try:  # Sheets API ke 429 (rate-limit) par khud backoff+retry
        gc = gspread.service_account_from_dict(info, http_client=gspread.BackOffHTTPClient)
    except (TypeError, AttributeError):
        gc = gspread.service_account_from_dict(info)
    sh = gc.open_by_key(SHEET_ID)
    queue = sh.worksheet("Queue")  # ye user khud maintain karta hai
    try:
        hist = sh.worksheet("History")
    except gspread.WorksheetNotFound:
        hist = sh.add_worksheet("History", 2000, 7)
        hist.append_row(["Time (IST)", "Link", "Status", "Video Link", "Title", "Error", "Platform"])
    try:
        parts_q = sh.worksheet("PartsQueue")
    except gspread.WorksheetNotFound:
        parts_q = sh.add_worksheet("PartsQueue", 500, 12)
        parts_q.append_row([
            "Added (IST)", "Series ID", "Title", "Description", "Hashtags JSON",
            "Tags JSON", "Duration Sec", "Part Sec", "Total Parts",
            "Next Part Index", "Status", "Retry Hint",
        ])
    try:
        sh.worksheet("State")
    except gspread.WorksheetNotFound:
        state = sh.add_worksheet("State", 10, 2)
        state.append_row(["key", "value"])
    return queue, hist, parts_q


def next_link(queue):
    vals = queue.col_values(1)
    links = [(i, v.strip()) for i, v in enumerate(vals, start=1) if v.strip().lower().startswith("http")]
    if not links:
        return None, None, 0
    row, url = links[0]
    return row, url, len(links) - 1


def pending_parts_buffer(parts_q):
    """PartsQueue me abhi kitne parts 'bache' hai (sab pending series milakar)."""
    values = parts_q.get_all_values()[1:]
    total = 0
    for row in values:
        if len(row) < 11:
            continue
        status = row[10].strip().lower()
        if status != "pending":
            continue
        try:
            total_parts = int(float(row[8] or 1))
            next_index = int(float(row[9] or 1))
        except ValueError:
            continue
        total += max(0, total_parts - next_index + 1)
    return total


# ---------------- 1. DOWNLOAD ----------------
def download(url):
    raw = os.path.join(WORK, "raw.mp4")
    formats = ["bv*[height<=1080]+ba/b[height<=1080]/b", "b", "worst"]
    last_err = ""
    for attempt in range(2):
        for fmt in formats:
            cmd = ["yt-dlp", "-f", fmt, "--merge-output-format", "mp4", "-o", raw,
                   "--no-playlist", "--force-overwrites", "--no-warnings", "--retries", "5"]
            if os.path.exists("cookies.txt"):
                cmd += ["--cookies", "cookies.txt"]
            cmd.append(url)
            log(f"Download attempt {attempt + 1} | fmt={fmt}")
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0 and os.path.exists(raw):
                return raw
            last_err = (r.stderr or r.stdout or "")[-400:]
            if "confirm you're not a bot" in last_err or "confirm you\u2019re not a bot" in last_err:
                raise FatalError("Source site ne bot-block kiya. YT_COOKIES secret update karo.")
        time.sleep(5)
    raise RuntimeError(f"Download failed: {last_err}")


# ---------------- 2. LIGHT EDIT (speed-up only, no mirror, no colorgrade) ----------------
def edit(raw):
    """Sirf speed-up karta hai (mirror/color/zoom/captions part-level par hote
    hai). SYNC FIX: setpts se video compress hone ke baad fps filter se frames
    sahi jagah rakhte hai (sirf encoder ke vsync par chhodne se video kuch
    frames late reh jata tha), aur audio ko 48kHz par normalize karte hai taaki
    aage ke pitch-trick ka sample-rate assumption sahi rahe."""
    out = os.path.join(WORK, "edited.mp4")
    fps_str, _ = probe_fps(raw)
    vf = f"setpts=PTS/{SPEED_FACTOR},fps={fps_str}:round=near"
    atempo = f"atempo={SPEED_FACTOR}" if SPEED_FACTOR <= 2.0 else f"atempo=2.0,atempo={SPEED_FACTOR / 2.0}"
    af = f"aresample=48000,{atempo},aresample=async=1:first_pts=0"
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", raw, "-vf", vf, "-af", af,
        "-map_metadata", "-1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-b:a", "160k",
        "-movflags", "+faststart", out,
    ]
    t = time.time()
    log("Speed-up edit shuru (CPU)...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"Edit failed: {(r.stderr or '')[-400:]}")
    log(f"Edit done in {int(time.time() - t)}s")
    return out


def probe_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    return float((r.stdout or "0").strip() or 0)


# ---------------- 3. TRANSCRIBE (Groq Whisper, first 10 min) ----------------
def _w_get(w, key, default=None):
    if isinstance(w, dict):
        return w.get(key, default)
    return getattr(w, key, default)


def _extract_words(res, chunk_offset):
    """Groq verbose_json response se word list nikalta hai (dict ya object,
    dono handle). Word-timestamps na mile to segments ko characters ke hisaab
    se words me baant deta hai."""
    out = []
    words = _w_get(res, "words")
    if words:
        for w in words:
            try:
                out.append({"word": str(_w_get(w, "word", "")).strip(),
                            "start": float(_w_get(w, "start")) + chunk_offset,
                            "end": float(_w_get(w, "end")) + chunk_offset})
            except (TypeError, ValueError):
                continue
        if out:
            return out
    for seg in (_w_get(res, "segments") or []):
        try:
            s0, s1 = float(_w_get(seg, "start")), float(_w_get(seg, "end"))
        except (TypeError, ValueError):
            continue
        toks = str(_w_get(seg, "text", "")).split()
        if not toks:
            continue
        total = sum(len(t) for t in toks) or 1
        t = s0
        for tok in toks:
            d = (s1 - s0) * len(tok) / total
            out.append({"word": tok, "start": t + chunk_offset, "end": t + d + chunk_offset})
            t += d
    return out


def transcribe_full(edited_path, duration):
    """POORE video ka word-level transcript (captions + metadata dono ke liye).
    Audio 10-10 min ke chunks me Groq Whisper ko bheja jata hai (file-size/
    rate-limit safe). Timestamps edited-video ki timeline me hote hai, jo
    seedha part offsets se match karti hai. Koi chunk fail ho to wo skip hota
    hai (us hisse me captions nahi aayenge), poora pipeline nahi rukta."""
    client = Groq(api_key=GROQ_API_KEY)
    chunk_len = 600
    all_words = []
    n_chunks = max(1, int(-(-duration // chunk_len)))
    for k in range(n_chunks):
        off = k * chunk_len
        audio = os.path.join(WORK, f"chunk_{k:03d}.mp3")
        cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", str(off), "-t", str(chunk_len),
               "-i", edited_path, "-vn", "-ac", "1", "-ar", "16000",
               "-c:a", "libmp3lame", "-b:a", "48k", audio]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(audio):
            log(f"Chunk {k} audio extract fail: {(r.stderr or '')[-200:]}")
            continue
        kwargs = dict(model=WHISPER_MODEL, response_format="verbose_json",
                      timestamp_granularities=["word", "segment"])
        if WHISPER_LANG:
            kwargs["language"] = WHISPER_LANG
        done = False
        for attempt in range(1, 6):
            try:
                with open(audio, "rb") as f:
                    res = client.audio.transcriptions.create(file=(f"chunk{k}.mp3", f.read()), **kwargs)
                words = _extract_words(res, off)
                all_words.extend(words)
                log(f"Transcribe chunk {k + 1}/{n_chunks}: {len(words)} words")
                done = True
                break
            except Exception as e:
                msg = str(e)
                log(f"Transcribe chunk {k} attempt {attempt} fail: {msg[:160]}")
                if "429" in msg or "rate" in msg.lower():
                    m = re.search(r"try again in (?:(\d+)m)?(?:([\d.]+)s)?", msg)
                    wait = 30 * attempt
                    if m and (m.group(1) or m.group(2)):
                        wait = int(m.group(1) or 0) * 60 + int(float(m.group(2) or 0)) + 3
                    time.sleep(min(wait, 400))
                else:
                    time.sleep(5 * attempt)
        if not done:
            log(f"Chunk {k} transcribe nahi ho paya — is hisse me captions nahi honge.")
        try:
            os.remove(audio)
        except OSError:
            pass
    log(f"Total {len(all_words)} words transcribed.")
    return all_words


def text_of_first_seconds(words, seconds):
    return " ".join(w["word"] for w in words if w["start"] < seconds).strip()


# ---------------- 4. METADATA (Groq LLM, ENGLISH only) ----------------
PROMPT = """You are an expert YouTube/Facebook SEO and viral curiosity-marketing
content specialist for an English-language story/series channel.

You are given only the FIRST PART (beginning) of a longer video's transcript —
the full video is much longer and has a twist/climax later that is NOT included
below. Build curiosity about what happens next, WITHOUT inventing or revealing
any specific twist/ending you don't actually know. Tease it generically (e.g.
"what happens next changes everything", "the ending nobody saw coming") —
never state a fake concrete twist as fact.

LANGUAGE RULE: everything (title, description, hashtags, tags) must be in
plain ENGLISH only. No other language.

HASHTAG/TAG RULE (very important):
- "hashtags": every entry must be a single real, commonly-used, widely-searched
  English word (no spaces, no invented/made-up compound words, no gibberish).
  If you are not confident a hashtag is real and commonly used, leave it out
  rather than invent one.
- "tags": real SEO keyword phrases (1-4 words each) that people actually
  search for on YouTube. No invented phrases.

STYLE RULES:
- Heavy curiosity-gap / cliffhanger hooks: "what happens next?", "the real
  twist is at the end", "you have to watch till the end to believe it".
- Dramatic, emotional, high-CTR tone — but do not fabricate real named people,
  real crimes, or claims that could be read as factual news.
- Do NOT put any hashtags inside "description" — hashtags go only in the
  separate "hashtags" field.

Respond with ONLY a single valid, COMPLETE JSON object — no markdown, no code
fences, no commentary. Keep the response compact; a complete, slightly shorter
JSON is far better than a longer one that gets cut off. Use EXACTLY these keys:

{
  "title": "1 extreme, high-CTR clickbait title in English, emojis ok, ALL CAPS ok. Hints a twist without revealing it. Max 15 words.",
  "description": "Detailed description in English, 150-220 words. Include: (1) curiosity-hook opening in first 2 lines, (2) short summary of what's shown so far WITHOUT revealing any ending, (3) explicit tease that the real twist/climax comes later, (4) a brief one-line channel promo. NO hashtags here.",
  "hashtags": ["15-20 real, commonly-used ENGLISH hashtags (no # symbol, no spaces, lowercase, single words)"],
  "tags": ["exactly 20 real ENGLISH SEO keyword phrases (1-4 words each) that people actually search"],
  "thumbnail_prompt": "A detailed but concise (under 80 words) prompt for an AI image generator to create a high-CTR clickbait thumbnail based on the most dramatic moment visible in this partial transcript — suspenseful, not graphic."
}

Finish the JSON completely — do not leave any field or bracket unclosed.

Transcript (first part of the video only):
__TRANSCRIPT__
"""


def _shrink(text, max_chars):
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    return text[:head].rstrip() + "\n...[truncated]...\n" + text[-(max_chars - head):].lstrip()


def _parse_json(raw):
    s = raw.strip()
    s = re.sub(r"^```(json)?", "", s, flags=re.I).strip()
    s = re.sub(r"```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        a, b = s.find("{"), s.rfind("}")
        if a != -1 and b > a:
            return json.loads(s[a:b + 1])
        raise


def generate_metadata(transcript):
    client = Groq(api_key=GROQ_API_KEY)
    max_chars = 9000
    completion = 3000
    for attempt in range(1, 5):
        prompt = PROMPT.replace("__TRANSCRIPT__", _shrink(transcript, max_chars))
        log(f"Metadata attempt {attempt} | transcript_chars<={max_chars} | max_tokens={completion}")
        try:
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=completion,
                response_format={"type": "json_object"},
                extra_body={"reasoning_effort": "low"},
            )
            text = resp.choices[0].message.content
            if not text:
                raise ValueError("empty response")
            data = _parse_json(text)
            for k in ("title", "description", "hashtags", "tags", "thumbnail_prompt"):
                if k not in data:
                    raise ValueError(f"missing key {k}")
            return data
        except Exception as e:
            err = str(e).lower()
            log(f"Metadata error: {str(e)[:200]}")
            if "413" in err or "tokens per minute" in err or "tpm" in err:
                max_chars = max(2500, max_chars // 2)
            elif "429" in err or "rate" in err or "overloaded" in err or "503" in err:
                time.sleep(20)
            else:
                completion = min(4500, completion + 700)
                max_chars = max(3000, int(max_chars * 0.75))
    return None


def fallback_metadata():
    return {
        "title": "Trending Story — Part Series",
        "description": "An ongoing story series. Watch till the end for the full twist.",
        "hashtags": [],
        "tags": [],
        "thumbnail_prompt": "",
    }


# ---------------- 5. CUT INTO 10-MIN PARTS ----------------
def disk_free_gb(path="."):
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except OSError:
        return 999.0


def prepare_parts(edited_path, duration, series_id, title="", description="",
                  words=None, overlay_img=None):
    """Poore video ko 10-min parts me kaatta hai. Aakhri bacha hua tukda
    (remainder) alag chhota part nahi banta — last part me hi jud jata hai.

    Har part ke liye FALLBACK CHAIN (koi fancy feature fail ho to part gayab
    nahi hota, simple version me ban jata hai):
        1) captions + overlay image + zoom   (full)
        2) bina captions
        3) bina captions, bina overlay image
        4) bina captions, bina overlay, bina zoom  (sabse safe)
    Aakhir me end me ~1 min ka freeze-frame + voice recap outro jodta hai
    (outro fail ho to part bina outro ke hi ship hota hai).

    Returns: (total_parts, parts_made, notes)"""
    total_parts = max(1, int(duration // PART_SECONDS))
    parts_dir = os.path.join(ARTIFACT_DIR, "parts")
    os.makedirs(parts_dir, exist_ok=True)
    tmp_dir = os.path.join(WORK, "partstmp")
    os.makedirs(tmp_dir, exist_ok=True)

    width, height = probe_dimensions(edited_path)
    log(f"Source dimensions: {width}x{height} | {total_parts} parts banenge (~{PART_SECONDS // 60} min each)")

    levels = [
        ("full", dict(words=words, overlay_img=overlay_img, use_zoom=True)),
        ("no-captions", dict(words=None, overlay_img=overlay_img, use_zoom=True)),
        ("no-captions/no-overlay", dict(words=None, overlay_img=None, use_zoom=True)),
        ("safe-mode(no zoom)", dict(words=None, overlay_img=None, use_zoom=False)),
    ]
    # Jo features pehle se hi off hai unke duplicate levels hata do
    seen, uniq = set(), []
    for name, kw in levels:
        key = (bool(kw["words"]), bool(kw["overlay_img"]), kw["use_zoom"])
        if key not in seen:
            seen.add(key)
            uniq.append((name, kw))
    levels = uniq

    parts_made, notes, degraded, no_outro, failed_parts = 0, [], [], [], []
    for i in range(1, total_parts + 1):
        if disk_free_gb() < MIN_FREE_DISK_GB:
            log(f"Disk kam hai ({disk_free_gb():.1f}GB free) — baaki parts nahi bana raha.")
            notes.append(f"disk full hone se part {i} se aage nahi bane")
            failed_parts.extend(range(i, total_parts + 1))
            break
        offset = (i - 1) * PART_SECONDS
        this_dur = (duration - offset) if i == total_parts else PART_SECONDS
        final_path = os.path.join(parts_dir, f"part_{i:03d}.mp4")
        raw_part = os.path.join(tmp_dir, f"part_{i:03d}_main.mp4")

        built, cw, ch, cap_note, used_level = False, width, height, "", ""
        for lvl_name, kw in levels:
            try:
                _, (cw, ch), cap_note = make_part(edited_path, offset, this_dur, i, total_parts,
                                                  raw_part, width, height, **kw)
                built, used_level = True, lvl_name
                break
            except Exception as e:
                log(f"Part {i} level '{lvl_name}' fail: {str(e)[:300]}")
        if not built:
            log(f"Part {i} kisi bhi level me nahi bana — skip.")
            failed_parts.append(i)
            continue
        if used_level != "full":
            degraded.append(f"{i}({used_level})")

        try:
            add_recap_outro(raw_part, title, description, final_path, cw, ch)
            log(f"Part {i}/{total_parts} taiyar [{used_level}: {cap_note}] + recap outro")
        except Exception as e:
            log(f"Part {i} recap outro fail ({str(e)[:200]}) — bina outro ke ship.")
            no_outro.append(i)
            shutil.copy(raw_part, final_path)
        parts_made += 1
        try:
            os.remove(raw_part)
        except OSError:
            pass

    if degraded:
        notes.append("simple mode me bane: " + ", ".join(degraded))
    if no_outro:
        notes.append(f"recap outro nahi laga: parts {no_outro}")
    if failed_parts:
        notes.append(f"BANE NAHI: parts {failed_parts}")
    log(f"{parts_made}/{total_parts} parts taiyar ho gaye.")
    return total_parts, parts_made, notes


# ---------------- ORCHESTRATION ----------------
def wipe_work():
    for f in os.listdir(WORK):
        p = os.path.join(WORK, f)
        try:
            os.remove(p) if os.path.isfile(p) else shutil.rmtree(p)
        except OSError:
            pass


def process(url, parts_q):
    raw = download(url)
    edited = edit(raw)
    try:  # raw ab zaroorat nahi — disk bachao (lambe videos me kai GB)
        os.remove(raw)
    except OSError:
        pass
    duration = probe_duration(edited)
    if duration < 5:
        raise RuntimeError(f"Edited video ki duration bahut kam hai ({duration:.1f}s) — source kharab lagta hai.")
    notes = []

    # Poore video ka word-level transcript — captions ke liye bhi, aur
    # pehle 10 min ka text metadata ke liye bhi (alag se dobara transcribe nahi).
    words = []
    try:
        words = transcribe_full(edited, duration)
    except Exception as e:
        log(f"Full transcription fail (captions nahi honge): {e}")

    if not words:
        notes.append("transcript nahi mila — captions nahi lage")
    meta = None
    try:
        transcript = text_of_first_seconds(words, TRANSCRIBE_MAX_SECONDS)
        if transcript.strip():
            meta = generate_metadata(transcript)
    except Exception as e:
        log(f"Metadata step fail (default metadata use hoga): {e}")
    if not meta:
        meta = fallback_metadata()

    title = str(meta.get("title") or "Trending Story").replace("<", "").replace(">", "")[:98]
    description = str(meta.get("description") or "")
    hashtags = clean_hashtags(meta.get("hashtags"))
    tags = clean_tags(meta.get("tags"))

    series_id = uuid.uuid4().hex[:12]

    overlay_img = fetch_overlay_image(OVERLAY_IMAGE_URL, os.path.join(WORK, "overlay.png"))
    if OVERLAY_IMAGE_URL and not overlay_img:
        notes.append("overlay image download nahi hui — overlay skip hua")

    total_parts, parts_made, part_notes = prepare_parts(
        edited, duration, series_id, title, description, words, overlay_img)
    notes.extend(part_notes)
    if parts_made == 0:
        # Ek bhi part nahi bana — queue me khaali series mat daalo; link fail
        # mana jayega aur agla link try hoga.
        raise RuntimeError("Koi bhi part nahi ban paya (details upar log me).")

    parts_q.append_row([
        now_ist(), series_id, title, description,
        json.dumps(hashtags, ensure_ascii=False), json.dumps(tags, ensure_ascii=False),
        round(duration, 1), PART_SECONDS, total_parts, 1, "pending", "",
    ])

    thumb = meta.get("thumbnail_prompt", "")
    return series_id, title, thumb, total_parts, notes


def main():
    if os.environ.get("YT_COOKIES"):
        with open("cookies.txt", "w") as f:
            f.write(os.environ["YT_COOKIES"])

    try:
        queue, hist, parts_q = open_sheet()
    except Exception as e:
        tg(f"❌ Google Sheet open nahi hui: {e}")
        raise

    buffer = pending_parts_buffer(parts_q)
    if buffer >= BUFFER_THRESHOLD:
        log(f"PartsQueue me already {buffer} parts pending hai (>= {BUFFER_THRESHOLD}) — ingestion skip.")
        return 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        row, url, remaining = next_link(queue)
        if not url:
            tg("📭 Queue khaali hai — koi link nahi mila. Sheet me naye links daalo.")
            return 0
        tg(f"⏳ Ingestion start (try {attempt}/{MAX_ATTEMPTS}, buffer={buffer}):\n{url}")
        try:
            series_id, title, thumb, total_parts, notes = process(url, parts_q)
            hist.append_row([now_ist(), url, "SUCCESS", series_id, title, "", "INGEST"])
            queue.delete_rows(row)

            gh_out = os.environ.get("GITHUB_OUTPUT")
            if gh_out:
                with open(gh_out, "a") as f:
                    f.write(f"series_id={series_id}\n")

            msg = (f"✅ SUCCESS — prepped {total_parts} parts (~10 min each)\n{title}\n"
                   f"Series ID: {series_id}\nQueue me bache: {remaining}")
            if notes:
                msg += "\n\n⚠️ Notes:\n• " + "\n• ".join(notes)
            if thumb:
                msg += f"\n\n🎨 Thumbnail prompt:\n{thumb}"
            tg(msg)
            if remaining < 2:
                tg(f"⚠️ Queue me sirf {remaining} link bache hain — aur links daal do.")
            return 0
        except FatalError as e:
            tg(f"🛑 RUK GAYA (link safe hai, delete nahi hua):\n{e}")
            log(f"FATAL: {e}")
            return 1
        except Exception as e:
            err = str(e)[:500]
            log(f"FAILED: {err}")
            try:
                hist.append_row([now_ist(), url, "FAILED", "", "", err, "INGEST"])
                queue.delete_rows(row)
            except Exception as se:
                log(f"Sheet update fail: {se}")
            tg(f"❌ FAILED\n{url}\n{err}\n(agla link try kar raha hoon...)")
        finally:
            wipe_work()

    tg(f"❌ Is run me {MAX_ATTEMPTS} links fail hue — koi video prepare nahi hua.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
