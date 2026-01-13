import os
import re
from bs4 import BeautifulSoup

REQUIRE_IMAGES = os.getenv("REQUIRE_IMAGES", "true").lower() == "true"
MIN_YEAR = int(os.getenv("MIN_YEAR", "2016"))

BLACKLIST_WORDS = [
    "unfall",
    "bastler",
    "motorschaden",
    "defekt",
    "export",
    "teileträger",
    "ohne tüv",
]

def is_blacklisted(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in BLACKLIST_WORDS)

def year_ok(year: int | None) -> bool:
    if year is None:
        # wenn kein Jahr erkennbar: lieber raus (sauberer)
        return False
    return year >= MIN_YEAR

def has_images(image_count: int | None) -> bool:
    if not REQUIRE_IMAGES:
        return True
    return (image_count or 0) > 0

def extract_year(text: str) -> int | None:
    # sucht nach 19xx oder 20xx
    if not text:
        return None
    m = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    return int(m.group(1)) if m else None
