"""
Trending Shots — Auto Pipeline (GitHub Actions edition)

Har run:
  1. Queue (Google Sheet "Queue" tab) se 1 link leta hai.
  2. Download + edit (speed/flip/watermark, CPU ffmpeg).
  3. Pehle 10 min transcribe (Groq Whisper) + Groq LLM se SEO metadata.
  4. Master video YouTube par upload (unlisted/public — see PRIVACY).
  5. WAHI master video Facebook Page par bhi upload (normal video post).
  6. "ReelsQueue" tab me ek row daalta hai — taaki reels.py isko baad me
     85-sec ke parts me kaatke IG Reels + FB Reels par daal sake.
  7. Success/fail Queue se link delete, History me log, Telegram notify.
"""
import os
import re
import sys
import json
import time
import shutil
import subprocess
from datetime import datetime, timezone, timedelta

import requests
import gspread
from groq import Groq
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from seo_utils import clean_hashtags, clean_tags, hashtag_block

# ---------------- CONFIG ----------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "openai/gpt-oss-120b"
WHISPER_MODEL = "whisper-large-v3-turbo"
WHISPER_LANG = os.environ.get("WHISPER_LANG", "")      # e.g. "hi" (khaali = auto-detect)

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
SHEET_ID = os.environ.get("SHEET_ID", "")
PRIVACY = os.environ.get("PRIVACY", "unlisted")   # public/unlisted/private (reels.py ko UNLISTED+ chahiye)

FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "")
FB_PAGE_TOKEN = os.environ.get("FB_PAGE_TOKEN", "")
GRAPH_VER = "v21.0"

SPEED_FACTOR = 1.2
TRANSCRIBE_MAX_SECONDS = 600
REEL_PART_SECONDS = 85
MAX_ATTEMPTS = 3
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

WORK = "work"
os.makedirs(WORK, exist_ok=True)
IST = timezone(timedelta(hours=5, minutes=30))


class FatalError(Exception):
    """Systemic problem (token/quota/bot-block) — link delete nahi hoga, run ruk jayega."""


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
    gc = gspread.service_account_from_dict(json.loads(os.environ["GCP_SA_JSON"]))
    sh = gc.open_by_key(SHEET_ID)
    queue = sh.worksheet("Queue")
    try:
        hist = sh.worksheet("History")
    except gspread.WorksheetNotFound:
        hist = sh.add_worksheet("History", 2000, 7)
        hist.append_row(["Time (IST)", "Link", "Status", "YouTube Link", "Title", "Error", "Platform"])
    try:
        reels_q = sh.worksheet("ReelsQueue")
    except gspread.WorksheetNotFound:
        reels_q = sh.add_worksheet("ReelsQueue", 500, 12)
        reels_q.append_row([
            "Added (IST)", "YT ID", "YT Link", "Title", "Reel Caption",
            "Hashtags JSON", "Duration Sec", "Part Sec", "Total Parts",
            "Next Part Index", "Status", "Master URL Hint",
        ])
    return queue, hist, reels_q


def next_link(queue):
    vals = queue.col_values(1)
    links = [(i, v.strip()) for i, v in enumerate(vals, start=1) if v.strip().lower().startswith("http")]
    if not links:
        return None, None, 0
    row, url = links[0]
    return row, url, len(links) - 1


# ---------------- 1. DOWNLOAD ----------------
def download(url):
    raw = os.path.join(WORK, "raw.mp4")
    formats = ["bv*[height<=720]+ba/b[height<=720]/b", "b", "worst"]
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
            if "confirm you’re not a bot" in last_err or "confirm you're not a bot" in last_err:
                raise FatalError("YouTube ne bot-block kiya. YT_COOKIES secret update karo.")
        time.sleep(5)
    raise RuntimeError(f"Download failed: {last_err}")


