"""Best-effort client for the public api-web.nhle.com endpoints.

This app matches players by name (accent/punctuation-insensitive) the first
time it sees them, caches the numeric NHL player id once matched, and uses
that id afterwards. If NHL ever renames a field this file doesn't know
about yet, refresh() degrades gracefully: it records what happened in the
`meta` table (see get_meta("last_refresh_detail")) instead of crashing, and
the dashboard's "Actualiser" panel shows that detail so it's obvious what to
fix rather than silently going stale.

Note for future-me / Vincent: this sandbox that built the app has no
outbound access to nhle.com to test against, so the exact field names below
are best-effort from the public (unofficial) NHL API reference. If a
refresh comes back "ok" with everything at 0, open /admin/stats, hit
"Debug: dernière réponse brute" for one team, and compare the JSON keys to
the `pick()` calls below.
"""
import asyncio
import concurrent.futures
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from . import scoring
from .db import get_conn, set_meta, now_iso

BASE = "https://api-web.nhle.com/v1"
TIMEOUT = 12
LANDING_WORKERS = 8

# The season this pool is scoring. Until the 2026-27 season has games on the
# board, api-web.nhle.com's "now" endpoints (standings, club-stats, and a
# player's featuredStats) keep returning the last COMPLETED season (right
# now: 2025-26) instead of empty 2026-27 rows — there's no "no data yet"
# response, just last season's full totals dressed up as "current". Every
# fetch below is checked against this constant before it's trusted, so the
# pool shows 0 (accurate) rather than someone's entire 2025-26 season
# (wrong) while waiting for 2026-27 to actually start.
POOL_SEASON = "20262027"


def _season_str(value) -> str:
    return str(value) if value is not None else ""


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "pool-les-chums/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def pick(d: dict, *keys, default=0):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def full_name(entry: dict) -> str:
    first = entry.get("firstName")
    last = entry.get("lastName")
    if isinstance(first, dict):
        first = first.get("default", "")
    if isinstance(last, dict):
        last = last.get("default", "")
    return f"{first or ''} {last or ''}".strip()


def fetch_standings():
    """(season_seen, {team_code: {wins, ot_losses, losses}}).

    `season_seen` is whatever NHL season the API actually handed back (e.g.
    "20252026") — the dict is only populated when it matches POOL_SEASON;
    otherwise it's {} and the caller knows to skip writing team stats
    rather than saving last season's final standings as this season's."""
    data = _get(f"{BASE}/standings/now")
    rows = data.get("standings", [])
    season_seen = _season_str(rows[0].get("seasonId")) if rows else None
    out = {}
    if season_seen == POOL_SEASON:
        for row in rows:
            abbrev = row.get("teamAbbrev", {})
            code = abbrev.get("default") if isinstance(abbrev, dict) else abbrev
            if not code:
                continue
            out[code] = {
                "wins": pick(row, "wins"),
                "ot_losses": pick(row, "otLosses", "otWins", default=0),
                "losses": pick(row, "losses"),
            }
    return season_seen, out


def fetch_club_stats(team_code: str) -> dict:
    return _get(f"{BASE}/club-stats/{team_code}/now")


def fetch_player_landing(nhl_id: int) -> dict:
    return _get(f"{BASE}/player/{nhl_id}/landing")


def extract_landing_stats(landing: dict):
    """`club-stats/{team}/now` doesn't expose powerPlayPoints/shorthandedPoints/
    otGoals for skaters (only the goal-count variants) — verified live against
    the real API. The per-player `landing` endpoint's
    featuredStats.regularSeason.subSeason does, and is also more accurate
    for a player who has been traded mid-season (club-stats is keyed by
    *our* seed's team code, landing isn't).

    Returns None (instead of stats) when featuredStats isn't for POOL_SEASON
    — before 2026-27 has games, this endpoint returns 2025-26's completed
    totals here, and those must not be written in as "current" stats."""
    fs = landing.get("featuredStats") or {}
    if _season_str(fs.get("season")) != POOL_SEASON:
        return None
    sub = ((fs.get("regularSeason") or {}).get("subSeason")) or {}
    return {
        "games_played": pick(sub, "gamesPlayed"),
        "goals": pick(sub, "goals"),
        "assists": pick(sub, "assists"),
        "points": pick(sub, "points"),
        "pp_points": pick(sub, "powerPlayPoints"),
        "sh_points": pick(sub, "shorthandedPoints"),
        "ot_goals": pick(sub, "otGoals"),
    }


