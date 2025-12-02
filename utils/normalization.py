# utils/normalization.py
import re
from slugify import slugify

def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    return slugify(s, separator=" ")

def contains_any(text: str, *keywords: str) -> bool:
    t = normalize_text(text or "")
    return any(normalize_text(k) in t for k in keywords)
