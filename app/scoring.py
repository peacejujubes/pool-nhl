"""Pool scoring rules, applied to whatever is currently in stats_* tables.

Rules as given by Vincent (see the project's pool-regles-strategie doc):
  - skater goal or assist = 1 pt
  - +1 bonus if that point came on the power play
  - +1 bonus if shorthanded
  - +1 bonus if in overtime
  - goalie who scores a goal = 10 pts (rare; NHL boxscore stats rarely expose
    this cleanly, so it is also editable by hand on the /admin/stats page)
  - win (goalie or a picked NHL team) = 2 pts
  - overtime loss = 1 pt
  - loss = 0 pts
  - shutout win = 3 pts total (i.e. the normal 2 for the win, +1 bonus)
  - a team AND its goalie both being on the same roster is not a special
    combo bonus in this app: each already scores independently, so holding
    both naturally adds up to 4 pts on a win (2 + 2). No extra code needed.

Known gap (documented for Vincent, see README "Limites connues"): NHL season
stat totals don't expose a team-wide shutout bonus (only the goalie's own
shutouts). If that happens, log it from /ajustements by hand.
"""
from .db import get_conn, SLOTS, now_iso


def skater_points(row) -> float:
    if row is None:
        return 0.0
    return float(
        (row["points"] or 0)
        + (row["pp_points"] or 0)
        + (row["sh_points"] or 0)
        + (row["ot_goals"] or 0)
    )


def goalie_points(row) -> float:
    if row is None:
        return 0.0
    wins = row["wins"] or 0
    otl = row["ot_losses"] or 0
    shutouts = row["shutouts"] or 0
    goals = row["goals"] or 0
    return float(wins * 2 + otl * 1 + shutouts * 1 + goals * 10)


def team_points(row) -> float:
    if row is None:
        return 0.0
    wins = row["wins"] or 0
    otl = row["ot_losses"] or 0
    return float(wins * 2 + otl * 1)


def player_points(player, skater_row, goalie_row, team_row) -> float:
    pos = player["pos"]
    if pos == "G":
        return goalie_points(goalie_row)
    if pos == "T":
        return team_points(team_row)
    return skater_points(skater_row)


def compute_all(conn=None):
    """Return {pooler_id: {"name", "total", "adjustments", "slots": {slot: {...}}}}."""
    owns_conn = conn is None
    if owns_conn:
        cm = get_conn()
        conn = cm.__enter__()
    try:
        poolers = conn.execute(
            "SELECT id, name FROM poolers ORDER BY sort_order"
        ).fetchall()
        players = {p["id"]: p for p in conn.execute("SELECT * FROM players").fetchall()}
        skaters = {
            r["player_id"]: r for r in conn.execute("SELECT * FROM stats_skater").fetchall()
        }
        goalies = {
            r["player_id"]: r for r in conn.execute("SELECT * FROM stats_goalie").fetchall()
        }
        teams = {r["team"]: r for r in conn.execute("SELECT * FROM stats_team").fetchall()}

        result = {}
        for p in poolers:
            roster_rows = conn.execute(
                "SELECT slot, player_id FROM roster WHERE pooler_id = ?", (p["id"],)
            ).fetchall()
            roster_by_slot = {r["slot"]: r["player_id"] for r in roster_rows}

            adj_rows = conn.execute(
                "SELECT id, label, points, created_at FROM adjustments "
                "WHERE pooler_id = ? ORDER BY created_at DESC",
                (p["id"],),
            ).fetchall()
            adj_total = sum(a["points"] for a in adj_rows)

            slots_out = {}
            total = adj_total
            cat = {
                "goals": 0, "assists": 0, "pp": 0, "sh": 0, "ot": 0,
                "goalie_wins": 0, "goalie_so": 0, "team_wins": 0, "games_played": 0,
            }
            for slot in SLOTS:
                pid = roster_by_slot.get(slot)
                player = players.get(pid) if pid else None
                pts = 0.0
                team_code = None
                if player:
                    team_code = player["team"]
                    sk, gl, tm = skaters.get(pid), goalies.get(pid), teams.get(team_code)
                    pts = player_points(player, sk, gl, tm)
                    if player["pos"] in ("F", "D") and sk:
                        cat["goals"] += sk["goals"] or 0
                        cat["assists"] += sk["assists"] or 0
                        cat["pp"] += sk["pp_points"] or 0
                        cat["sh"] += sk["sh_points"] or 0
                        cat["ot"] += sk["ot_goals"] or 0
                        cat["games_played"] += sk["games_played"] or 0
                    elif player["pos"] == "G" and gl:
                        cat["goalie_wins"] += gl["wins"] or 0
                        cat["goalie_so"] += gl["shutouts"] or 0
                        cat["games_played"] += gl["games_played"] or 0
                    elif player["pos"] == "T" and tm:
                        cat["team_wins"] += tm["wins"] or 0
                slots_out[slot] = {
                    "player": dict(player) if player else None,
                    "points": pts,
                }
                total += pts

            result[p["id"]] = {
                "name": p["name"],
                "total": total,
                "adjustments": [dict(a) for a in adj_rows],
                "adjustments_total": adj_total,
                "slots": slots_out,
                "categories": cat,
            }
        return result
    finally:
        if owns_conn:
            cm.__exit__(None, None, None)