def _format_et_time(start_utc: str, eastern_offset: str) -> str:
    """`start_utc` like '2026-09-29T23:00:00Z', `eastern_offset` like '-04:00'
    (the NHL API gives us this per-game, so no DST guessing needed)."""
    try:
        dt = datetime.strptime(start_utc, "%Y-%m-%dT%H:%M:%SZ")
        sign = -1 if eastern_offset.strip().startswith("-") else 1
        h, m = eastern_offset.strip().lstrip("+-").split(":")
        local = dt + sign * timedelta(hours=int(h), minutes=int(m))
        return local.strftime("%Hh%M")
    except Exception:  # noqa: BLE001
        return ""


def fetch_schedule_week() -> list:
    """The upcoming ~7 days of NHL games, one call for the whole league.

    Returns [{"date": "2026-10-06", "games": [{"away","home","venue",
    "start_utc","time_et","state"}, ...]}, ...].
    """
    data = _get(f"{BASE}/schedule/now")
    out = []
    for day in data.get("gameWeek", []):
        games = []
        for g in day.get("games", []):
            start_utc = g.get("startTimeUTC", "")
            eastern_offset = g.get("easternUTCOffset", "-04:00")
            games.append(
                {
                    "id": g.get("id"),
                    "start_utc": start_utc,
                    "time_et": _format_et_time(start_utc, eastern_offset),
                    "state": g.get("gameState"),
                    "away": (g.get("awayTeam") or {}).get("abbrev"),
                    "home": (g.get("homeTeam") or {}).get("abbrev"),
                    "venue": (g.get("venue") or {}).get("default", ""),
                }
            )
        out.append({"date": day.get("date"), "games": games})
    return out


