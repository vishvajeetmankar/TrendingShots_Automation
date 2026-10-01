"""
Trending Shots — Reels distributor (IG Reels + FB Reels)

Kya karta hai (har run):
  1. Google Sheet ki "ReelsQueue" tab se sabse purane pending master video ki
     row uthata hai.
  2. Master video ka source nikalta hai:
       Priority 1: pipeline.py ne jo already-edited master video GitHub
                   Actions "artifact" ke roop me save kiya tha, seedha wahi
                   use karta hai — YouTube se DOBARA download nahi hota,
                   koi cookies/bot-block ka jhanjhat nahi.
       Priority 2 (fallback, sirf tab jab artifact expire/missing ho): sheet
                   me stored ORIGINAL input link (jo Queue me paste kiya tha,
                   FB wala) se yt-dlp se nikalta hai — YouTube link se NAHI.
  3. Us part ko seek+cut karke vertical 1080x1920 canvas me letterbox karta
     hai (upar-niche black bar — horizontal video ke liye), aur upar
     "Part N/Total" likhta hai.
  4. IG Reels + FB Reels dono par upload karta hai — PUBLIC.
  5. IG ka rate limit LIVE check karta hai (content_publishing_limit API se);
     FB Reels ka rate limit apni History sheet se khud track karta hai
     (official docs: 30 reels/24h/page — hum 25 tak hi jaate hai, safety margin).
  6. Jitne parts is run me ho sakein utne karta hai (REELS_MAX_PER_RUN tak),
     baaki agle scheduled run me. Ek part dono platform par fail ho to usi
     part ko dobara try karta hai (MAX_PART_RETRIES tak), index tabhi
     badhta hai jab kam se kam ek platform par upload safal ho.
"""
import os
import re
import sys
import json
import time
import shutil
import zipfile
import subprocess
from datetime import datetime, timezone, timedelta

import requests
import gspread

from seo_utils import clean_hashtags, hashtag_block

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
SHEET_ID = os.environ.get("SHEET_ID", "")

IG_USER_ID = os.environ.get("IG_USER_ID", "")
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
GRAPH_VER = "v21.0"

REELS_MAX_PER_RUN = int(os.environ.get("REELS_MAX_PER_RUN", "4"))
MAX_PART_RETRIES = 3             # itni baar fail hone ke baad hi part skip hoga
IG_SAFETY_MARGIN = 2             # IG ke live-reported total me se itna margin rakho

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
WORK = "reels_work"
os.makedirs(WORK, exist_ok=True)
IST = timezone(timedelta(hours=5, minutes=30))


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


def wipe_work():
    keep = {os.path.abspath(p) for p in _master_cache.values() if p}
    for f in os.listdir(WORK):
        p = os.path.join(WORK, f)
        if os.path.abspath(p) in keep:
            continue
        try:
            os.remove(p) if os.path.isfile(p) else shutil.rmtree(p)
        except OSError:
            pass


# ---------------- SHEET ----------------
def open_sheet():
    gc = gspread.service_account_from_dict(json.loads(os.environ["GCP_SA_JSON"]))
    sh = gc.open_by_key(SHEET_ID)
    reels_q = sh.worksheet("ReelsQueue")
    try:
        hist = sh.worksheet("History")
    except gspread.WorksheetNotFound:
        hist = sh.add_worksheet("History", 2000, 7)
        hist.append_row(["Time (IST)", "Link", "Status", "YouTube Link", "Title", "Error", "Platform"])
    return reels_q, hist


COLS = ["added", "yt_id", "yt_link", "title", "reel_caption", "hashtags_json",
        "duration", "part_sec", "total_parts", "next_index", "status", "hint",
        "source_url"]


def pending_rows(reels_q):
    """Returns list of (row_number, dict) for rows with status == 'pending', oldest first."""
    values = reels_q.get_all_values()
    out = []
    for i, row in enumerate(values[1:], start=2):  # row 1 = header
        row = row + [""] * (len(COLS) - len(row))
        d = dict(zip(COLS, row))
        if d.get("status", "").strip().lower() == "pending" and d.get("yt_id"):
            out.append((i, d))
    return out