def taken_player_ids(conn) -> set:
    rows = conn.execute(
        "SELECT player_id FROM roster WHERE player_id IS NOT NULL"
    ).fetchall()
    return {r["player_id"] for r in rows}


def flat_roster(standings: dict) -> list:
    """Every rostered (player, points, pooler_name, slot) tuple across all 6
    teams, as plain dicts — the basis for league-wide leaderboards."""
    out = []
    for pooler_id, t in standings.items():
        for slot, s in t["slots"].items():
            if s["player"]:
                out.append(
                    {
                        "player": s["player"],
                        "points": s["points"],
                        "pooler_id": pooler_id,
                        "pooler_name": t["name"],
                        "slot": slot,
                    }
                )
    return out


def league_leaders(standings: dict, limit: int = 8) -> dict:
    flat = flat_roster(standings)
    skaters = sorted((r for r in flat if r["player"]["pos"] in ("F", "D")), key=lambda r: r["points"], reverse=True)
    goalies = sorted((r for r in flat if r["player"]["pos"] == "G"), key=lambda r: r["points"], reverse=True)
    teams = sorted((r for r in flat if r["player"]["pos"] == "T"), key=lambda r: r["points"], reverse=True)
    return {
        "skaters": skaters[:limit],
        "goalies": goalies[:limit],
        "teams": teams[:limit],
    }


def take_snapshot(conn):
    """Record every pooler's current total and every rostered player's
    current points, timestamped together, so the dashboard can show rank
    movement, point deltas, and "trending" players between refreshes."""
    standings = compute_all(conn)
    ts = now_iso()
    for pooler_id, t in standings.items():
        conn.execute(
            "INSERT INTO pooler_snapshots (pooler_id, total, taken_at) VALUES (?,?,?)",
            (pooler_id, t["total"], ts),
        )
    for row in flat_roster(standings):
        conn.execute(
            "INSERT INTO player_snapshots (player_id, pooler_id, points, taken_at) VALUES (?,?,?,?)",
            (row["player"]["id"], row["pooler_id"], row["points"], ts),
        )
    return ts


def previous_pooler_totals(conn):
    """(batch_timestamp, {pooler_id: total}) from the most recent snapshot
    batch strictly before the latest one (i.e. the comparison point for
    "since last refresh"). Returns (None, {}) if fewer than 2 batches exist."""
    batches = conn.execute(
        "SELECT DISTINCT taken_at FROM pooler_snapshots ORDER BY taken_at DESC LIMIT 2"
    ).fetchall()
    if len(batches) < 2:
        return None, {}
    ts = batches[1]["taken_at"]
    rows = conn.execute(
        "SELECT pooler_id, total FROM pooler_snapshots WHERE taken_at = ?", (ts,)
    ).fetchall()
    return ts, {r["pooler_id"]: r["total"] for r in rows}


def previous_player_points(conn):
    """(batch_timestamp, {player_id: points}) — same idea as
    previous_pooler_totals but per player, used for the "trending" panel."""
    batches = conn.execute(
        "SELECT DISTINCT taken_at FROM player_snapshots ORDER BY taken_at DESC LIMIT 2"
    ).fetchall()
    if len(batches) < 2:
        return None, {}
    ts = batches[1]["taken_at"]
    rows = conn.execute(
        "SELECT player_id, points FROM player_snapshots WHERE taken_at = ?", (ts,)
    ).fetchall()
    return ts, {r["player_id"]: r["points"] for r in rows}


def pooler_sparkline(conn, pooler_id: int, limit: int = 12) -> list:
    rows = conn.execute(
        "SELECT total, taken_at FROM pooler_snapshots WHERE pooler_id = ? "
        "ORDER BY taken_at DESC LIMIT ?",
        (pooler_id, limit),
    ).fetchall()
    return [r["total"] for r in reversed(rows)]
