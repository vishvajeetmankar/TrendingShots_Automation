"""
Shared helper: master video se ek 10-minute "part" banata hai — upload-ready.
pipeline.py isko ingestion ke turant baad call karta hai (saare parts ek saath
taiyar kar leta hai); distributor.py sirf ready files upload karta hai.

Is version me (purane 85-sec/vertical-letterbox version se badlav):
  - Native aspect ratio preserve hoti hai — agar source horizontal hai to
    horizontal hi rehta hai, vertical hai to vertical hi. Koi forced
    letterbox/pillarbox nahi.
  - Mirror (hflip) hata diya gaya.
  - Subtle "breathing" zoom in/out (Ken Burns style) — barely visible.
  - Naya color grade: saturation thodi kam, contrast/sharpness/vibrance thoda
    zyada, shadows thode deep, halka filmic fade.
  - HAR PART ke liye RANDOM micro-variation (brightness/saturation/contrast/
    noise/crop/pitch/speed/volume) — har part thoda unique dikhta/sunta hai,
    taaki YouTube/Meta ke "duplicate/templated content" classifiers ko
    repeated-pattern na lage.
  - Naya overlay: full-opacity SAFED background, PEELA text "Part X/Y",
    bottom-center.
  - Har part ke END me ek ~1-minute "freeze frame + voice recap" outro —
    last frame freeze hoke, Edge-TTS (Microsoft) se description ka voice-over
    + like/share/follow/subscribe CTA bolta hai.
"""
import os
import re
import random
import asyncio
import subprocess

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
CHANNEL_HANDLE = "@trending_shots_ai"
TTS_VOICE = os.environ.get("TTS_VOICE", "en-US-AriaNeural")  # natural/warm English voice


def dt_escape(s):
    """ffmpeg drawtext ke liye khatarnak characters hata do (colon/%/backslash/quote)."""
    return re.sub(r"[:%\\']", "", str(s))


def probe_dimensions(path):
    """Video ki actual width/height nikalta hai — zoompan ke 's' parameter ko
    source ke EXACT dimensions chahiye, warna wo aspect ratio badal deta hai
    (jaise vertical ko horizontal me force kar dena — isliye ye zaroori hai)."""
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


def _even(n):
    n = int(n)
    return n - 1 if n % 2 else n


def random_fingerprint_params():
    """Har part ke liye alag-alag chhote random variations — taaki consecutive
    parts bilkul identical-pattern na lagein (YouTube/Meta ke duplicate/
    templated-content classifiers ke liye)."""
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


def make_part(input_src, offset_sec, dur_sec, part_no, total_parts, out_path, width=None, height=None):
    """input_src (local file) se ek segment kaatta hai, zoom + color-grade +
    random fingerprint-variation + 'Part N/Total' overlay ke saath, native
    aspect ratio preserve karte hue. width/height na diye jaye to khud probe
    kar lega (ek master ke saare parts ke liye ek hi baar probe karke pass
    karna zyada efficient hai)."""
    if not width or not height:
        width, height = probe_dimensions(input_src)
    label = dt_escape(f"Part {part_no}/{total_parts}")
    p = random_fingerprint_params()

    # Crop thode pixels har side se (fingerprint ke liye) — baaki saare filters
    # isi NAYE (chhote) size par chalte hai, isliye zoompan ka 's' bhi isi se
    # match karta hai.
    cw, ch = _even(width - 2 * p["crop_x"]), _even(height - 2 * p["crop_y"])

    vf = (
        f"crop={cw}:{ch}:{p['crop_x']}:{p['crop_y']},"
        # Subtle Ken-Burns breathing zoom (d=1 => video ke har frame ke liye
        # exactly 1 output frame, isse audio/video sync kabhi nahi bigadta).
        f"zoompan=z='if(lte(zoom,1.0),1.03,max(1.0,zoom-0.0008))':d=1:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={cw}x{ch},"
        # Color grade: fixed baseline (gamma/vibrance/sharpen/fade-curve) +
        # is part ke apne random brightness/saturation/contrast.
        f"eq=brightness={p['brightness']}:saturation={p['saturation']}:"
        f"contrast={p['contrast']}:gamma=0.95,"
        "vibrance=intensity=0.15,"
        "unsharp=5:5:0.5:5:5:0.0,"
        "curves=master='0/0.03 0.5/0.5 1/0.97',"
        f"noise=alls={p['noise']}:allf=t+u,"
        # Part number overlay — full-opacity white box, yellow text, bottom-center.
        f"drawtext=fontfile={FONT}:text='{label}':x=(w-text_w)/2:y=h-120:"
        "fontsize=44:fontcolor=yellow:box=1:boxcolor=white@1.0:boxborderw=20,"
        f"setpts=PTS/{p['speed']}"
    )
    # Audio: pitch shift (asetrate+aresample, phir atempo se original tempo
    # restore karke upar se apna speed-variation + volume laga do).
    atempo = round(p["speed"] / p["pitch"], 4)
    af = (
        f"asetrate=48000*{p['pitch']},aresample=48000,"
        f"atempo={atempo},volume={p['volume']}"
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
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg part cut failed (part {part_no}): {(r.stderr or '')[-400:]}")
    return out_path, (cw, ch)


# ---------------- RECAP OUTRO (freeze-frame + Edge-TTS voice-over) ----------------
def probe_media_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    try:
        return float((r.stdout or "0").strip())
    except ValueError:
        return 0.0


def build_recap_script(description, title):
    """Description ko hi voice-over script bana deta hai (jaisa user ne bola —
    naya analysis generate karne ki zaroorat nahi), plus closing like/share/
    follow/subscribe CTA."""
    desc = re.sub(r"\s+", " ", (description or "").strip())
    script = (
        f"{desc} "
        f"Quick recap — that's the story so far on {title}. "
        "If you enjoyed this, make sure to like this video, share it with a "
        "friend, and follow and subscribe to Trending Shots so you never miss "
        "the next part. See you in the next one!"
    )
    return script


async def _tts_save_async(text, voice, out_path, rate="-4%"):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(out_path)


def generate_tts(text, out_path, voice=None):
    """Microsoft Edge TTS se narration banata hai (asli neural voice — natural
    sunta hai). Thoda slow rate (-4%) rakha hai taaki zyada natural/calm lage."""
    voice = voice or TTS_VOICE
    asyncio.run(_tts_save_async(text, voice, out_path))
    return out_path


def add_recap_outro(part_path, title, description, out_path, width, height):
    """Part ke END me ek freeze-frame (last frame hold) + TTS voice-over recap
    + like/share/follow/subscribe CTA jodta hai. Freeze ki duration TTS audio
    ki actual length ke barabar hoti hai (safe floor/ceiling ke saath)."""
    work_dir = os.path.dirname(out_path) or "."
    base = os.path.splitext(os.path.basename(part_path))[0]

    tts_path = os.path.join(work_dir, f"{base}_tts.mp3")
    script = build_recap_script(description, title)
    generate_tts(script, tts_path)
    tts_dur = probe_media_duration(tts_path)
    freeze_dur = max(20.0, min(90.0, tts_dur + 1.5))  # ~1 min target, sane bounds

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
        "x=(w-text_w)/2:y=h-120:fontsize=40:fontcolor=yellow:box=1:boxcolor=white@1.0:boxborderw=34"
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
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
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