# ---------------- IG QUOTA ----------------
def ig_quota_remaining():
    if not IG_USER_ID or not IG_ACCESS_TOKEN:
        return 0
    try:
        r = requests.get(
            f"https://graph.facebook.com/{GRAPH_VER}/{IG_USER_ID}/content_publishing_limit",
            params={"fields": "config,quota_usage", "access_token": IG_ACCESS_TOKEN},
            timeout=30,
        ).json()
        d = (r.get("data") or [{}])[0]
        total = d.get("config", {}).get("quota_total", 50)
        used = d.get("quota_usage", 0)
        return max(0, total - used - IG_SAFETY_MARGIN)
    except Exception as e:
        log(f"IG quota check failed, assuming 0 remaining this run: {e}")
        return 0


# ---------------- MASTER VIDEO SOURCE ----------------
# Priority 1: pipeline.py ne jo master video GitHub Actions "artifact" ke roop
# me save kiya tha, wahi use karo — koi dobara download nahi, koi cookies nahi.
# Priority 2 (fallback, tabhi jab artifact expire/missing ho): sheet me stored
# ORIGINAL input link (FB wala) se yt-dlp se nikalo — YouTube link se NAHI,
# taaki YouTube ka bot-block wala jhanjhat hi na aaye.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
_master_cache = {}  # yt_id -> local file path, isi run ke andar dobara fetch na ho


def fetch_master_artifact(yt_id):
    if yt_id in _master_cache:
        return _master_cache[yt_id]
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        _master_cache[yt_id] = None
        return None
    name = f"master-{yt_id}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/artifacts",
            headers=headers, params={"name": name, "per_page": 10}, timeout=30,
        )
        r.raise_for_status()
        arts = [a for a in r.json().get("artifacts", []) if not a.get("expired")]
        if not arts:
            log(f"Artifact '{name}' nahi mila (expire ho gaya ya abhi bana nahi) — fallback use hoga.")
            _master_cache[yt_id] = None
            return None
        art = sorted(arts, key=lambda a: a["created_at"], reverse=True)[0]
        dl = requests.get(art["archive_download_url"], headers=headers, timeout=600)
        dl.raise_for_status()
        zpath = os.path.join(WORK, f"{yt_id}_artifact.zip")
        with open(zpath, "wb") as f:
            f.write(dl.content)
        with zipfile.ZipFile(zpath) as z:
            mp4_names = [n for n in z.namelist() if n.endswith(".mp4")]
            if not mp4_names:
                _master_cache[yt_id] = None
                return None
            z.extract(mp4_names[0], WORK)
            local_path = os.path.join(WORK, mp4_names[0])
        log(f"Artifact '{name}' se master mil gaya (dobara download nahi hua).")
        _master_cache[yt_id] = local_path
        return local_path
    except Exception as e:
        log(f"Artifact fetch fail ({name}): {e}")
        _master_cache[yt_id] = None
        return None


def get_master_source(d):
    """Local file path (preferred) ya remote direct-URL (fallback) return karta hai."""
    local = fetch_master_artifact(d["yt_id"])
    if local:
        return local
    source_url = d.get("source_url") or d.get("yt_link")
    log(f"Fallback: original source link se nikal raha hoon: {source_url}")
    return get_direct_url(source_url)


# ---------------- YT DIRECT URL ----------------
def get_direct_url(yt_link):
    cmd = ["yt-dlp", "-f", "b[height<=720]/b", "-g"]
    if os.path.exists("cookies.txt"):
        cmd += ["--cookies", "cookies.txt"]
    cmd.append(yt_link)
    last_err = ""
    for attempt in range(3):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        urls = [u for u in (r.stdout or "").strip().splitlines() if u.startswith("http")]
        if r.returncode == 0 and urls:
            return urls[0]
        last_err = (r.stderr or "")[-300:]
        log(f"yt-dlp direct URL attempt {attempt + 1} fail, retrying in 20s: {last_err}")
        time.sleep(20)
    raise RuntimeError(f"yt-dlp direct URL fail after retries: {last_err}")


