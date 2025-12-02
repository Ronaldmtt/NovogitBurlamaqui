# utils/option_catalog.py
import json
from pathlib import Path
from typing import List, Dict, Any
from .normalization import normalize_text

CATALOG_DIR = Path("static/option_catalog")
CATALOG_DIR.mkdir(parents=True, exist_ok=True)

def load_catalog(name: str) -> List[Dict[str, Any]]:
    f = CATALOG_DIR / f"{name}.json"
    if not f.exists():
        return []
    data = json.loads(f.read_text(encoding="utf-8"))
    for d in data:
        d["normalized"] = normalize_text(d.get("text", ""))
    return data

def save_catalog(name: str, options: List[Dict[str, Any]]) -> None:
    for o in options:
        o["normalized"] = normalize_text(o.get("text", ""))
    f = CATALOG_DIR / f"{name}.json"
    f.write_text(json.dumps(options, ensure_ascii=False, indent=2), encoding="utf-8")
