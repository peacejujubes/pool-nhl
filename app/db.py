"""SQLite access layer for the pool app.

One file, no ORM: the schema is small and this keeps the whole app easy to
read and hack on. `get_conn()` opens a fresh connection with row access by
column name; callers use it as a context manager.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.environ.get("POOL_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "pool.db"))
SEED_DIR = Path(__file__).resolve().parent.parent / "data_seed"
PLAYERS_SEED_PATH = SEED_DIR / "players_seed.json"
INITIAL_ROSTER_PATH = SEED_DIR / "initial_roster.json"

# Fixed 24-slot roster shape shared by every pooler.
SLOTS = (
    [f"F{i}" for i in range(1, 13)]
    + [f"D{i}" for i in range(1, 7)]
    + ["G1", "G2"]
    + ["F_RES", "D_RES", "G_RES"]
    + ["T1"]
)
SLOT_POS = {}
for s in SLOTS:
    if s.startswith("F"):
        SLOT_POS[s] = "F"
    elif s.startswith("D"):
        SLOT_POS[s] = "D"
    elif s.startswith("G"):
        SLOT_POS[s] = "G"
    elif s.startswith("T"):
        SLOT_POS[s] = "T"

DISCORD_MENTIONS_SEED_PATH = SEED_DIR / "initial_discord_mentions.json"

DEFAULT_POOLERS = [
    "Olivier Payette",
    "Fred Blanchette",
    "Vincent Belle-Isle",
    "Max David",
    "Max Couillard",
    "Will Tremblay",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    team TEXT NOT NULL,
    pos TEXT NOT NULL CHECK (pos IN ('F','D','G','T')),
    note TEXT DEFAULT '',
    note_type TEXT DEFAULT '',
    nhl_id INTEGER
);

CREATE TABLE IF NOT EXISTS poolers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS roster (
    pooler_id INTEGER NOT NULL REFERENCES poolers(id),
    slot TEXT NOT NULL,
    player_id TEXT REFERENCES players(id),
    PRIMARY KEY (pooler_id, slot)
);

CREATE TABLE IF NOT EXISTS stats_skater (
    player_id TEXT PRIMARY KEY REFERENCES players(id),
    games_played INTEGER DEFAULT 0,
    goals INTEGER DEFAULT 0,
    assists INTEGER DEFAULT 0,
    points INTEGER DEFAULT 0,
    pp_points INTEGER DEFAULT 0,
    sh_points INTEGER DEFAULT 0,
    ot_goals INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS stats_goalie (
    player_id TEXT PRIMARY KEY REFERENCES players(id),
    games_played INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    ot_losses INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    shutouts INTEGER DEFAULT 0,
    goals INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS stats_team (
    team TEXT PRIMARY KEY,
    wins INTEGER DEFAULT 0,
    ot_losses INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pooler_id INTEGER NOT NULL REFERENCES poolers(id),
    label TEXT NOT NULL,
    points REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS pooler_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pooler_id INTEGER NOT NULL REFERENCES poolers(id),
    total REAL NOT NULL,
    taken_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pooler_snap ON pooler_snapshots(pooler_id, taken_at);

CREATE TABLE IF NOT EXISTS player_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id TEXT NOT NULL REFERENCES players(id),
    pooler_id INTEGER NOT NULL REFERENCES poolers(id),
    points REAL NOT NULL,
    taken_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_player_snap ON player_snapshots(player_id, taken_at);
"""


@contextmanager
def get_conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables, seed players and the 6 default poolers if empty. Idempotent."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)

        n_players = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        if n_players == 0 and PLAYERS_SEED_PATH.exists():
            seed = json.loads(PLAYERS_SEED_PATH.read_text(encoding="utf-8"))
            conn.executemany(
                "INSERT OR IGNORE INTO players (id, name, team, pos, note, note_type) "
                "VALUES (:id, :name, :team, :pos, :note, :note_type)",
                seed,
            )

        n_poolers = conn.execute("SELECT COUNT(*) FROM poolers").fetchone()[0]
        if n_poolers == 0:
            for i, name in enumerate(DEFAULT_POOLERS):
                conn.execute(
                    "INSERT INTO poolers (name, sort_order) VALUES (?, ?)", (name, i)
                )

        poolers = conn.execute("SELECT id, name FROM poolers").fetchall()
        for p in poolers:
            for slot in SLOTS:
                conn.execute(
                    "INSERT OR IGNORE INTO roster (pooler_id, slot, player_id) VALUES (?, ?, NULL)",
                    (p["id"], slot),
                )

        # First boot only: if every roster slot is still empty and we ship a
        # known-good initial lineup (the real 2026-27 draft results), load it
        # so the dashboard is useful immediately instead of showing 6 blank
        # teams. Any manual edit made afterwards via /setup is untouched.
        n_filled = conn.execute(
            "SELECT COUNT(*) FROM roster WHERE player_id IS NOT NULL"
        ).fetchone()[0]
        if n_filled == 0 and INITIAL_ROSTER_PATH.exists():
            initial = json.loads(INITIAL_ROSTER_PATH.read_text(encoding="utf-8"))
            name_to_id = {p["name"]: p["id"] for p in poolers}
            for pooler_name, slots in initial.items():
                pooler_id = name_to_id.get(pooler_name)
                if not pooler_id:
                    continue
                for slot, player_id in slots.items():
                    conn.execute(
                        "UPDATE roster SET player_id = ? WHERE pooler_id = ? AND slot = ?",
                        (player_id, pooler_id, slot),
                    )

        # Discord mention IDs (numeric Discord user IDs, keyed by pooler
        # name): seeded once from a known mapping Vincent gave us, but only
        # if he hasn't already configured any via /admin/alertes — never
        # overwrites what's there. Same one-time-seed shape as the roster
        # above, just for the "meta" table instead of "roster".
        existing_mentions = conn.execute(
            "SELECT value FROM meta WHERE key = 'discord_mentions'"
        ).fetchone()
        if not existing_mentions and DISCORD_MENTIONS_SEED_PATH.exists():
            seed_by_name = json.loads(DISCORD_MENTIONS_SEED_PATH.read_text(encoding="utf-8"))
            name_to_id = {p["name"]: p["id"] for p in poolers}
            mentions = {
                str(name_to_id[name]): discord_id
                for name, discord_id in seed_by_name.items()
                if name in name_to_id
            }
            if mentions:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('discord_mentions', ?) "
                    "ON CONFLICT(key) DO NOTHING",
                    (json.dumps(mentions, ensure_ascii=False),),
                )

        # Seed empty stat rows lazily on refresh instead of here; team codes
        # come from the players table itself.


def set_meta(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_meta(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