# ---------------- CUT + VERTICAL LETTERBOX ----------------
def cut_reel_part(input_src, offset_sec, dur_sec, part_no, total_parts):
    out = os.path.join(WORK, f"part_{part_no}.mp4")
    label = f"Part {part_no}/{total_parts}".replace("'", "")
    vf = (
        "scale=w=1080:h=1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"drawtext=fontfile={FONT}:text='{label}':x=(w-text_w)/2:y=60:"
        "fontsize=56:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=14"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(offset_sec), "-i", input_src, "-t", str(dur_sec),
        "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"ffmpeg cut failed: {(r.stderr or '')[-400:]}")
    return out


# ---------------- IG REELS UPLOAD ----------------
def _ig_upload_once(video_path, caption):
    r = requests.post(
        f"https://graph.facebook.com/{GRAPH_VER}/{IG_USER_ID}/media",
        data={"media_type": "REELS", "upload_type": "resumable",
              "caption": caption, "access_token": IG_ACCESS_TOKEN},
        timeout=60,
    ).json()
    if "id" not in r:
        raise RuntimeError(f"IG container create failed: {r}")
    container_id = r["id"]
    # IMPORTANT: IG kabhi-kabhi container-creation response me hi ek "uri" deta
    # hai jahan actual bytes bhejne hai. Agar hum ye ignore karke khud URL
    # banate hai aur wo mismatch ho jaye, to video kabhi upload hi nahi hota
    # aur container hamesha "IN_PROGRESS" me atka reh jata hai (yahi asli bug tha).
    upload_url = r.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{GRAPH_VER}/{container_id}"
    log(f"IG container created: {container_id} | upload_url={upload_url}")

    size = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        data = f.read()
    up = requests.post(
        upload_url,
        headers={"Authorization": f"OAuth {IG_ACCESS_TOKEN}",
                 "offset": "0", "file_size": str(size),
                 "Content-Type": "application/octet-stream"},
        data=data, timeout=300,
    )
    try:
        up_json = up.json()
    except Exception:
        up_json = {"raw_text": up.text[:300], "status_code": up.status_code}
    log(f"IG binary upload response ({up.status_code}): {up_json}")
    if up.status_code >= 400 or up_json.get("success") is False:
        # Meta ke server-side ka ek jaana-maana transient glitch: "ProcessingFailedError /
        # Request processing failed" — same tarah ki file kabhi chal jati hai kabhi nahi.
        # Isliye caller (ig_upload_reel) isko naye container ke saath retry karega.
        raise RuntimeError(f"IG binary upload failed: {up_json}")

    last_status = {}
    for i in range(60):
        time.sleep(10)
        last_status = requests.get(
            f"https://graph.facebook.com/{GRAPH_VER}/{container_id}",
            params={"fields": "status_code,status", "access_token": IG_ACCESS_TOKEN},
            timeout=30,
        ).json()
        code = last_status.get("status_code")
        if i % 3 == 0:  # har 30 sec me ek baar log karo, spam nahi
            log(f"IG container {container_id} poll {i}: {last_status}")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError(f"IG container processing error: {last_status}")
    else:
        raise RuntimeError(f"IG container processing timeout (last status: {last_status})")

    pub = requests.post(
        f"https://graph.facebook.com/{GRAPH_VER}/{IG_USER_ID}/media_publish",
        data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN},
        timeout=60,
    ).json()
    if "id" not in pub:
        raise RuntimeError(f"IG publish failed: {pub}")
    return pub["id"]


