"""
Shared helper: master video se ek 10-minute "part" banata hai — upload-ready.
pipeline.py isko ingestion ke turant baad call karta hai; distributor.py sirf
ready files upload karta hai.

Is version me:
  - A/V SYNC FIX: pehle pitch-trick (asetrate=48000*pitch) maan leta tha ki
    source audio 48kHz hai. 44.1kHz audio par ye audio ko ~8.8% tez kar deta
    tha aur video wahi rehta tha (drift). Ab pehle audio 48kHz me resample hota
    hai. Saath me zoompan ka fps ab source ke fps se match karta hai (default
    25 tha, jo 24/30fps source ko retime kar deta tha).
  - Mirror (hflip) wapas, aur zoom pehle se zyada (smooth sine "breathing").
  - Professional captions: blurred frosted background, white text, patla
    black outline. Timing Whisper ke word-timestamps se.
  - "Part X/Y" label: yellow text, safed box, patla black outline.
  - Har part me random fingerprint-variation (brightness/saturation/contrast/
    noise/crop/pitch/speed/volume).
  - Part ke END me ~1 min freeze-frame + Edge-TTS voice recap.
"""
import os
import re
import random
import asyncio
import subprocess

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONTS_DIR = "/usr/share/fonts/truetype/dejavu"
CHANNEL_HANDLE = "@trending_shots_ai"
TTS_VOICE = os.environ.get("TTS_VOICE", "en-US-AriaNeural")

# Zoom: center 1.09x, +-0.03 (yaani 1.06x se 1.12x), ~20 sec ka smooth cycle.
ZOOM_BASE = 1.09
ZOOM_AMP = 0.03
ZOOM_PERIOD_SEC = 20


def dt_escape(s):
    """ffmpeg drawtext ke liye khatarnak characters hata do (colon/%/backslash/quote)."""
    return re.sub(r"[:%\\']", "", str(s))


def _even(n):
    n = int(n)
    return n - 1 if n % 2 else n


# ---------------- PROBES ----------------
def probe_dimensions(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", path],
        capture_output=True, text=True,
    )
    out = (r.stdout or "").strip()
    try:
        w, h = out.split("x")
        return int(w), int(h)
    except ValueError:
        raise RuntimeError(f"Could not probe dimensions of {path}: {out} / {r.stderr}")


