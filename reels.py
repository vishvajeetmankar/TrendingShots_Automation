"""
Trending Shots — Reels distributor (IG Reels only)

Kya karta hai (har run):
  1. Google Sheet ke "State" tab se pata karta hai kaunsa series "active" hai
     (ek time pe sirf ek series ke parts jaate hai, mix nahi hote). Agar
     active series khatam/missing hai, turant agla pending series uthata hai.
  2. pipeline.py ne us series ke SAARE 85-sec parts already taiyar karke
     (vertical + watermark + part number, IG-ready) ek GitHub "artifact"
     (parts-<video_id>) me save kiye hote hai — ye sirf wahi download karke
     agla part uthata hai. KOI cutting/converting yahan nahi hoti, isliye na
     YouTube chhuna padta hai na cookies chahiye.
  3. Us part ko Instagram Reels par PUBLIC upload karta hai.
  4. IG ka rate limit LIVE check karta hai (content_publishing_limit API se).
  5. Jitne parts is run me ho sakein utne karta hai (REELS_MAX_PER_RUN tak,
     beech me random 30-180s anti-spam gap), baaki agle scheduled run me.
  6. Ek part fail ho to turant skip NAHI hota — agla run usi part ko phir try
     karega. Sirf bahut zyada baar (MAX_RUN_LEVEL_RETRIES) fail hone par hi
     skip hota hai, taaki ek genuinely-kharab part pura series block na kare.
     Sheet ke "Retry Hint" column me manually "skip" likh ke turant bhi skip
     karaya ja sakta hai.
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

from seo_utils import clean_hashtags, hashtag_block

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
SHEET_ID = os.environ.get("SHEET_ID", "")

IG_USER_ID = os.environ.get("IG_USER_ID", "")
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
GRAPH_VER = "v21.0"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")

REELS_MAX_PER_RUN = int(os.environ.get("REELS_MAX_PER_RUN", "4"))
MAX_RUN_LEVEL_RETRIES = 8
IG_SAFETY_MARGIN = 2

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
    """Sirf current series ke extracted parts cache ke alawa sab saaf karo,
    taaki isi run ke andar baar-baar wahi artifact dobara download na ho."""
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
    reels_q = sh.worksheet("ReelsQueue")
    try:
        hist = sh.worksheet("History")
    except gspread.WorksheetNotFound:
        hist = sh.add_worksheet("History", 2000, 7)
        hist.append_row(["Time (IST)", "Link", "Status", "YouTube Link", "Title", "Error", "Platform"])
    try:
        state = sh.worksheet("State")
    except gspread.WorksheetNotFound:
        state = sh.add_worksheet("State", 10, 2)
        state.append_row(["key", "value"])
    return reels_q, hist, state


def get_state(state_ws):
    rows = state_ws.get_all_values()[1:]
    return {r[0]: r[1] for r in rows if r and r[0]}


def set_state(state_ws, key, value):
    cell = state_ws.find(key, in_column=1)
    if cell:
        state_ws.update_cell(cell.row, 2, value)
    else:
        state_ws.append_row([key, value])


COLS = ["added", "yt_id", "yt_link", "title", "reel_caption", "hashtags_json",
        "duration", "part_sec", "total_parts", "next_index", "status", "hint",
        "source_url"]


def pending_rows(reels_q):
    """Returns list of (row_number, dict) for rows with status == 'pending', oldest first."""
    values = reels_q.get_all_values()
    out = []
    for i, row in enumerate(values[1:], start=2):
        row = row + [""] * (len(COLS) - len(row))
        d = dict(zip(COLS, row))
        if d.get("status", "").strip().lower() == "pending" and d.get("yt_id"):
            out.append((i, d))
    return out


def find_row_by_yt_id(reels_q, yt_id):
    """Row number + dict for a specific yt_id, chahe koi bhi status ho. None agar nahi mila."""
    values = reels_q.get_all_values()
    for i, row in enumerate(values[1:], start=2):
        row = row + [""] * (len(COLS) - len(row))
        d = dict(zip(COLS, row))
        if d.get("yt_id") == yt_id:
            return i, d
    return None, None


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


# ---------------- READY-MADE PARTS ARTIFACT ----------------
_parts_cache = {}  # yt_id -> {part_no: local_path}, isi run me dobara fetch na ho


def fetch_parts_artifact(yt_id):
    """pipeline.py ne banaye hue 'parts-<yt_id>' artifact ko ek baar download
    karke extract karta hai, aur {part_number: file_path} dict return karta
    hai. Isi run ke andar cached rehta hai taaki baar-baar download na ho."""
    if yt_id in _parts_cache:
        return _parts_cache[yt_id]
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        log("GITHUB_TOKEN/GITHUB_REPOSITORY nahi mile — parts artifact fetch nahi ho sakta.")
        _parts_cache[yt_id] = {}
        return {}

    name = f"parts-{yt_id}"
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
            _parts_cache[yt_id] = {}
            return {}
        art = sorted(arts, key=lambda a: a["created_at"], reverse=True)[0]
        dl = requests.get(art["archive_download_url"], headers=headers, timeout=600)
        dl.raise_for_status()

        extract_dir = os.path.join(WORK, "parts", yt_id)
        os.makedirs(extract_dir, exist_ok=True)
        zpath = os.path.join(WORK, f"{yt_id}_parts.zip")
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
        _parts_cache[yt_id] = parts
        return parts
    except Exception as e:
        log(f"Artifact fetch fail ({name}): {e}")
        _parts_cache[yt_id] = {}
        return {}


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
        if i % 3 == 0:
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


def ig_upload_reel(video_path, caption, max_attempts=4):
    """Meta ke server-side par kabhi-kabhi 'ProcessingFailedError' aata hai
    (transient glitch — same file kabhi reject hoti hai kabhi accept). Isliye
    naya container bana ke, thoda zyada ruk ke, dobara try karo."""
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _ig_upload_once(video_path, caption)
        except Exception as e:
            last_err = e
            log(f"IG upload attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                time.sleep(30 * attempt)
    raise last_err


# ---------------- ORCHESTRATION ----------------
def build_caption(d, part_no, total_parts):
    hashtags = clean_hashtags(json.loads(d.get("hashtags_json") or "[]"))
    return f"{d.get('reel_caption') or d.get('title')}\n\nPart {part_no}/{total_parts}\n\n{hashtag_block(hashtags)}"


def main():
    if not (IG_USER_ID and IG_ACCESS_TOKEN):
        log("IG credentials nahi mile — kuch karne layak nahi.")
        return 0

    try:
        reels_q, hist, state_ws = open_sheet()
    except Exception as e:
        tg(f"❌ ReelsQueue sheet open nahi hui: {e}")
        raise

    state = get_state(state_ws)
    active_yt_id = state.get("active_yt_id", "")
    row_num, d = (find_row_by_yt_id(reels_q, active_yt_id) if active_yt_id else (None, None))

    if not d or d.get("status", "").strip().lower() == "done":
        rows = pending_rows(reels_q)
        if not rows:
            set_state(state_ws, "active_yt_id", "")
            tg("📭 Koi pending video nahi hai ReelsQueue me.")
            return 0
        row_num, d = rows[0]
        active_yt_id = d["yt_id"]
        set_state(state_ws, "active_yt_id", active_yt_id)
        log(f"Active series set: {active_yt_id} ({d.get('title')})")

    posted_ig, failed = 0, 0

    for i in range(REELS_MAX_PER_RUN):
        ig_remaining = ig_quota_remaining()
        if ig_remaining <= 0:
            log("IG ka daily quota khatam — run rok raha hoon.")
            break

        row_num, d = find_row_by_yt_id(reels_q, active_yt_id)
        if not d or d.get("status", "").strip().lower() == "done":
            log("Aaj ka series poora ho gaya.")
            break

        total_parts = int(float(d.get("total_parts") or 1))
        next_index = int(float(d.get("next_index") or 1))

        if next_index > total_parts:
            reels_q.update_cell(row_num, COLS.index("status") + 1, "done")
            break

        hint_val = (d.get("hint") or "").strip()

        if hint_val.lower() == "skip":
            log(f"Part {next_index} manually 'skip' marked hai sheet me — aage badh raha hoon.")
            tg(f"⏭️ Part {next_index}/{total_parts} ({d['title']}) manually skip kiya gaya.")
            new_index = next_index + 1
            reels_q.update_cell(row_num, COLS.index("next_index") + 1, new_index)
            reels_q.update_cell(row_num, COLS.index("hint") + 1, "")
            if new_index > total_parts:
                reels_q.update_cell(row_num, COLS.index("status") + 1, "done")
            continue

        parts = fetch_parts_artifact(active_yt_id)
        clip = parts.get(next_index)
        if not clip:
            log(f"Part {next_index} ka ready file artifact me nahi mila — agla run try karega.")
            tg(f"⚠️ Part {next_index}/{total_parts} ({d['title']}) ka ready file abhi nahi mila "
               f"(shayad pipeline.py abhi chal raha hai ya artifact expire ho gaya).")
            failed += 1
            if failed >= 2:
                break
            continue

        try:
            caption = build_caption(d, next_index, total_parts)
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
            retries = int(hint_val) + 1 if hint_val.isdigit() else 1

            if retries >= MAX_RUN_LEVEL_RETRIES:
                log(f"Part {next_index} {retries} alag runs me fail ho chuka — skip kar raha hoon.")
                tg(f"⚠️ Part {next_index}/{total_parts} ({d['title']}) {retries} baar fail hua, "
                   f"ye part genuinely kharab lag raha hai — skip kiya, series aage badh raha hai.")
                new_index = next_index + 1
                reels_q.update_cell(row_num, COLS.index("next_index") + 1, new_index)
                reels_q.update_cell(row_num, COLS.index("hint") + 1, "")
                if new_index > total_parts:
                    reels_q.update_cell(row_num, COLS.index("status") + 1, "done")
            else:
                reels_q.update_cell(row_num, COLS.index("hint") + 1, str(retries))
                tg(f"❌ IG Reel FAILED (Part {next_index}/{total_parts}, {d['title']}, "
                   f"ab tak {retries}/{MAX_RUN_LEVEL_RETRIES} baar fail) — agla run phir try karega:\n{str(e)[:300]}")
            failed += 1
            if failed >= 2:
                break

        if i < REELS_MAX_PER_RUN - 1:
            gap = random.randint(30, 180)
            log(f"Agle part se pehle {gap}s ruk raha hoon (anti-spam pacing)...")
            time.sleep(gap)

    if posted_ig:
        tg(f"🎬 Reels run complete: IG={posted_ig} is baar upload hue.")
    elif failed == 0:
        tg("🎬 Reels run: is baar kuch upload nahi hua (quota khatam ya series poora ho chuka).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