def ig_upload_reel(video_path, caption, max_attempts=3):
    """Meta ke server-side par kabhi-kabhi 'ProcessingFailedError' aata hai
    (transient glitch, same file dobara try karne par chal jati hai). Isliye
    naya container bana ke, thoda ruk ke, dobara try karo."""
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _ig_upload_once(video_path, caption)
        except Exception as e:
            last_err = e
            log(f"IG upload attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                time.sleep(15 * attempt)
    raise last_err


# ---------------- ORCHESTRATION ----------------
def build_caption(d, part_no, total_parts):
    hashtags = clean_hashtags(json.loads(d.get("hashtags_json") or "[]"))
    return f"{d.get('reel_caption') or d.get('title')}\n\nPart {part_no}/{total_parts}\n\n{hashtag_block(hashtags)}"


def main():
    if os.environ.get("YT_COOKIES"):
        with open("cookies.txt", "w") as f:
            f.write(os.environ["YT_COOKIES"])

    if not (IG_USER_ID and IG_ACCESS_TOKEN):
        log("IG credentials nahi mile — kuch karne layak nahi.")
        return 0

    try:
        reels_q, hist = open_sheet()
    except Exception as e:
        tg(f"❌ ReelsQueue sheet open nahi hui: {e}")
        raise

    posted_ig, failed = 0, 0

    for _ in range(REELS_MAX_PER_RUN):
        ig_remaining = ig_quota_remaining()
        if ig_remaining <= 0:
            log("IG ka daily quota khatam — run rok raha hoon.")
            break

        rows = pending_rows(reels_q)
        if not rows:
            log("ReelsQueue khaali hai.")
            break
        row_num, d = rows[0]

        duration = float(d.get("duration") or 0)
        part_sec = int(float(d.get("part_sec") or 85))
        total_parts = int(float(d.get("total_parts") or 1))
        next_index = int(float(d.get("next_index") or 1))

        if next_index > total_parts:
            reels_q.update_cell(row_num, COLS.index("status") + 1, "done")
            continue

        offset = (next_index - 1) * part_sec
        this_dur = min(part_sec, duration - offset)
        if this_dur < 15:
            reels_q.update_cell(row_num, COLS.index("status") + 1, "done")
            continue

        try:
            input_src = get_master_source(d)
            clip = cut_reel_part(input_src, offset, this_dur, next_index, total_parts)
            caption = build_caption(d, next_index, total_parts)

            try:
                ig_id = ig_upload_reel(clip, caption)
                hist.append_row([now_ist(), d["yt_link"], "SUCCESS", ig_id, d["title"], "", "IG_REEL"])
                posted_ig += 1

                new_index = next_index + 1
                reels_q.update_cell(row_num, COLS.index("next_index") + 1, new_index)
                reels_q.update_cell(row_num, COLS.index("hint") + 1, "")
                if new_index > total_parts:
                    reels_q.update_cell(row_num, COLS.index("status") + 1, "done")

            except Exception as e:
                hist.append_row([now_ist(), d["yt_link"], "FAILED", "", d["title"], str(e)[:300], "IG_REEL"])
                tg(f"❌ IG Reel FAILED (Part {next_index}/{total_parts}, {d['title']}):\n{str(e)[:300]}")
                retries = int((d.get("hint") or "0").strip() or 0) + 1
                if retries >= MAX_PART_RETRIES:
                    log(f"Part {next_index} {MAX_PART_RETRIES} baar fail hua — skip kar raha hoon.")
                    tg(f"⚠️ Part {next_index}/{total_parts} ({d['title']}) {MAX_PART_RETRIES} baar fail hua, skip kiya.")
                    reels_q.update_cell(row_num, COLS.index("next_index") + 1, next_index + 1)
                    reels_q.update_cell(row_num, COLS.index("hint") + 1, "")
                else:
                    reels_q.update_cell(row_num, COLS.index("hint") + 1, str(retries))
                failed += 1
                if failed >= 2:
                    break

        except Exception as e:
            failed += 1
            log(f"Part {next_index} processing failed: {e}")
            tg(f"❌ Reel part processing FAILED ({d.get('title')}, part {next_index}):\n{str(e)[:300]}")
            if failed >= 2:
                break
        finally:
            wipe_work()

    if posted_ig:
        tg(f"🎬 Reels run complete: IG={posted_ig} is baar upload hue.")
    elif failed == 0:
        tg("🎬 Reels run: is baar kuch upload nahi hua (quota khatam ya queue khaali).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