def refresh_all() -> dict:
    """Pull standings + every team's club stats, update the DB, log a summary.

    Returns the summary dict that also gets stored in meta so the dashboard
    can show it without another round trip.
    """
    summary = {
        "ok_teams": [],
        "failed_teams": [],
        "skaters_updated": 0,
        "goalies_updated": 0,
        "teams_updated": 0,
        "landing_updated": 0,
        "landing_failed": [],
        "started_at": now_iso(),
        "pool_season": POOL_SEASON,
    }

    try:
        season_seen, standings = fetch_standings()
    except (urllib.error.URLError, TimeoutError, Exception) as e:  # noqa: BLE001
        season_seen, standings = None, {}
        summary["standings_error"] = str(e)

    summary["nhl_season_seen"] = season_seen
    season_started = season_seen == POOL_SEASON
    summary["season_started"] = season_started
    if season_seen and not season_started:
        summary["skipped_reason"] = (
            f"La saison {POOL_SEASON[:4]}-{POOL_SEASON[6:]} n'a pas encore de matchs joués — "
            f"l'API LNH renvoie encore la saison {season_seen[:4]}-{season_seen[6:]} (la dernière "
            f"complétée) comme \"actuelle\". Stats laissées à 0 plutôt que d'écrire l'ancienne saison."
        )

    with get_conn() as conn:
        if not season_started:
            # Self-healing: earlier refreshes (before this season check
            # existed) may have written 2025-26's final totals into these
            # tables thinking they were "current". Once we know the pool
            # season hasn't started, any existing rows here can only be that
            # stale data — wipe them so the dashboard shows 0 (accurate)
            # instead of leftover numbers from last season. Manual notes on
            # players (injuries etc.) are untouched; only the numeric stat
            # tables are cleared.
            before = conn.execute("SELECT COUNT(*) FROM stats_skater").fetchone()[0]
            before += conn.execute("SELECT COUNT(*) FROM stats_goalie").fetchone()[0]
            before += conn.execute("SELECT COUNT(*) FROM stats_team").fetchone()[0]
            if before:
                conn.execute("DELETE FROM stats_skater")
                conn.execute("DELETE FROM stats_goalie")
                conn.execute("DELETE FROM stats_team")
                summary["cleared_stale_rows"] = before

        for code, s in standings.items():
            conn.execute(
                "INSERT INTO stats_team (team, wins, ot_losses, losses, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(team) DO UPDATE SET wins=excluded.wins, "
                "ot_losses=excluded.ot_losses, losses=excluded.losses, updated_at=excluded.updated_at",
                (code, s["wins"], s["ot_losses"], s["losses"], now_iso()),
            )
            summary["teams_updated"] += 1

        if season_started:
            team_codes = [
                r["team"] for r in conn.execute(
                    "SELECT DISTINCT team FROM players WHERE pos IN ('F','D','G')"
                ).fetchall()
            ]

            for team_code in team_codes:
                try:
                    club = fetch_club_stats(team_code)
                except Exception as e:  # noqa: BLE001
                    summary["failed_teams"].append({"team": team_code, "error": str(e)})
                    continue
                club_season = _season_str(club.get("season"))
                if club_season != POOL_SEASON:
                    # Defensive: standings said the season started but this
                    # team's own endpoint still disagrees — skip rather than
                    # write stale data, don't count it as a hard failure.
                    summary["failed_teams"].append(
                        {"team": team_code, "error": f"saison inattendue reçue : {club_season}"}
                    )
                    continue
                summary["ok_teams"].append(team_code)

                roster = conn.execute(
                    "SELECT id, name, pos, nhl_id FROM players WHERE team = ? AND pos IN ('F','D','G')",
                    (team_code,),
                ).fetchall()
                by_name = {_norm(r["name"]): r for r in roster}
                by_nhl_id = {r["nhl_id"]: r for r in roster if r["nhl_id"]}

                for sk in club.get("skaters", []):
                    nhl_id = pick(sk, "playerId", default=None)
                    row = by_nhl_id.get(nhl_id) or by_name.get(_norm(full_name(sk)))
                    if not row or row["pos"] not in ("F", "D"):
                        continue
                    if nhl_id and not row["nhl_id"]:
                        conn.execute("UPDATE players SET nhl_id = ? WHERE id = ?", (nhl_id, row["id"]))
                    conn.execute(
                        "INSERT INTO stats_skater (player_id, games_played, goals, assists, points, "
                        "pp_points, sh_points, ot_goals, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(player_id) DO UPDATE SET games_played=excluded.games_played, "
                        "goals=excluded.goals, assists=excluded.assists, points=excluded.points, "
                        "pp_points=excluded.pp_points, sh_points=excluded.sh_points, "
                        "ot_goals=excluded.ot_goals, updated_at=excluded.updated_at",
                        (
                            row["id"],
                            pick(sk, "gamesPlayed"),
                            pick(sk, "goals"),
                            pick(sk, "assists"),
                            pick(sk, "points"),
                            pick(sk, "powerPlayPoints", "ppPoints"),
                            pick(sk, "shorthandedPoints", "shPoints"),
                            pick(sk, "otGoals"),
                            now_iso(),
                        ),
                    )
                    summary["skaters_updated"] += 1

                for g in club.get("goalies", []):
                    nhl_id = pick(g, "playerId", default=None)
                    row = by_nhl_id.get(nhl_id) or by_name.get(_norm(full_name(g)))
                    if not row or row["pos"] != "G":
                        continue
                    if nhl_id and not row["nhl_id"]:
                        conn.execute("UPDATE players SET nhl_id = ? WHERE id = ?", (nhl_id, row["id"]))
                    conn.execute(
                        "INSERT INTO stats_goalie (player_id, games_played, wins, ot_losses, losses, "
                        "shutouts, goals, updated_at) VALUES (?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(player_id) DO UPDATE SET games_played=excluded.games_played, "
                        "wins=excluded.wins, ot_losses=excluded.ot_losses, losses=excluded.losses, "
                        "shutouts=excluded.shutouts, goals=excluded.goals, updated_at=excluded.updated_at",
                        (
                            row["id"],
                            pick(g, "gamesPlayed"),
                            pick(g, "wins"),
                            pick(g, "otLosses", "overtimeLosses"),
                            pick(g, "losses"),
                            pick(g, "shutouts"),
                            pick(g, "goals"),
                            now_iso(),
                        ),
                    )
                    summary["goalies_updated"] += 1

            # Second pass, skaters only: club-stats above doesn't expose PP/SH
            # bonus points or OT goals (only goal-count variants), and it's
            # keyed by our seed's team code so it misses mid-season trades.
            # The per-player landing endpoint fixes both — fetched in
            # parallel since this is one call per known skater, not 32.
            skater_rows = conn.execute(
                "SELECT id, nhl_id FROM players WHERE pos IN ('F','D') AND nhl_id IS NOT NULL"
            ).fetchall()

            def _fetch_landing(row):
                try:
                    stats = extract_landing_stats(fetch_player_landing(row["nhl_id"]))
                    if stats is None:
                        return row["id"], None, "saison précédente encore renvoyée par l'API"
                    return row["id"], stats, None
                except Exception as e:  # noqa: BLE001
                    return row["id"], None, str(e)

            if skater_rows:
                with concurrent.futures.ThreadPoolExecutor(max_workers=LANDING_WORKERS) as ex:
                    for player_id, stats, err in ex.map(_fetch_landing, skater_rows):
                        if stats is None:
                            summary["landing_failed"].append({"player_id": player_id, "error": err})
                            continue
                        conn.execute(
                            "INSERT INTO stats_skater (player_id, games_played, goals, assists, points, "
                            "pp_points, sh_points, ot_goals, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(player_id) DO UPDATE SET games_played=excluded.games_played, "
                            "goals=excluded.goals, assists=excluded.assists, points=excluded.points, "
                            "pp_points=excluded.pp_points, sh_points=excluded.sh_points, "
                            "ot_goals=excluded.ot_goals, updated_at=excluded.updated_at",
                            (
                                player_id,
                                stats["games_played"],
                                stats["goals"],
                                stats["assists"],
                                stats["points"],
                                stats["pp_points"],
                                stats["sh_points"],
                                stats["ot_goals"],
                                now_iso(),
                            ),
                        )
                        summary["landing_updated"] += 1

        scoring.take_snapshot(conn)

    # One call for the whole league's upcoming week — independent of the
    # per-team loop above, so a schedule fetch failure never blocks stats.
    try:
        schedule = fetch_schedule_week()
        set_meta("schedule_week", json.dumps(schedule, ensure_ascii=False))
        set_meta("schedule_updated_at", now_iso())
    except Exception as e:  # noqa: BLE001
        summary["schedule_error"] = str(e)

    summary["finished_at"] = now_iso()
    set_meta("last_refresh_detail", json.dumps(summary, ensure_ascii=False))
    set_meta("last_refresh_at", summary["finished_at"])
    ok = len(summary["failed_teams"]) == 0 and not summary.get("standings_error")
    set_meta("last_refresh_status", "ok" if ok else "partial")
    return summary


async def refresh_loop(interval_seconds: int = 2 * 60 * 60):
    """Background task: refresh once at startup, then every `interval_seconds`."""
    while True:
        try:
            await asyncio.to_thread(refresh_all)
        except Exception as e:  # noqa: BLE001
            set_meta("last_refresh_status", "error")
            set_meta("last_refresh_detail", json.dumps({"error": str(e)}))
        await asyncio.sleep(interval_seconds)
