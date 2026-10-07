"""
Trending Shots — Distributor (YouTube PUBLIC + Facebook Page)

Kya karta hai (har run):
  1. "State" tab se pata karta hai aaj kitne parts ja chuke hai (quota_used)
     aur kaunsa series "active" hai. Roz sirf DAILY_CAP (5) parts jaate hai —
     YouTube + Facebook dono par (har part dono jagah jata hai, alag-alag
     count nahi hota).
  2. Active series khatam/missing ho to turant agla pending series uthata hai
     (chahe manually delete kiya ho, chahe poora ho chuka ho) — kabhi din
     bhar ke liye atakta nahi.
  3. pipeline.py ne banaye hue ready parts ek GitHub "artifact"
     (parts-<series_id>) se uthata hai — koi cutting/converting yahan nahi.
  4. Har part YouTube par PUBLIC video, aur Facebook Page par normal video
     post ke roop me upload hota hai.
  5. Ek part fail ho to turant skip nahi hota — kai baar (cross-run) retry
     hota hai, bahut zyada baar fail hone par hi (MAX_RUN_LEVEL_RETRIES)
     skip hota hai taaki series hamesha ke liye block na ho. Sheet ke "Retry
     Hint" column me manually "skip" likh ke turant bhi skip karaya ja sakta hai.
"""
import os
import sys
import json
import time
import random
import shutil
import zipfile
from datetime import datetime, timezone, timedelta

import requests
import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from seo_utils import clean_hashtags, hashtag_block

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
SHEET_ID = os.environ.get("SHEET_ID", "")

FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "")
FB_PAGE_TOKEN = os.environ.get("FB_PAGE_TOKEN", "")
GRAPH_VER = "v21.0"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

DAILY_CAP = int(os.environ.get("DAILY_CAP", "5"))          # roz max itne parts (YT+FB dono par)
PARTS_PER_RUN = int(os.environ.get("PARTS_PER_RUN", "2"))   # ek run me max itne try
MAX_RUN_LEVEL_RETRIES = 4

WORK = "dist_work"
os.makedirs(WORK, exist_ok=True)
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def today_ist():
    return datetime.now(IST).strftime("%Y-%m-%d")


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
    keep_dir = os.path.abspath(os.path.join(WORK, "parts"))
    for f in os.listdir(WORK):
        p = os.path.join(WORK, f)
        if os.path.abspath(p) == keep_dir:
            continue
        try:
            os.remove(p) if os.path.isfile(p) else shutil.rmtree(p)
        except OSError:
            pass


# ---------------- SHEET ----------------
def open_sheet():
    gc = gspread.service_account_from_dict(json.loads(os.environ["GCP_SA_JSON"]))
    sh = gc.open_by_key(SHEET_ID)
    parts_q = sh.worksheet("PartsQueue")
    try:
        hist = sh.worksheet("History")
    except gspread.WorksheetNotFound:
        hist = sh.add_worksheet("History", 2000, 7)
        hist.append_row(["Time (IST)", "Link", "Status", "Video Link", "Title", "Error", "Platform"])
    try:
        state = sh.worksheet("State")
    except gspread.WorksheetNotFound:
        state = sh.add_worksheet("State", 10, 2)
        state.append_row(["key", "value"])
    return parts_q, hist, state


def get_state(state_ws):
    rows = state_ws.get_all_values()[1:]
    return {r[0]: r[1] for r in rows if r and r[0]}


def set_state(state_ws, key, value):
    cell = state_ws.find(key, in_column=1)
    if cell:
        state_ws.update_cell(cell.row, 2, value)
    else:
        state_ws.append_row([key, value])


COLS = ["added", "series_id", "title", "description", "hashtags_json", "tags_json",
        "duration", "part_sec", "total_parts", "next_index", "status", "hint"]


def pending_rows(parts_q):
    values = parts_q.get_all_values()
    out = []
    for i, row in enumerate(values[1:], start=2):
        row = row + [""] * (len(COLS) - len(row))
        d = dict(zip(COLS, row))
        if d.get("status", "").strip().lower() == "pending" and d.get("series_id"):
            out.append((i, d))
    return out


def find_row_by_series_id(parts_q, series_id):
    values = parts_q.get_all_values()
    for i, row in enumerate(values[1:], start=2):
        row = row + [""] * (len(COLS) - len(row))
        d = dict(zip(COLS, row))
        if d.get("series_id") == series_id:
            return i, d
    return None, None


# ---------------- READY-MADE PARTS ARTIFACT ----------------
_parts_cache = {}


