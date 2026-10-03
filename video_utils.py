"""
Shared helper: ek master video se ek IG-Reels-ready vertical part banata hai.
pipeline.py isko turant master banne ke baad call karta hai (saare parts ek
saath taiyar kar leta hai) — reels.py sirf ready files upload karta hai, kuch
cut/convert nahi karta.
"""
import re
import subprocess

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def dt_escape(s):
    """ffmpeg drawtext ke liye khatarnak characters hata do (colon/%/backslash/quote)."""
    return re.sub(r"[:%\\']", "", str(s))


def make_reel_part(input_src, offset_sec, dur_sec, part_no, total_parts, title, out_path):
    """input_src (local file) se ek 85-sec (ya jo bhi dur_sec) segment kaatta
    hai, vertical 1080x1920 letterbox + Part number + title + yellow watermark
    ke saath, aur IG Reels ke exact spec ke mutabik encode karta hai."""
    label = dt_escape(f"Part {part_no}/{total_parts}")
    title_display = dt_escape(title)[:42]
    if len(title) > 42:
        title_display += "..."

    vf = (
        "scale=w=1080:h=1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"drawtext=fontfile={FONT}:text='{label}':x=(w-text_w)/2:y=300:"
        "fontsize=52:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=12,"
        f"drawtext=fontfile={FONT}:text='{title_display}':x=(w-text_w)/2:y=380:"
        "fontsize=32:fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=8,"
        f"drawtext=fontfile={FONT}:text='@trending_shots_ai':x=(w-text_w)/2:y=h-100:"
        "fontsize=38:fontcolor=yellow"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(offset_sec), "-i", input_src, "-t", str(dur_sec),
        "-vf", vf,
        # IG Reels spec: fixed frame rate, closed GOP 2-5s, H.264 main profile,
        # 8-bit yuv420p, AAC 48kHz stereo — ye sab IG ke server-side validation
        # errors (ProcessingFailedError / 2207052) ka sabse common karan hai.
        "-r", "30", "-g", "60", "-sc_threshold", "0", "-bf", "2",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main", "-level:v", "4.1",
        "-crf", "23", "-maxrate", "4M", "-bufsize", "8M",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
        "-movflags", "+faststart", out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg cut failed (part {part_no}): {(r.stderr or '')[-400:]}")
    return out_path
