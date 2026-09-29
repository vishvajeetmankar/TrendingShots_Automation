"""
Shared SEO helpers — pipeline.py aur reels.py dono isse import karte hai.

Goal: AI kabhi-kabhi ajeeb/invented hashtags bana deta hai (jo kabhi search hi
nahi hote). Isko rokne ke 2 layers hai:
  1. STRICT PATTERN FILTER — hashtag = ek clean lowercase word (spaces/symbols
     nahi), tag = 1-4 simple lowercase words. Isse garbled/gibberish reject ho
     jata hai.
  2. CURATED FALLBACK POOL — ye asli, commonly-used English hashtags/keywords
     hai (reels/story/animation niche ke liye). AI ke saaf-filtered output ko
     inhi ke saath merge karke final list banti hai, taaki list hamesha
     "real" tags se bhari rahe.
Note: ye "abhi live trending kya hai" guarantee nahi karta — uske liye ek paid
trend-API (jaise Google Trends scraper ya TikTok/IG trend tool) chahiye hoga.
Ye sirf invented/nonsense tags ka risk kam karta hai.
"""
import re

CURATED_HASHTAGS = [
    "reels", "reelsinstagram", "instareels", "reelitfeelit", "trending",
    "viral", "viralreels", "explorepage", "explore", "fyp", "foryou",
    "foryoupage", "reelsvideo", "reelsdaily", "trendingreels", "viralvideo",
    "shortvideo", "story", "storytime", "animation", "cartoon", "2danimation",
    "animatedstory", "moralstory", "suspense", "suspensestory", "thriller",
    "mystery", "mysterystory", "drama", "dramaseries", "webseries",
    "emotionalstory", "plottwist", "cliffhanger", "mustwatch", "watchtillend",
    "hindistory", "animatedseries", "shortstory", "binge",
]

CURATED_TAGS = [
    "hindi story", "animated story", "moral story", "suspense story",
    "mystery story", "thriller story", "drama series", "web series",
    "story time", "plot twist", "emotional story", "trending reels",
    "viral reels", "reels india", "cartoon story", "2d animation",
    "animated series", "hindi web series", "short story", "must watch",
]

_HASHTAG_RE = re.compile(r"^[a-z0-9]{2,25}$")
_TAG_RE = re.compile(r"^[a-z0-9]+( [a-z0-9]+){0,3}$")


def clean_hashtags(raw_list, limit=20):
    """AI ke hashtags ko filter karta hai, phir curated pool se fill karta hai."""
    out = []
    for h in raw_list or []:
        h = str(h).strip().lower().lstrip("#").replace("_", "")
        if _HASHTAG_RE.match(h) and h not in out:
            out.append(h)
    for h in CURATED_HASHTAGS:
        if len(out) >= limit:
            break
        if h not in out:
            out.append(h)
    return out[:limit]


def clean_tags(raw_list, limit=20):
    """AI ke SEO tags (keywords) ko filter karta hai, phir curated pool se fill."""
    out = []
    for t in raw_list or []:
        t = " ".join(str(t).strip().lower().replace("#", "").split())
        if _TAG_RE.match(t) and len(t) <= 40 and t not in out:
            out.append(t)
    for t in CURATED_TAGS:
        if len(out) >= limit:
            break
        if t not in out:
            out.append(t)
    return out[:limit]


def hashtag_block(hashtags):
    return " ".join(f"#{h}" for h in hashtags)