def probe_fps(path):
    """(rational_string, float) — jaise ('30000/1001', 29.97). Fail ho to 30."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    s = (r.stdout or "").strip().split("\n")[0]
    try:
        a, b = s.split("/")
        val = float(a) / float(b)
        if 5 <= val <= 120:
            return s, val
    except (ValueError, ZeroDivisionError):
        pass
    return "30/1", 30.0


def probe_media_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    try:
        return float((r.stdout or "0").strip())
    except ValueError:
        return 0.0


# ---------------- RANDOM FINGERPRINT ----------------
def random_fingerprint_params():
    return {
        "brightness": round(random.uniform(-0.03, 0.03), 3),
        "saturation": round(random.uniform(0.95, 1.08), 3),
        "contrast": round(random.uniform(0.97, 1.05), 3),
        "noise": random.randint(6, 14),
        "crop_x": random.randint(2, 10),
        "crop_y": random.randint(2, 10),
        "pitch": round(random.uniform(0.97, 1.03), 3),
        "speed": round(random.uniform(0.98, 1.02), 3),
        "volume": round(random.uniform(0.85, 1.0), 2),
    }


# ---------------- CAPTIONS ----------------
def _w_get(w, key, default=None):
    if isinstance(w, dict):
        return w.get(key, default)
    return getattr(w, key, default)


def build_cues(words, offset, dur, speed, max_chars, tail_guard):
    """Whisper word-timestamps (edited-video timeline) se is part ke liye
    caption cues banata hai. Times part ke apne timeline me convert hote hai:
    (t - offset) / speed — kyunki part me video `speed` factor se retime hota
    hai. Aakhri `tail_guard` seconds me koi caption nahi (freeze-frame outro me
    caption frozen na dikhe)."""
    sel = []
    for w in words or []:
        try:
            ws, we = float(_w_get(w, "start")), float(_w_get(w, "end"))
        except (TypeError, ValueError):
            continue
        txt = str(_w_get(w, "word", "")).strip()
        if not txt:
            continue
        if ws >= offset - 0.05 and ws < offset + dur:
            sel.append((ws, max(we, ws + 0.05), txt))

    chunks, cur = [], []
    for ws, we, txt in sel:
        if cur:
            gap = ws - cur[-1][1]
            cur_text = " ".join(x[2] for x in cur)
            too_long = len(cur_text) + 1 + len(txt) > max_chars
            too_slow = (we - cur[0][0]) > 3.6
            sentence_end = cur_text.endswith((".", "?", "!")) and len(cur) >= 3
            if gap > 0.8 or too_long or too_slow or sentence_end:
                chunks.append(cur)
                cur = []
        cur.append((ws, we, txt))
    if cur:
        chunks.append(cur)

    out_dur = dur / speed
    cues = []
    for i, ch in enumerate(chunks):
        start = ch[0][0]
        end = ch[-1][1] + 0.15
        if i + 1 < len(chunks):
            end = min(end, chunks[i + 1][0][0] - 0.02)
        end = max(end, start + 0.5)
        s = (start - offset) / speed
        e = (end - offset) / speed
        s = max(0.0, s)
        e = min(e, out_dur - tail_guard)
        if e - s < 0.3:
            continue
        text = " ".join(x[2] for x in ch)
        text = re.sub(r"[{}\\]", "", text)
        cues.append((s, e, text))
    return cues


def _ass_time(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def write_ass(path, cues, cw, ch, fs, cx, cy, margin_lr):
    outline = max(2, round(fs * 0.05))
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {cw}\nPlayResY: {ch}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,DejaVu Sans,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,"
        f"100,100,0,0,1,{outline},0,5,{margin_lr},{margin_lr},0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for s, e, text in cues:
        lines.append(
            f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,,"
            f"{{\\an5\\pos({cx},{cy})}}{text}"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n")


def _merge_windows(cues, gap=0.35):
    wins = []
    for s, e, _ in cues:
        if wins and s - wins[-1][1] <= gap:
            wins[-1][1] = max(wins[-1][1], e)
        else:
            wins.append([s, e])
    return wins


# ---------------- MAIN PART BUILDER ----------------
def make_part(input_src, offset_sec, dur_sec, part_no, total_parts, out_path,
              width=None, height=None, words=None):
    """input_src se ek segment kaatta hai: mirror + smooth zoom + color-grade +
    random fingerprint + (optional) professional captions + 'Part N/Total'
    label. Native aspect ratio preserve hoti hai."""
    if not width or not height:
        width, height = probe_dimensions(input_src)
    fps_str, fps_val = probe_fps(input_src)
    label = dt_escape(f"Part {part_no}/{total_parts}")
    p = random_fingerprint_params()

    cw, ch = _even(width - 2 * p["crop_x"]), _even(height - 2 * p["crop_y"])

    # Zoom: smooth sine. `in` = input frame number; fps_val source fps ke
    # barabar hai (zoompan ka default 25 tha — wahi sync bigadta tha).
    zoom_expr = (f"{ZOOM_BASE}+{ZOOM_AMP}*sin(2*PI*in/({fps_val:.4f}*{ZOOM_PERIOD_SEC}))")

    vf = (
        "hflip,"
        f"crop={cw}:{ch}:{p['crop_x']}:{p['crop_y']},"
        f"zoompan=z='{zoom_expr}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"s={cw}x{ch}:fps={fps_str},"
        f"eq=brightness={p['brightness']}:saturation={p['saturation']}:"
        f"contrast={p['contrast']}:gamma=0.95,"
        "vibrance=intensity=0.15,"
        "unsharp=5:5:0.5:5:5:0.0,"
        "curves=master='0/0.03 0.5/0.5 1/0.97',"
        f"noise=alls={p['noise']}:allf=t+u,"
        # setpts se video compress hota hai; fps=30:round=near se frames
        # exactly sahi jagah par aate hai (sirf -r 30 par chhodne se video
        # 2 frame tak late reh jata tha).
        f"setpts=PTS/{p['speed']},fps=30:round=near"
    )

    # ---- Captions (blurred frosted band + white text w/ thin black outline) ----
    caption_note = "no captions"
    ass_path = None
    fs = max(24, int(min(cw, ch) * 0.055))
    sw = _even(int(cw * 0.90))
    sh = _even(int(fs * 2.7))
    sx = (cw - sw) // 2
    sy = ch - 160 - sh
    if words and sy > 0:
        max_chars = max(14, int(0.80 * cw / (0.60 * fs)))
        cues = build_cues(words, offset_sec, dur_sec, p["speed"], max_chars, tail_guard=1.3)
        if cues:
            ass_path = os.path.splitext(out_path)[0] + ".ass"
            write_ass(ass_path, cues, cw, ch, fs, cw // 2, sy + sh // 2, sx + 30)
            wins = _merge_windows(cues)
            enable = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in wins)
            vf += (
                f",split[a][b];[b]crop={sw}:{sh}:{sx}:{sy},gblur=sigma=24,"
                "eq=brightness=-0.10:saturation=0.9[bl];"
                f"[a][bl]overlay={sx}:{sy}:enable='{enable}',"
                f"ass=filename='{ass_path}':fontsdir={FONTS_DIR}"
            )
            caption_note = f"{len(cues)} caption cues"

    # ---- Part label: yellow text, white box, thin black outline ----
    vf += (
        f",drawtext=fontfile={FONT}:text='{label}':x=(w-text_w)/2:y=h-120:"
        "fontsize=44:fontcolor=yellow:borderw=2:bordercolor=black:"
        "box=1:boxcolor=white@1.0:boxborderw=20"
    )

    # ---- Audio: pehle 48kHz (SYNC FIX), phir pitch+speed+volume ----
    atempo = round(p["speed"] / p["pitch"], 4)
    af = (
        "aresample=48000,"
        f"asetrate=48000*{p['pitch']},aresample=48000,"
        f"atempo={atempo},volume={p['volume']},"
        "aresample=async=1:first_pts=0"
    )

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(offset_sec), "-i", input_src, "-t", str(dur_sec),
        "-vf", vf, "-af", af,
        "-r", "30", "-g", "60", "-sc_threshold", "0", "-bf", "2",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main", "-level:v", "4.1",
        "-crf", "23", "-maxrate", "6M", "-bufsize", "12M",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart", out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if ass_path and os.path.exists(ass_path):
        try:
            os.remove(ass_path)
        except OSError:
            pass
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg part cut failed (part {part_no}): {(r.stderr or '')[-500:]}")
    return out_path, (cw, ch), caption_note


# ---------------- RECAP OUTRO (freeze-frame + Edge-TTS voice-over) ----------------
def build_recap_script(description, title):
    desc = re.sub(r"\s+", " ", (description or "").strip())
    return (
        f"{desc} "
        f"Quick recap — that's the story so far on {title}. "
        "If you enjoyed this, make sure to like this video, share it with a "
        "friend, and follow and subscribe to Trending Shots so you never miss "
        "the next part. See you in the next one!"
    )


async def _tts_save_async(text, voice, out_path, rate="-4%"):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(out_path)


def generate_tts(text, out_path, voice=None):
    asyncio.run(_tts_save_async(text, voice or TTS_VOICE, out_path))
    return out_path


def add_recap_outro(part_path, title, description, out_path, width, height):
    """Part ke END me last-frame freeze + TTS voice-over recap + CTA jodta hai.
    Freeze ki duration TTS audio ki actual length ke barabar (20-90s bound)."""
    work_dir = os.path.dirname(out_path) or "."
    base = os.path.splitext(os.path.basename(part_path))[0]

    tts_path = os.path.join(work_dir, f"{base}_tts.mp3")
    generate_tts(build_recap_script(description, title), tts_path)
    freeze_dur = max(20.0, min(90.0, probe_media_duration(tts_path) + 1.5))

    last_frame = os.path.join(work_dir, f"{base}_lastframe.png")
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-1", "-i", part_path,
         "-update", "1", "-q:v", "2", last_frame],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not os.path.exists(last_frame):
        raise RuntimeError(f"Last-frame extract failed: {(r.stderr or '')[-300:]}")

    freeze_clip = os.path.join(work_dir, f"{base}_freeze.mp4")
    vf = (
        f"scale={width}:{height},"
        f"drawtext=fontfile={FONT}:text='Follow {CHANNEL_HANDLE} for more!':"
        "x=(w-text_w)/2:y=h-120:fontsize=40:fontcolor=yellow:borderw=2:bordercolor=black:"
        "box=1:boxcolor=white@1.0:boxborderw=34"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-i", last_frame, "-i", tts_path,
        "-t", str(freeze_dur), "-vf", vf,
        "-r", "30", "-g", "60", "-sc_threshold", "0",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main", "-level:v", "4.1",
        "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
        "-shortest", "-movflags", "+faststart", freeze_clip,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not os.path.exists(freeze_clip):
        raise RuntimeError(f"Freeze-clip encode failed: {(r.stderr or '')[-300:]}")

    list_file = os.path.join(work_dir, f"{base}_concat.txt")
    with open(list_file, "w") as f:
        f.write(f"file '{os.path.abspath(part_path)}'\n")
        f.write(f"file '{os.path.abspath(freeze_clip)}'\n")
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", list_file, "-c", "copy", out_path],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"Concat (part+recap) failed: {(r.stderr or '')[-300:]}")

    for p in (tts_path, last_frame, freeze_clip, list_file):
        try:
            os.remove(p)
        except OSError:
            pass
    return out_path
