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
  - Naya overlay: full-opacity SAFED background, PEELA text "Part X/Y",
    bottom-center.
"""
import re
import subprocess

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


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


def make_part(input_src, offset_sec, dur_sec, part_no, total_parts, out_path, width=None, height=None):
    """input_src (local file) se ek segment kaatta hai, zoom + color-grade +
    'Part N/Total' overlay ke saath, native aspect ratio preserve karte hue.
    width/height na diye jaye to khud probe kar lega (ek master ke saare parts
    ke liye ek hi baar probe karke pass karna zyada efficient hai)."""
    if not width or not height:
        width, height = probe_dimensions(input_src)
    label = dt_escape(f"Part {part_no}/{total_parts}")

    vf = (
        # Subtle Ken-Burns breathing zoom (d=1 => video ke har frame ke liye
        # exactly 1 output frame, isse audio/video sync kabhi nahi bigadta).
        # s=WxH EXACTLY source ke barabar — isse aspect ratio kabhi nahi badalta.
        f"zoompan=z='if(lte(zoom,1.0),1.03,max(1.0,zoom-0.0008))':d=1:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height},"
        # Color grade: saturation thodi kam, contrast thoda zyada, gamma
        # thoda kam (shadows deep), vibrance zyada, halka sharpen, aur ek
        # filmic "fade" curve (blacks thode lift, whites thode pulled down).
        "eq=saturation=0.92:contrast=1.08:gamma=0.95,"
        "vibrance=intensity=0.15,"
        "unsharp=5:5:0.5:5:5:0.0,"
        "curves=master='0/0.03 0.5/0.5 1/0.97',"
        # Part number overlay — full-opacity white box, yellow text, bottom-center.
        f"drawtext=fontfile={FONT}:text='{label}':x=(w-text_w)/2:y=h-120:"
        "fontsize=44:fontcolor=yellow:box=1:boxcolor=white@1.0:boxborderw=20"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(offset_sec), "-i", input_src, "-t", str(dur_sec),
        "-vf", vf,
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
    return out_path
