"""Inställningar (flikar) och SQLite-databasen med fillistor och innehållsindex."""
import json
import os
import sqlite3
from pathlib import Path

_LOCAL = Path(os.environ.get("LOCALAPPDATA") or Path.home())
APP_DIR = Path(os.environ.get("SWPT_DIR") or _LOCAL / "swpt")
OLD_APP_DIR = _LOCAL / "Mappsok"  # appens tidigare namn
CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = APP_DIR / "index.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY,
    tab TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    is_dir INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    cloud INTEGER NOT NULL DEFAULT 0,
    cmtime REAL,
    UNIQUE(tab, path)
);
CREATE VIRTUAL TABLE IF NOT EXISTS content USING fts5(text, tokenize='trigram remove_diacritics 1');
"""


def load_config():
    if not APP_DIR.exists() and OLD_APP_DIR.exists() and "SWPT_DIR" not in os.environ:
        try:
            OLD_APP_DIR.rename(APP_DIR)
        except OSError:
            pass
    try:
        cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("tabs", [])
    return cfg


def save_config(cfg):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, CONFIG_PATH)


def connect():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    if "omtime" not in [r[1] for r in con.execute("PRAGMA table_info(files)")]:
        try:   # när filen senast lästes med OCR (äldre databaser saknar kolumnen)
            con.execute("ALTER TABLE files ADD COLUMN omtime REAL")
            con.commit()
        except sqlite3.OperationalError:
            pass
    return con