# ---------------- 2. EDIT (CPU) ----------------
def edit(raw):
    out = os.path.join(WORK, "master.mp4")
    wm_left = f"drawtext=fontfile={FONT}:text='Trending Shots':x=20:y=H-th-20:fontsize=24:fontcolor=white@0.5"
    wm_right = (f"drawtext=fontfile={FONT}:text='Trending Shots':x=W-tw-20:y=H-th-20:fontsize=36:"
                "fontcolor=yellow:box=1:boxcolor=black@0.6")
    vf = (f"scale=-2:720,setpts=PTS/{SPEED_FACTOR},hflip,"
          f"eq=saturation=1.25:contrast=1.15:brightness=0.02,{wm_left},{wm_right}")
    af = f"highpass=f=80,lowpass=f=12000,dynaudnorm,bass=g=4:f=110,atempo={SPEED_FACTOR}"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-vf", vf, "-af", af,
           "-map_metadata", "-1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
           "-maxrate", "3M", "-bufsize", "6M", "-threads", "0", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out]
    t = time.time()
    log("Master edit shuru (CPU)...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"Edit failed: {(r.stderr or '')[-400:]}")
    log(f"Master edit done in {int(time.time() - t)}s")
    return out


def probe_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    return float((r.stdout or "0").strip() or 0)


# ---------------- 3. TRANSCRIBE (Groq Whisper, first 10 min) ----------------
def transcribe(raw):
    audio = os.path.join(WORK, "audio.mp3")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-t", str(TRANSCRIBE_MAX_SECONDS),
           "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k", audio]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(audio):
        raise RuntimeError(f"Audio extract failed: {(r.stderr or '')[-300:]}")
    client = Groq(api_key=GROQ_API_KEY)
    kwargs = dict(model=WHISPER_MODEL, response_format="json")
    if WHISPER_LANG:
        kwargs["language"] = WHISPER_LANG
    with open(audio, "rb") as f:
        res = client.audio.transcriptions.create(file=("audio.mp3", f.read()), **kwargs)
    text = res if isinstance(res, str) else res.text
    log(f"Transcript ready ({len(text)} chars)")
    return text