def fetch_parts_artifact(series_id):
    if series_id in _parts_cache:
        return _parts_cache[series_id]
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        log("GITHUB_TOKEN/GITHUB_REPOSITORY nahi mile — parts artifact fetch nahi ho sakta.")
        _parts_cache[series_id] = {}
        return {}

    name = f"parts-{series_id}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/artifacts",
            headers=headers, params={"name": name, "per_page": 10}, timeout=30,
        )
        r.raise_for_status()
        arts = [a for a in r.json().get("artifacts", []) if not a.get("expired")]
        if not arts:
            log(f"Artifact '{name}' nahi mila (abhi bana nahi ya expire ho gaya).")
            _parts_cache[series_id] = {}
            return {}
        art = sorted(arts, key=lambda a: a["created_at"], reverse=True)[0]
        dl = requests.get(art["archive_download_url"], headers=headers, timeout=600)
        dl.raise_for_status()

        extract_dir = os.path.join(WORK, "parts", series_id)
        os.makedirs(extract_dir, exist_ok=True)
        zpath = os.path.join(WORK, f"{series_id}_parts.zip")
        with open(zpath, "wb") as f:
            f.write(dl.content)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(extract_dir)
        os.remove(zpath)

        parts = {}
        for fn in os.listdir(extract_dir):
            if fn.startswith("part_") and fn.endswith(".mp4"):
                try:
                    num = int(fn[5:8])
                    parts[num] = os.path.join(extract_dir, fn)
                except ValueError:
                    continue
        log(f"Artifact '{name}' se {len(parts)} parts mil gaye.")
        _parts_cache[series_id] = parts
        return parts
    except Exception as e:
        log(f"Artifact fetch fail ({name}): {e}")
        _parts_cache[series_id] = {}
        return {}


