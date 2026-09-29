"""
Trending Shots — Reels distributor (IG Reels + FB Reels)

Kya karta hai (har run):
  1. Google Sheet ki "ReelsQueue" tab se sabse purane pending master video ki
     row uthata hai.
  2. YouTube se us video ka DIRECT stream URL nikalta hai (yt-dlp -g) — poora
     video dobara download nahi hota, seedha us part ka seek+cut hota hai.
  3. Part ko vertical 1080x1920 canvas me letterbox karta hai (upar-niche
     black bar — horizontal video ke liye), aur upar "Part N/Total" likhta hai.
  4. IG Reels + FB Reels dono par upload karta hai — PUBLIC.
  5. IG ka rate limit LIVE check karta hai (content_publishing_limit API se);
     FB Reels ka rate limit apni History sheet se khud track karta hai
     (official docs: 30 reels/24h/page — hum 25 tak hi jaate hai, safety margin).
  6. Jitne parts is run me ho sakein utne karta hai (REELS_MAX_PER_RUN tak),
     baaki agle scheduled run me.
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

from seo_utils import clean_hashtags, hashtag_block

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
SHEET_ID = os.environ.get("SHEET_ID", "")

IG_USER_ID = os.environ.get("IG_USER_ID", "")
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "")
FB_PAGE_TOKEN = os.environ.get("FB_PAGE_TOKEN", "")
GRAPH_VER = "v21.0"

REELS_MAX_PER_RUN = int(os.environ.get("REELS_MAX_PER_RUN", "6"))
FB_REELS_DAILY_CAP = 25          # docs allow 30/24h — safety margin niche rakha
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
    for f in os.listdir(WORK):
        p = os.path.join(WORK, f)
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
        "duration", "part_sec", "total_parts", "next_index", "status", "hint"]


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


def fb_reels_used_last_24h(hist):
    try:
        values = hist.get_all_values()[-500:]  # perf: recent rows kaafi hai
    except Exception:
        return 0
    cutoff = datetime.now(IST) - timedelta(hours=24)
    count = 0
    for row in values:
        if len(row) < 7 or row[6] != "FB_REEL" or row[2] != "SUCCESS":
            continue
        try:
            t = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        except ValueError:
            continue
        if t >= cutoff:
            count += 1
    return count


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


# ---------------- YT DIRECT URL ----------------
def get_direct_url(yt_link):
    cmd = ["yt-dlp", "-f", "b[height<=720]/b", "-g"]
    if os.path.exists("cookies.txt"):
        cmd += ["--cookies", "cookies.txt"]
    cmd.append(yt_link)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    urls = [u for u in (r.stdout or "").strip().splitlines() if u.startswith("http")]
    if r.returncode != 0 or not urls:
        raise RuntimeError(f"yt-dlp direct URL fail: {(r.stderr or '')[-300:]}")
    return urls[0]


# ---------------- CUT + VERTICAL LETTERBOX ----------------
def cut_reel_part(direct_url, offset_sec, dur_sec, part_no, total_parts):
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
        "-ss", str(offset_sec), "-i", direct_url, "-t", str(dur_sec),
        "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"ffmpeg cut failed: {(r.stderr or '')[-400:]}")
    return out


# ---------------- IG REELS UPLOAD ----------------
def ig_upload_reel(video_path, caption):
    r = requests.post(
        f"https://graph.facebook.com/{GRAPH_VER}/{IG_USER_ID}/media",
        data={"media_type": "REELS", "upload_type": "resumable",
              "caption": caption, "access_token": IG_ACCESS_TOKEN},
        timeout=60,
    ).json()
    if "id" not in r:
        raise RuntimeError(f"IG container create failed: {r}")
    container_id = r["id"]

    size = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        data = f.read()
    up = requests.post(
        f"https://rupload.facebook.com/ig-api-upload/{GRAPH_VER}/{container_id}",
        headers={"Authorization": f"OAuth {IG_ACCESS_TOKEN}",
                 "offset": "0", "file_size": str(size)},
        data=data, timeout=300,
    ).json()
    if up.get("success") is False:
        raise RuntimeError(f"IG binary upload failed: {up}")

    for _ in range(30):
        time.sleep(10)
        s = requests.get(
            f"https://graph.facebook.com/{GRAPH_VER}/{container_id}",
            params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN},
            timeout=30,
        ).json()
        code = s.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError(f"IG container processing error: {s}")
    else:
        raise RuntimeError("IG container processing timeout")

    pub = requests.post(
        f"https://graph.facebook.com/{GRAPH_VER}/{IG_USER_ID}/media_publish",
        data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN},
        timeout=60,
    ).json()
    if "id" not in pub:
        raise RuntimeError(f"IG publish failed: {pub}")
    return pub["id"]


# ---------------- FB REELS UPLOAD ----------------
def fb_upload_reel(video_path, title, caption):
    start = requests.post(
        f"https://graph.facebook.com/{GRAPH_VER}/{FB_PAGE_ID}/video_reels",
        data={"upload_phase": "start", "access_token": FB_PAGE_TOKEN},
        timeout=60,
    ).json()
    if "video_id" not in start:
        raise RuntimeError(f"FB reel start failed: {start}")
    video_id = start["video_id"]
    upload_url = start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{GRAPH_VER}/{video_id}"

    size = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        data = f.read()
    up = requests.post(
        upload_url,
        headers={"Authorization": f"OAuth {FB_PAGE_TOKEN}", "offset": "0", "file_size": str(size)},
        data=data, timeout=300,
    ).json()
    if up.get("success") is False:
        raise RuntimeError(f"FB reel binary upload failed: {up}")

    for _ in range(30):
        time.sleep(10)
        s = requests.get(
            f"https://graph.facebook.com/{GRAPH_VER}/{video_id}",
            params={"fields": "status", "access_token": FB_PAGE_TOKEN},
            timeout=30,
        ).json()
        vstatus = (s.get("status") or {}).get("video_status")
        if vstatus == "ready":
            break
        if vstatus in ("error", "upload_failed"):
            raise RuntimeError(f"FB reel processing error: {s}")
    else:
        raise RuntimeError("FB reel processing timeout")

    fin = requests.post(
        f"https://graph.facebook.com/{GRAPH_VER}/{FB_PAGE_ID}/video_reels",
        params={"access_token": FB_PAGE_TOKEN, "video_id": video_id, "upload_phase": "finish",
                "video_state": "PUBLISHED", "description": caption, "title": title[:255]},
        timeout=60,
    ).json()
    if fin.get("success") is False:
        raise RuntimeError(f"FB reel publish failed: {fin}")
    return video_id


# ---------------- ORCHESTRATION ----------------
def build_caption(d, part_no, total_parts):
    hashtags = clean_hashtags(json.loads(d.get("hashtags_json") or "[]"))
    return f"{d.get('reel_caption') or d.get('title')}\n\nPart {part_no}/{total_parts}\n\n{hashtag_block(hashtags)}"


def main():
    if os.environ.get("YT_COOKIES"):
        with open("cookies.txt", "w") as f:
            f.write(os.environ["YT_COOKIES"])

    if not (IG_USER_ID and IG_ACCESS_TOKEN) and not (FB_PAGE_ID and FB_PAGE_TOKEN):
        log("Na IG na FB reels credentials mile — kuch karne layak nahi.")
        return 0

    try:
        reels_q, hist = open_sheet()
    except Exception as e:
        tg(f"❌ ReelsQueue sheet open nahi hui: {e}")
        raise

    posted_ig, posted_fb, failed = 0, 0, 0
    fb_used = fb_reels_used_last_24h(hist)

    for _ in range(REELS_MAX_PER_RUN):
        ig_remaining = ig_quota_remaining() if (IG_USER_ID and IG_ACCESS_TOKEN) else 0
        fb_remaining = max(0, FB_REELS_DAILY_CAP - fb_used) if (FB_PAGE_ID and FB_PAGE_TOKEN) else 0

        if ig_remaining <= 0 and fb_remaining <= 0:
            log("Dono platform ka daily quota khatam — run rok raha hoon.")
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
            direct_url = get_direct_url(d["yt_link"])
            clip = cut_reel_part(direct_url, offset, this_dur, next_index, total_parts)
            caption = build_caption(d, next_index, total_parts)

            if ig_remaining > 0 and IG_USER_ID and IG_ACCESS_TOKEN:
                try:
                    ig_id = ig_upload_reel(clip, caption)
                    hist.append_row([now_ist(), d["yt_link"], "SUCCESS", ig_id, d["title"], "", "IG_REEL"])
                    posted_ig += 1
                except Exception as e:
                    hist.append_row([now_ist(), d["yt_link"], "FAILED", "", d["title"], str(e)[:300], "IG_REEL"])
                    tg(f"❌ IG Reel FAILED (Part {next_index}/{total_parts}, {d['title']}):\n{str(e)[:300]}")

            if fb_remaining > 0 and FB_PAGE_ID and FB_PAGE_TOKEN:
                try:
                    fb_id = fb_upload_reel(clip, d["title"], caption)
                    hist.append_row([now_ist(), d["yt_link"], "SUCCESS", fb_id, d["title"], "", "FB_REEL"])
                    posted_fb += 1
                    fb_used += 1
                except Exception as e:
                    hist.append_row([now_ist(), d["yt_link"], "FAILED", "", d["title"], str(e)[:300], "FB_REEL"])
                    tg(f"❌ FB Reel FAILED (Part {next_index}/{total_parts}, {d['title']}):\n{str(e)[:300]}")

            new_index = next_index + 1
            reels_q.update_cell(row_num, COLS.index("next_index") + 1, new_index)
            if new_index > total_parts:
                reels_q.update_cell(row_num, COLS.index("status") + 1, "done")

        except Exception as e:
            failed += 1
            log(f"Part {next_index} processing failed: {e}")
            tg(f"❌ Reel part processing FAILED ({d.get('title')}, part {next_index}):\n{str(e)[:300]}")
            # yt-dlp/ffmpeg step fail hua (link/network issue) — index badhao mat, agli baar retry hoga
            if failed >= 2:
                break
        finally:
            wipe_work()

    if posted_ig or posted_fb:
        tg(f"🎬 Reels run complete: IG={posted_ig}, FB={posted_fb} is baar upload hue.")
    elif failed == 0:
        tg("🎬 Reels run: is baar kuch upload nahi hua (quota khatam ya queue khaali).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