# ---------------- 4. METADATA (Groq LLM) ----------------
PROMPT = """You are an expert YouTube/Instagram/Facebook SEO and viral
suspense/curiosity-marketing content specialist for a Hindi/Hinglish
storytelling channel called 'Trending Shots'.

You are given only the FIRST PART (beginning) of a longer video's transcript —
the full video is much longer and has a twist/climax later that is NOT included
below. Build curiosity and suspense about what happens next, WITHOUT inventing
or revealing any specific twist/ending you don't actually know. Tease it
generically (e.g. "aage jo hua usne sabko hila diya", "ant me aisa mod aaya
jiski kisi ko umeed nahi thi") — never state a fake concrete twist as fact.

LANGUAGE RULES (very important):
- "title", "description" and "reel_caption": natural Hinglish — a mix of Hindi
  (Devanagari or Roman) and English, whichever reads as high-CTR clickbait.
  Do NOT put hashtags inside these three fields.
- "hashtags": ENGLISH ONLY. Every entry must be a single real, commonly-used,
  widely-searched English word (no Hindi, no invented/made-up compound words,
  no gibberish). If you are not confident a hashtag is a real, commonly used
  one for this kind of content (story/suspense/drama/animation/reels), do not
  include it — leave it out rather than invent one.
- "tags": ENGLISH ONLY, short real SEO keyword phrases (1-4 words each) that
  people actually search for on YouTube (e.g. "hindi story", "suspense
  thriller", "animated story"). No Hindi, no invented phrases.

STYLE RULES:
- Heavy curiosity-gap / cliffhanger hooks: "aage kya hua?", "asli twist end
  mein hai", "video pura dekhoge to hi pata chalega". Viewer must feel they
  MUST watch till the end.
- Include a viewer-discretion / negative-marketing style warning line near the
  top of the description — e.g. "⚠️ Ye video bacchon (18 saal se kam) ko akele
  nahi dekhna chahiye — content emotionally intense/disturbing hai." Marketing
  hook, not a literal rating — keep it tasteful, no explicit/graphic claims.
- Dramatic, emotional, shocking tone — but do not fabricate real named people,
  real crimes, or claims that could be read as factual news.
- Do NOT put any hashtags inside "description" or "reel_caption" — hashtags go
  only in the separate "hashtags" field.

Respond with ONLY a single valid, COMPLETE JSON object — no markdown, no code
fences, no commentary. Keep the response compact; a complete, slightly shorter
JSON is far better than a longer one that gets cut off. Use EXACTLY these keys:

{
  "title": "1 extreme, high-CTR clickbait title, Hinglish mix, emojis, ALL CAPS ok. Hints a twist without revealing it. Max 15 words.",
  "description": "Detailed description, Hinglish mix, __DESC_WORDS__ words. Include: (1) curiosity-hook opening in first 2 lines, (2) viewer-discretion warning line near the top, (3) short story summary of what's shown so far WITHOUT revealing any ending, (4) explicit tease that the real twist/climax comes later, (5) brief 'About the Channel' line promoting Trending Shots. NO hashtags here.",
  "reel_caption": "A short 1-2 line Hinglish curiosity hook for a short Reels clip of this story (under 150 characters). NO hashtags here.",
  "hashtags": ["15-20 real, commonly-used ENGLISH hashtags (no # symbol, no spaces, lowercase, single words), relevant to story/suspense/drama/reels content"],
  "tags": ["exactly 20 real ENGLISH SEO keyword phrases (1-4 words each) that people actually search"],
  "thumbnail_prompt": "A detailed but concise (under 80 words) prompt for an AI image generator to create a high-CTR clickbait 2D animated style thumbnail based on the most dramatic moment visible in this partial transcript — suspenseful, not graphic."
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
    completion = 3200
    for attempt in range(1, 5):
        desc_words = "250-300" if completion >= 3200 else "150-200"
        prompt = PROMPT.replace("__DESC_WORDS__", desc_words).replace(
            "__TRANSCRIPT__", _shrink(transcript, max_chars))
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
            for k in ("title", "description", "reel_caption", "hashtags", "tags", "thumbnail_prompt"):
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
            else:  # json cut / parse / validate
                completion = min(4800, completion + 700)
                max_chars = max(3000, int(max_chars * 0.75))
    return None


def build_youtube_fields(meta):
    title, desc = "Trending Shots AI Story", "Awesome 2D Animation"
    hashtags, tags = clean_hashtags([]), clean_tags([])
    if meta:
        title = str(meta.get("title") or title)
        desc = str(meta.get("description") or desc)
        hashtags = clean_hashtags(meta.get("hashtags"))
        tags = clean_tags(meta.get("tags"))
    title = title.replace("<", "").replace(">", "").replace('"', "")[:98]
    desc = desc.replace("<", "").replace(">", "")
    if hashtags:
        desc = desc.rstrip() + "\n\n" + hashtag_block(hashtags)
    while len(desc.encode("utf-8")) > 4900:
        desc = desc[:-100]
    return title, desc, tags


# ---------------- 5. YOUTUBE UPLOAD ----------------
def yt_service():
    info = json.loads(os.environ["YT_TOKEN_JSON"])
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as e:
            raise FatalError(f"YouTube token refresh fail (get_token.py dobara chalao): {e}")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload_youtube(video_path, title, desc, tags):
    yt = yt_service()
    body = {
        "snippet": {"title": title, "description": desc, "tags": tags, "categoryId": "24"},
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(video_path, chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response, retries, last_pct = None, 0, -10
    while response is None:
        try:
            st, response = req.next_chunk()
            if st:
                pct = int(st.progress() * 100)
                if pct >= last_pct + 10:
                    log(f"YT upload {pct}%")
                    last_pct = pct
        except HttpError as e:
            content = (e.content or b"").decode("utf-8", "ignore")
            if "quotaExceeded" in content or "uploadLimitExceeded" in content:
                raise FatalError("YouTube quota/upload limit khatam ho gaya.")
            if e.resp.status in (500, 502, 503, 504) and retries < 5:
                retries += 1
                time.sleep(5 * retries)
                continue
            raise
    return response["id"]


# ---------------- 6. FACEBOOK — FULL MASTER VIDEO (normal page post) ----------------
def fb_upload_full_video(path, title, desc):
    if not FB_PAGE_ID or not FB_PAGE_TOKEN:
        log("FB_PAGE_ID/FB_PAGE_TOKEN set nahi — FB full-video upload skip.")
        return None
    size = os.path.getsize(path)
    r = requests.post(
        f"https://graph-video.facebook.com/{GRAPH_VER}/{FB_PAGE_ID}/videos",
        data={"upload_phase": "start", "access_token": FB_PAGE_TOKEN, "file_size": size},
        timeout=60,
    ).json()
    if "upload_session_id" not in r:
        raise RuntimeError(f"FB start failed: {r}")
    session_id = r["upload_session_id"]
    video_id = r["video_id"]
    start, end = int(r["start_offset"]), int(r["end_offset"])
    with open(path, "rb") as f:
        while start != end:
            f.seek(start)
            chunk = f.read(end - start)
            resp = requests.post(
                f"https://graph-video.facebook.com/{GRAPH_VER}/{FB_PAGE_ID}/videos",
                data={"upload_phase": "transfer", "upload_session_id": session_id,
                      "start_offset": start, "access_token": FB_PAGE_TOKEN},
                files={"video_file_chunk": chunk},
                timeout=300,
            ).json()
            if "start_offset" not in resp:
                raise RuntimeError(f"FB transfer failed at offset {start}: {resp}")
            start, end = int(resp["start_offset"]), int(resp["end_offset"])
    fin = requests.post(
        f"https://graph-video.facebook.com/{GRAPH_VER}/{FB_PAGE_ID}/videos",
        data={"upload_phase": "finish", "upload_session_id": session_id,
              "access_token": FB_PAGE_TOKEN, "title": title[:255], "description": desc},
        timeout=60,
    ).json()
    if fin.get("success") is False:
        raise RuntimeError(f"FB finish failed: {fin}")

    # FB ab single video uploads ko bhi "Reel" ki tarah treat karta hai, isliye
    # asli dekhne wala link nikalne ke liye permalink_url fetch karo (thoda wait
    # karo taaki processing shuru ho jaye).
    permalink = f"https://www.facebook.com/{video_id}"
    for _ in range(6):
        time.sleep(5)
        try:
            info = requests.get(
                f"https://graph.facebook.com/{GRAPH_VER}/{video_id}",
                params={"fields": "permalink_url,published", "access_token": FB_PAGE_TOKEN},
                timeout=30,
            ).json()
            if info.get("permalink_url"):
                permalink = f"https://www.facebook.com{info['permalink_url']}"
                break
        except Exception:
            pass
    return video_id, permalink


# ---------------- 7. ORCHESTRATION ----------------
def wipe_work():
    for f in os.listdir(WORK):
        p = os.path.join(WORK, f)
        try:
            os.remove(p) if os.path.isfile(p) else shutil.rmtree(p)
        except OSError:
            pass


def process(url, reels_q):
    raw = download(url)
    master = edit(raw)
    duration = probe_duration(master)

    meta = None
    try:
        transcript = transcribe(raw)
        if transcript.strip():
            meta = generate_metadata(transcript)
    except Exception as e:
        log(f"Transcribe/metadata step fail (default metadata use hoga): {e}")

    title, desc, tags = build_youtube_fields(meta)
    vid = upload_youtube(master, title, desc, tags)

    fb_note = ""
    try:
        fb_result = fb_upload_full_video(master, title, meta.get("description", desc) if meta else desc)
        if fb_result:
            fb_id, fb_link = fb_result
            fb_note = f"\n📘 FB (dekhega Reel tab me): {fb_link}"
    except Exception as e:
        log(f"FB full-video upload failed (non-fatal): {e}")
        fb_note = f"\n⚠️ FB full-video upload fail hua: {str(e)[:200]}"

    # ReelsQueue me row daalo taaki reels.py isse baad me kaate
    reel_caption = (meta or {}).get("reel_caption") or title
    hashtags = clean_hashtags((meta or {}).get("hashtags"))
    total_parts = max(1, int(duration // REEL_PART_SECONDS) + (1 if duration % REEL_PART_SECONDS >= 15 else 0))
    reels_q.append_row([
        now_ist(), vid, f"https://youtu.be/{vid}", title, reel_caption,
        json.dumps(hashtags, ensure_ascii=False), round(duration, 1), REEL_PART_SECONDS,
        total_parts, 1, "pending", "",
    ])

    thumb = (meta or {}).get("thumbnail_prompt", "")
    return vid, title, thumb, fb_note, total_parts


def main():
    if os.environ.get("YT_COOKIES"):
        with open("cookies.txt", "w") as f:
            f.write(os.environ["YT_COOKIES"])

    try:
        queue, hist, reels_q = open_sheet()
    except Exception as e:
        tg(f"❌ Google Sheet open nahi hui: {e}")
        raise

    for attempt in range(1, MAX_ATTEMPTS + 1):
        row, url, remaining = next_link(queue)
        if not url:
            tg("📭 Queue khaali hai — koi link nahi mila. Sheet me naye links daalo.")
            return 0
        tg(f"⏳ Start (try {attempt}/{MAX_ATTEMPTS}):\n{url}")
        try:
            vid, title, thumb, fb_note, total_parts = process(url, reels_q)
            yt_link = f"https://youtu.be/{vid}"
            hist.append_row([now_ist(), url, "SUCCESS", yt_link, title, "", "YT+FB"])
            queue.delete_rows(row)
            msg = (f"✅ SUCCESS\n{title}\n{yt_link}{fb_note}\n"
                   f"🎬 Reels queue me {total_parts} parts add hue (85 sec each)\n"
                   f"Queue me bache: {remaining}")
            if thumb:
                msg += f"\n\n🎨 Thumbnail prompt:\n{thumb}"
            tg(msg)
            if remaining < 5:
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
                hist.append_row([now_ist(), url, "FAILED", "", "", err, "YT+FB"])
                queue.delete_rows(row)
            except Exception as se:
                log(f"Sheet update fail: {se}")
            tg(f"❌ FAILED\n{url}\n{err}\n(agla link try kar raha hoon...)")
        finally:
            wipe_work()

    tg(f"❌ Is run me {MAX_ATTEMPTS} links fail hue — koi video upload nahi hua.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