# ---------------- YOUTUBE UPLOAD (PUBLIC) ----------------
def yt_service():
    info = json.loads(os.environ["YT_TOKEN_JSON"])
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if not creds.valid:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload_youtube(video_path, title, description, tags):
    yt = yt_service()
    body = {
        "snippet": {"title": title[:100], "description": description, "tags": tags, "categoryId": "24"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(video_path, chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response, retries = None, 0
    while response is None:
        try:
            st, response = req.next_chunk()
        except HttpError as e:
            content = (e.content or b"").decode("utf-8", "ignore")
            if "quotaExceeded" in content or "uploadLimitExceeded" in content:
                raise RuntimeError(f"YouTube quota/upload limit khatam: {content[:200]}")
            if e.resp.status in (500, 502, 503, 504) and retries < 5:
                retries += 1
                time.sleep(5 * retries)
                continue
            raise
    return response["id"], f"https://youtu.be/{response['id']}"


# ---------------- FACEBOOK UPLOAD (normal page video post) ----------------
def fb_upload_part(path, title, description):
    if not FB_PAGE_ID or not FB_PAGE_TOKEN:
        return None, None
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
              "access_token": FB_PAGE_TOKEN, "title": title[:255], "description": description},
        timeout=60,
    ).json()
    if fin.get("success") is False:
        raise RuntimeError(f"FB finish failed: {fin}")

    permalink = f"https://www.facebook.com/{video_id}"
    for _ in range(6):
        time.sleep(5)
        try:
            info = requests.get(
                f"https://graph.facebook.com/{GRAPH_VER}/{video_id}",
                params={"fields": "permalink_url", "access_token": FB_PAGE_TOKEN},
                timeout=30,
            ).json()
            if info.get("permalink_url"):
                permalink = f"https://www.facebook.com{info['permalink_url']}"
                break
        except Exception:
            pass
    return video_id, permalink


# ---------------- ORCHESTRATION ----------------
def build_caption(d, part_no, total_parts):
    hashtags = clean_hashtags(json.loads(d.get("hashtags_json") or "[]"))
    title = f"{d.get('title')} — Part {part_no}/{total_parts}"
    desc = f"{d.get('description') or ''}\n\nPart {part_no}/{total_parts}"
    if hashtags:
        desc += "\n\n" + hashtag_block(hashtags)
    return title, desc


def main():
    try:
        parts_q, hist, state_ws = open_sheet()
    except Exception as e:
        tg(f"❌ PartsQueue sheet open nahi hui: {e}")
        raise

    state = get_state(state_ws)
    if state.get("quota_date") != today_ist():
        set_state(state_ws, "quota_date", today_ist())
        set_state(state_ws, "quota_used", "0")
        quota_used = 0
    else:
        quota_used = int(state.get("quota_used") or 0)

    if quota_used >= DAILY_CAP:
        log(f"Aaj ka quota ({DAILY_CAP}) pura ho chuka hai.")
        return 0

    active_series_id = state.get("active_series_id", "")
    row_num, d = (find_row_by_series_id(parts_q, active_series_id) if active_series_id else (None, None))

    if not d or d.get("status", "").strip().lower() == "done":
        rows = pending_rows(parts_q)
        if not rows:
            set_state(state_ws, "active_series_id", "")
            tg("📭 Koi pending series nahi hai PartsQueue me.")
            return 0
        row_num, d = rows[0]
        active_series_id = d["series_id"]
        set_state(state_ws, "active_series_id", active_series_id)
        log(f"Active series set: {active_series_id} ({d.get('title')})")

    posted, failed = 0, 0
    run_cap = min(PARTS_PER_RUN, DAILY_CAP - quota_used)

    for i in range(run_cap):
        row_num, d = find_row_by_series_id(parts_q, active_series_id)
        if not d or d.get("status", "").strip().lower() == "done":
            log("Active series poora ho gaya is run me.")
            break

        total_parts = int(float(d.get("total_parts") or 1))
        next_index = int(float(d.get("next_index") or 1))

        if next_index > total_parts:
            parts_q.update_cell(row_num, COLS.index("status") + 1, "done")
            break

        hint_val = (d.get("hint") or "").strip()

        if hint_val.lower() == "skip":
            log(f"Part {next_index} manually 'skip' marked hai — aage badh raha hoon.")
            tg(f"⏭️ Part {next_index}/{total_parts} ({d['title']}) manually skip kiya gaya.")
            new_index = next_index + 1
            parts_q.update_cell(row_num, COLS.index("next_index") + 1, new_index)
            parts_q.update_cell(row_num, COLS.index("hint") + 1, "")
            if new_index > total_parts:
                parts_q.update_cell(row_num, COLS.index("status") + 1, "done")
            continue

        parts = fetch_parts_artifact(active_series_id)
        clip = parts.get(next_index)
        if not clip:
            log(f"Part {next_index} ka ready file artifact me nahi mila — agla run try karega.")
            tg(f"⚠️ Part {next_index}/{total_parts} ({d['title']}) ka ready file abhi nahi mila.")
            failed += 1
            if failed >= 2:
                break
            continue

        title, desc = build_caption(d, next_index, total_parts)
        tags = json.loads(d.get("tags_json") or "[]")

        yt_ok, fb_ok = False, False
        yt_link, fb_link = "", ""
        errs = []

        try:
            yt_id, yt_link = upload_youtube(clip, title, desc, tags)
            hist.append_row([now_ist(), active_series_id, "SUCCESS", yt_link, title, "", "YT_PART"])
            yt_ok = True
        except Exception as e:
            hist.append_row([now_ist(), active_series_id, "FAILED", "", title, str(e)[:300], "YT_PART"])
            errs.append(f"YT: {str(e)[:200]}")

        try:
            fb_id, fb_link = fb_upload_part(clip, title, desc)
            if fb_id:
                hist.append_row([now_ist(), active_series_id, "SUCCESS", fb_link, title, "", "FB_PART"])
                fb_ok = True
        except Exception as e:
            hist.append_row([now_ist(), active_series_id, "FAILED", "", title, str(e)[:300], "FB_PART"])
            errs.append(f"FB: {str(e)[:200]}")

        if yt_ok or fb_ok:
            posted += 1
            quota_used += 1
            set_state(state_ws, "quota_used", str(quota_used))
            new_index = next_index + 1
            parts_q.update_cell(row_num, COLS.index("next_index") + 1, new_index)
            parts_q.update_cell(row_num, COLS.index("hint") + 1, "")
            if new_index > total_parts:
                parts_q.update_cell(row_num, COLS.index("status") + 1, "done")
            note = []
            if yt_ok:
                note.append(f"📺 {yt_link}")
            if fb_ok:
                note.append(f"📘 {fb_link}")
            if errs:
                note.append("⚠️ " + " | ".join(errs))
            tg(f"✅ Part {next_index}/{total_parts} ({d['title']}) posted:\n" + "\n".join(note))
        else:
            retries = int(hint_val) + 1 if hint_val.isdigit() else 1
            if retries >= MAX_RUN_LEVEL_RETRIES:
                log(f"Part {next_index} {retries} baar fail ho chuka — skip kar raha hoon.")
                tg(f"⚠️ Part {next_index}/{total_parts} ({d['title']}) {retries} baar fail hua, skip kiya.\n"
                   + " | ".join(errs))
                new_index = next_index + 1
                parts_q.update_cell(row_num, COLS.index("next_index") + 1, new_index)
                parts_q.update_cell(row_num, COLS.index("hint") + 1, "")
                if new_index > total_parts:
                    parts_q.update_cell(row_num, COLS.index("status") + 1, "done")
            else:
                parts_q.update_cell(row_num, COLS.index("hint") + 1, str(retries))
                tg(f"❌ Part {next_index}/{total_parts} ({d['title']}) FAILED "
                   f"(ab tak {retries}/{MAX_RUN_LEVEL_RETRIES}):\n" + " | ".join(errs))
            failed += 1
            if failed >= 2:
                break

        wipe_work()
        if i < run_cap - 1:
            gap = random.randint(15, 60)
            log(f"Agle part se pehle {gap}s ruk raha hoon...")
            time.sleep(gap)

    if posted:
        tg(f"🎬 Distributor run complete: {posted} parts posted (aaj total {quota_used}/{DAILY_CAP}).")
    elif failed == 0:
        tg("🎬 Distributor run: is baar kuch upload nahi hua (quota khatam ya series poora ho chuka).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
