import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date as _date, timedelta as _timedelta
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import discord_alerts, nhl, scoring
from .db import get_conn, init_db, now_iso, get_meta, set_meta, SLOTS, SLOT_POS
from .scoring import compute_all, taken_player_ids
from .teams import team_color, team_name
from .viz import sparkline_points, sparkline_trend

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["team_color"] = team_color
templates.env.globals["team_name"] = team_name
templates.env.globals["sparkline_points"] = sparkline_points
templates.env.globals["sparkline_trend"] = sparkline_trend

POS_LABELS = {"F": "Attaquant", "D": "Défenseur", "G": "Gardien", "T": "Équipe"}
NOTE_LABELS = {
    "injury": "Blessé",
    "trade": "Nouvelle équipe",
    "contract": "Sans contrat",
    "rookie": "Recrue",
    "young": "Jeune joueur",
    "role": "Rôle à surveiller",
    "other": "À noter",
}

# Columns shown on the /stats-lnh explorer, per position — (db_column, label).
STATS_LNH_COLUMNS = {
    "F": [
        ("games_played", "PJ"), ("goals", "B"), ("assists", "A"), ("points", "P"),
        ("pp_points", "AN"), ("sh_points", "DN"), ("ot_goals", "Prol."),
    ],
    "G": [
        ("games_played", "PJ"), ("wins", "V"), ("ot_losses", "DP"), ("losses", "D"),
        ("shutouts", "BL"), ("goals", "B (10pt)"),
    ],
    "T": [
        ("wins", "V"), ("ot_losses", "DP"), ("losses", "D"),
    ],
}
STATS_LNH_COLUMNS["D"] = STATS_LNH_COLUMNS["F"]

FR_WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
FR_MONTHS = [
    "janv.", "févr.", "mars", "avr.", "mai", "juin",
    "juill.", "août", "sept.", "oct.", "nov.", "déc.",
]
GAME_STATE_LABELS = {
    "FUT": "à venir", "PRE": "à venir", "LIVE": "en cours", "CRIT": "en cours",
    "OFF": "terminé", "FINAL": "terminé",
}


def refresh_status():
    detail_raw = get_meta("last_refresh_detail")
    detail = json.loads(detail_raw) if detail_raw else None
    return {
        "status": get_meta("last_refresh_status", "jamais"),
        "at": get_meta("last_refresh_at"),
        "detail": detail,
    }


async def dashboard(request):
    with get_conn() as conn:
        standings = compute_all(conn)
        prev_ts, prev_totals = scoring.previous_pooler_totals(conn)
        prev_player_ts, prev_points = scoring.previous_player_points(conn)
        leaders = scoring.league_leaders(standings, limit=8)
        sparks = {pid: scoring.pooler_sparkline(conn, pid) for pid in standings}

    ranked = sorted(standings.items(), key=lambda kv: kv[1]["total"], reverse=True)

    # rank movement: compare current order to the order implied by the
    # previous snapshot batch (same poolers, sorted by their old totals)
    prev_rank = {}
    if prev_totals:
        prev_order = sorted(prev_totals.items(), key=lambda kv: kv[1], reverse=True)
        prev_rank = {pid: i + 1 for i, (pid, _) in enumerate(prev_order)}

    movement = {}
    for i, (pid, t) in enumerate(ranked, start=1):
        old_rank = prev_rank.get(pid)
        movement[pid] = {
            "delta": t["total"] - prev_totals[pid] if pid in prev_totals else None,
            "rank_change": (old_rank - i) if old_rank else None,
        }

    # trending: rostered players with the biggest point gain since the
    # previous snapshot batch
    flat = scoring.flat_roster(standings)
    trending = []
    for r in flat:
        pid = r["player"]["id"]
        if pid in prev_points:
            delta = r["points"] - prev_points[pid]
            if delta > 0:
                trending.append({**r, "delta": delta})
    trending.sort(key=lambda r: r["delta"], reverse=True)
    trending = trending[:6]

    # status ticker: every rostered player currently flagged with a note
    statuses = [r for r in flat if r["player"].get("note")]
    statuses.sort(key=lambda r: r["player"]["name"])

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "ranked": ranked,
            "refresh": refresh_status(),
            "slots": SLOTS,
            "movement": movement,
            "leaders": leaders,
            "trending": trending,
            "statuses": statuses,
            "sparks": sparks,
            "has_history": bool(prev_totals),
        },
    )


async def team_detail(request):
    pooler_id = int(request.path_params["pooler_id"])
    with get_conn() as conn:
        standings = compute_all(conn)
        pooler_row = conn.execute("SELECT id, name FROM poolers WHERE id=?", (pooler_id,)).fetchone()
        spark = scoring.pooler_sparkline(conn, pooler_id, limit=30)
    if not pooler_row:
        return RedirectResponse("/", status_code=302)
    ranked = sorted(standings.items(), key=lambda kv: kv[1]["total"], reverse=True)
    rank = next(i for i, (pid, _) in enumerate(ranked, start=1) if pid == pooler_id)
    team = standings[pooler_id]
    return templates.TemplateResponse(
        request,
        "team_detail.html",
        {
            "pooler": pooler_row,
            "team": team,
            "rank": rank,
            "total_poolers": len(ranked),
            "slots": SLOTS,
            "note_labels": NOTE_LABELS,
            "refresh": refresh_status(),
            "spark": spark,
        },
    )


async def setup(request):
    with get_conn() as conn:
        poolers = conn.execute("SELECT id, name FROM poolers ORDER BY sort_order").fetchall()
        rosters = {}
        for p in poolers:
            rows = conn.execute(
                "SELECT r.slot, r.player_id, pl.name, pl.team, pl.pos "
                "FROM roster r LEFT JOIN players pl ON pl.id = r.player_id "
                "WHERE r.pooler_id = ?",
                (p["id"],),
            ).fetchall()
            by_slot = {r["slot"]: dict(r) for r in rows}
            rosters[p["id"]] = {slot: by_slot.get(slot) for slot in SLOTS}

        taken_rows = conn.execute(
            "SELECT r.player_id, pl2.name as pooler_name FROM roster r "
            "JOIN poolers pl2 ON pl2.id = r.pooler_id WHERE r.player_id IS NOT NULL"
        ).fetchall()
        taken_by = {r["player_id"]: r["pooler_name"] for r in taken_rows}

        players_by_pos = {}
        for pos in ("F", "D", "G", "T"):
            rows = conn.execute(
                "SELECT id, name, team FROM players WHERE pos = ? ORDER BY name", (pos,)
            ).fetchall()
            players_by_pos[pos] = [dict(r) for r in rows]

    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "poolers": poolers,
            "rosters": rosters,
            "slots": SLOTS,
            "slot_pos": SLOT_POS,
            "pos_labels": POS_LABELS,
            "taken_by": taken_by,
            "players_by_pos": players_by_pos,
        },
    )


async def api_search(request):
    q = request.query_params.get("q", "").strip()
    pos = request.query_params.get("pos", "").strip()
    pooler_id = request.query_params.get("pooler_id", "")
    slot = request.query_params.get("slot", "")
    with get_conn() as conn:
        taken = taken_player_ids(conn)
        # if editing a slot that already has a player, that player should
        # still show up (re-selecting the same slot isn't "taken by someone else")
        current_pid = None
        if pooler_id and slot:
            row = conn.execute(
                "SELECT player_id FROM roster WHERE pooler_id=? AND slot=?",
                (pooler_id, slot),
            ).fetchone()
            current_pid = row["player_id"] if row else None

        sql = "SELECT id, name, team, pos FROM players WHERE 1=1"
        params = []
        if pos:
            sql += " AND pos = ?"
            params.append(pos)
        if q:
            sql += " AND (name LIKE ? OR team LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%"])
        sql += " ORDER BY name LIMIT 40"
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            if r["id"] in taken and r["id"] != current_pid:
                continue
            out.append({"id": r["id"], "name": r["name"], "team": r["team"], "pos": r["pos"]})
    return JSONResponse(out)


async def setup_assign(request):
    form = await request.form()
    pooler_id = int(form["pooler_id"])
    slot = form["slot"]
    player_id = form.get("player_id") or None
    if slot not in SLOTS:
        return JSONResponse({"error": "slot invalide"}, status_code=400)
    with get_conn() as conn:
        if player_id:
            expected_pos = SLOT_POS[slot]
            p = conn.execute("SELECT pos FROM players WHERE id=?", (player_id,)).fetchone()
            if not p or p["pos"] != expected_pos:
                return JSONResponse({"error": "position invalide pour ce poste"}, status_code=400)
            taken = taken_player_ids(conn)
            current = conn.execute(
                "SELECT player_id FROM roster WHERE pooler_id=? AND slot=?", (pooler_id, slot)
            ).fetchone()
            if player_id in taken and player_id != (current["player_id"] if current else None):
                return JSONResponse({"error": "déjà repêché par quelqu'un d'autre"}, status_code=409)
        conn.execute(
            "UPDATE roster SET player_id = ? WHERE pooler_id = ? AND slot = ?",
            (player_id, pooler_id, slot),
        )
        scoring.take_snapshot(conn)
    if request.headers.get("accept", "").find("application/json") >= 0:
        return JSONResponse({"ok": True})
    return RedirectResponse("/setup", status_code=302)


async def ajustements(request):
    with get_conn() as conn:
        poolers = conn.execute("SELECT id, name FROM poolers ORDER BY sort_order").fetchall()
        rows = conn.execute(
            "SELECT a.id, a.label, a.points, a.created_at, p.name as pooler_name "
            "FROM adjustments a JOIN poolers p ON p.id = a.pooler_id "
            "ORDER BY a.created_at DESC"
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "ajustements.html",
        {"poolers": poolers, "rows": rows},
    )


async def ajustements_add(request):
    form = await request.form()
    pooler_id = int(form["pooler_id"])
    label = form["label"].strip()
    points = float(form["points"])
    if label:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO adjustments (pooler_id, label, points, created_at) VALUES (?,?,?,?)",
                (pooler_id, label, points, now_iso()),
            )
            scoring.take_snapshot(conn)
    return RedirectResponse("/ajustements", status_code=302)


async def ajustements_delete(request):
    adj_id = int(request.path_params["adj_id"])
    with get_conn() as conn:
        conn.execute("DELETE FROM adjustments WHERE id = ?", (adj_id,))
        scoring.take_snapshot(conn)
    return RedirectResponse("/ajustements", status_code=302)


async def admin_stats(request):
    pos = request.query_params.get("pos", "F")
    with get_conn() as conn:
        if pos == "G":
            rows = conn.execute(
                "SELECT pl.id, pl.name, pl.team, pl.note, pl.note_type, "
                "s.games_played, s.wins, s.ot_losses, s.losses, s.shutouts, s.goals, s.updated_at "
                "FROM players pl LEFT JOIN stats_goalie s ON s.player_id = pl.id "
                "WHERE pl.pos = 'G' ORDER BY pl.name"
            ).fetchall()
        elif pos == "T":
            rows = conn.execute(
                "SELECT pl.id, pl.name, pl.team, pl.note, pl.note_type, "
                "s.wins, s.ot_losses, s.losses, s.updated_at "
                "FROM players pl LEFT JOIN stats_team s ON s.team = pl.team "
                "WHERE pl.pos = 'T' ORDER BY pl.name"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT pl.id, pl.name, pl.team, pl.note, pl.note_type, "
                "s.games_played, s.goals, s.assists, s.points, s.pp_points, s.sh_points, "
                "s.ot_goals, s.updated_at "
                "FROM players pl LEFT JOIN stats_skater s ON s.player_id = pl.id "
                "WHERE pl.pos = ? ORDER BY pl.name",
                (pos,),
            ).fetchall()
    return templates.TemplateResponse(
        request,
        "admin_stats.html",
        {
            "pos": pos,
            "rows": rows,
            "refresh": refresh_status(),
            "note_labels": NOTE_LABELS,
        },
    )


async def admin_update_skater(request):
    pid = request.path_params["player_id"]
    form = await request.form()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO stats_skater (player_id, games_played, goals, assists, points, "
            "pp_points, sh_points, ot_goals, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(player_id) DO UPDATE SET games_played=excluded.games_played, "
            "goals=excluded.goals, assists=excluded.assists, points=excluded.points, "
            "pp_points=excluded.pp_points, sh_points=excluded.sh_points, "
            "ot_goals=excluded.ot_goals, updated_at=excluded.updated_at",
            (
                pid,
                int(form.get("games_played") or 0),
                int(form.get("goals") or 0),
                int(form.get("assists") or 0),
                int(form.get("points") or 0),
                int(form.get("pp_points") or 0),
                int(form.get("sh_points") or 0),
                int(form.get("ot_goals") or 0),
                now_iso(),
            ),
        )
        scoring.take_snapshot(conn)
    return RedirectResponse("/admin/stats?pos=F", status_code=302)


async def admin_update_goalie(request):
    pid = request.path_params["player_id"]
    form = await request.form()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO stats_goalie (player_id, games_played, wins, ot_losses, losses, "
            "shutouts, goals, updated_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(player_id) DO UPDATE SET games_played=excluded.games_played, "
            "wins=excluded.wins, ot_losses=excluded.ot_losses, losses=excluded.losses, "
            "shutouts=excluded.shutouts, goals=excluded.goals, updated_at=excluded.updated_at",
            (
                pid,
                int(form.get("games_played") or 0),
                int(form.get("wins") or 0),
                int(form.get("ot_losses") or 0),
                int(form.get("losses") or 0),
                int(form.get("shutouts") or 0),
                int(form.get("goals") or 0),
                now_iso(),
            ),
        )
        scoring.take_snapshot(conn)
    return RedirectResponse("/admin/stats?pos=G", status_code=302)


async def admin_update_team(request):
    team = request.path_params["team_code"]
    form = await request.form()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO stats_team (team, wins, ot_losses, losses, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(team) DO UPDATE SET wins=excluded.wins, ot_losses=excluded.ot_losses, "
            "losses=excluded.losses, updated_at=excluded.updated_at",
            (
                team,
                int(form.get("wins") or 0),
                int(form.get("ot_losses") or 0),
                int(form.get("losses") or 0),
                now_iso(),
            ),
        )
        scoring.take_snapshot(conn)
    return RedirectResponse("/admin/stats?pos=T", status_code=302)


async def admin_update_note(request):
    pid = request.path_params["player_id"]
    form = await request.form()
    note = form.get("note", "").strip()
    note_type = form.get("note_type", "").strip()
    with get_conn() as conn:
        prev = conn.execute("SELECT note_type, name FROM players WHERE id = ?", (pid,)).fetchone()
        conn.execute(
            "UPDATE players SET note = ?, note_type = ? WHERE id = ?", (note, note_type, pid)
        )
        # Injury alerts trigger right here, not from refresh_all(): injury
        # status on this app is set by hand (the NHL API doesn't expose it
        # reliably — see README), so "a player becomes injured" IS this
        # write, specifically a transition into note_type == "injury" from
        # something else. Editing the detail text of an existing injury
        # note (same note_type, different note) does not re-ping anyone.
        became_injured = note_type == "injury" and (not prev or prev["note_type"] != "injury")
        owners = []
        if became_injured:
            owners = conn.execute(
                "SELECT r.pooler_id, p.name AS pooler_name FROM roster r "
                "JOIN poolers p ON p.id = r.pooler_id WHERE r.player_id = ?",
                (pid,),
            ).fetchall()

    if became_injured and owners:
        player_name = prev["name"] if prev else pid
        await asyncio.to_thread(discord_alerts.notify_injury, owners, player_name, note)

    back = form.get("back_pos", "F")
    return RedirectResponse(f"/admin/stats?pos={back}", status_code=302)


async def stats_lnh(request):
    pos = request.query_params.get("pos", "F")
    if pos not in ("F", "D", "G", "T"):
        pos = "F"
    q = request.query_params.get("q", "").strip()
    avail_only = request.query_params.get("avail") == "1"
    sort = request.query_params.get("sort", "pool_points")
    direction = request.query_params.get("dir", "desc")

    with get_conn() as conn:
        taken_rows = conn.execute(
            "SELECT r.player_id, pl2.name as pooler_name FROM roster r "
            "JOIN poolers pl2 ON pl2.id = r.pooler_id WHERE r.player_id IS NOT NULL"
        ).fetchall()
        taken_by = {r["player_id"]: r["pooler_name"] for r in taken_rows}

        if pos == "G":
            rows = conn.execute(
                "SELECT pl.id, pl.name, pl.team, pl.note, pl.note_type, "
                "s.games_played, s.wins, s.ot_losses, s.losses, s.shutouts, s.goals "
                "FROM players pl LEFT JOIN stats_goalie s ON s.player_id = pl.id "
                "WHERE pl.pos = 'G'"
            ).fetchall()
        elif pos == "T":
            rows = conn.execute(
                "SELECT pl.id, pl.name, pl.team, pl.note, pl.note_type, "
                "s.wins, s.ot_losses, s.losses "
                "FROM players pl LEFT JOIN stats_team s ON s.team = pl.team "
                "WHERE pl.pos = 'T'"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT pl.id, pl.name, pl.team, pl.note, pl.note_type, "
                "s.games_played, s.goals, s.assists, s.points, s.pp_points, s.sh_points, s.ot_goals "
                "FROM players pl LEFT JOIN stats_skater s ON s.player_id = pl.id "
                "WHERE pl.pos = ?",
                (pos,),
            ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["owner"] = taken_by.get(d["id"])
        if pos == "G":
            d["pool_points"] = scoring.goalie_points(r)
        elif pos == "T":
            d["pool_points"] = scoring.team_points(r)
        else:
            d["pool_points"] = scoring.skater_points(r)
        out.append(d)

    if q:
        ql = q.lower()
        out = [d for d in out if ql in d["name"].lower() or ql in (d["team"] or "").lower()]
    if avail_only:
        out = [d for d in out if not d["owner"]]

    string_keys = {"name", "team", "owner", "note_type"}
    valid_sort_keys = {"name", "team", "owner", "pool_points"} | {c for c, _ in STATS_LNH_COLUMNS[pos]}
    if sort not in valid_sort_keys:
        sort = "pool_points"

    def sort_val(d):
        v = d.get(sort)
        if sort in string_keys:
            return (v or "").lower()
        return v if v is not None else 0

    out.sort(key=sort_val, reverse=(direction != "asc"))

    return templates.TemplateResponse(
        request,
        "stats_lnh.html",
        {
            "pos": pos,
            "rows": out,
            "columns": STATS_LNH_COLUMNS[pos],
            "note_labels": NOTE_LABELS,
            "q": q,
            "avail_only": avail_only,
            "sort": sort,
            "dir": direction,
            "refresh": refresh_status(),
        },
    )


async def calendrier(request):
    raw = get_meta("schedule_week")
    schedule = json.loads(raw) if raw else []
    updated_at = get_meta("schedule_updated_at")

    with get_conn() as conn:
        standings = compute_all(conn)

    # NHL team code -> set of pooler names who have a player (or that team
    # itself) from that team rostered — powers the "qui ça touche" chips.
    owners_by_team = {}
    for pooler_id, t in standings.items():
        for slot, s in t["slots"].items():
            pl = s["player"]
            if pl:
                owners_by_team.setdefault(pl["team"], set()).add(t["name"])

    team_dates = {}
    for day in schedule:
        for g in day.get("games", []):
            team_dates.setdefault(g["away"], set()).add(day["date"])
            team_dates.setdefault(g["home"], set()).add(day["date"])

    def is_b2b(team, iso_date):
        try:
            cur = _date.fromisoformat(iso_date)
        except ValueError:
            return False
        prev = (cur - _timedelta(days=1)).isoformat()
        return prev in team_dates.get(team, set())

    days_out = []
    for day in schedule:
        try:
            d = _date.fromisoformat(day["date"])
            label = f"{FR_WEEKDAYS[d.weekday()]} {d.day} {FR_MONTHS[d.month - 1]}"
        except (ValueError, KeyError):
            label = day.get("date", "")
        games_out = []
        for g in day.get("games", []):
            games_out.append(
                {
                    **g,
                    "state_label": GAME_STATE_LABELS.get(g.get("state"), g.get("state") or ""),
                    "away_owners": sorted(owners_by_team.get(g["away"], [])),
                    "home_owners": sorted(owners_by_team.get(g["home"], [])),
                    "away_b2b": is_b2b(g["away"], day["date"]),
                    "home_b2b": is_b2b(g["home"], day["date"]),
                }
            )
        days_out.append({"date": day["date"], "label": label, "games": games_out})

    return templates.TemplateResponse(
        request,
        "calendrier.html",
        {"days": days_out, "updated_at": updated_at, "refresh": refresh_status()},
    )


async def comparaison(request):
    with get_conn() as conn:
        standings = compute_all(conn)
        poolers = conn.execute("SELECT id, name FROM poolers ORDER BY sort_order").fetchall()

    pooler_ids = [p["id"] for p in poolers]
    default_b = pooler_ids[1] if len(pooler_ids) > 1 else pooler_ids[0]
    try:
        a_id = int(request.query_params.get("a", pooler_ids[0]))
    except (TypeError, ValueError):
        a_id = pooler_ids[0]
    try:
        b_id = int(request.query_params.get("b", default_b))
    except (TypeError, ValueError):
        b_id = default_b
    if a_id not in standings:
        a_id = pooler_ids[0]
    if b_id not in standings:
        b_id = default_b

    team_a = standings[a_id]
    team_b = standings[b_id]

    slot_rows = [{"slot": slot, "a": team_a["slots"][slot], "b": team_b["slots"][slot]} for slot in SLOTS]

    cat_labels = [
        ("goals", "Buts"), ("assists", "Passes"), ("pp", "Av. numérique"),
        ("sh", "Désavantage"), ("ot", "Prolongation"),
        ("goalie_wins", "Victoires gardien"), ("goalie_so", "Blanchissages"),
        ("team_wins", "Victoires équipe"),
    ]
    cat_rows = [
        {"key": key, "label": label, "a": team_a["categories"][key], "b": team_b["categories"][key]}
        for key, label in cat_labels
    ]

    return templates.TemplateResponse(
        request,
        "comparaison.html",
        {
            "poolers": poolers,
            "a_id": a_id,
            "b_id": b_id,
            "team_a": team_a,
            "team_b": team_b,
            "slot_rows": slot_rows,
            "cat_rows": cat_rows,
        },
    )


async def statuts(request):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, team, pos, note, note_type FROM players "
            "WHERE note IS NOT NULL AND note != '' ORDER BY note_type, name"
        ).fetchall()
        taken_rows = conn.execute(
            "SELECT r.player_id, pl2.name as pooler_name FROM roster r "
            "JOIN poolers pl2 ON pl2.id = r.pooler_id WHERE r.player_id IS NOT NULL"
        ).fetchall()
        taken_by = {r["player_id"]: r["pooler_name"] for r in taken_rows}

    out = []
    for r in rows:
        d = dict(r)
        d["owner"] = taken_by.get(d["id"])
        out.append(d)

    return templates.TemplateResponse(
        request,
        "statuts.html",
        {"rows": out, "note_labels": NOTE_LABELS},
    )


async def admin_alertes(request):
    with get_conn() as conn:
        poolers = conn.execute("SELECT id, name FROM poolers ORDER BY sort_order").fetchall()
    mentions = discord_alerts.get_discord_mentions()
    categories = discord_alerts.get_discord_mention_categories()
    test_results = json.loads(get_meta("discord_mention_test_results") or "{}")
    return templates.TemplateResponse(
        request,
        "admin_alertes.html",
        {
            "webhook_url": get_meta("discord_webhook_url", "") or "",
            "weekly_enabled": get_meta("discord_weekly_enabled", "0") == "1",
            "weekly_last_sent": get_meta("discord_weekly_last_sent"),
            "weekly_last_status": get_meta("discord_weekly_last_status"),
            "test_result": get_meta("discord_test_result"),
            "send_now_result": get_meta("discord_send_now_result"),
            "poolers": poolers,
            "mentions": mentions,
            "mention_categories": categories,
            "category_defs": discord_alerts.MENTION_CATEGORIES,
            "mention_test_results": test_results,
            "known_handles": {
                "Will Tremblay": "Slippy",
                "Fred Blanchette": "iFR3ZLEGENDZ",
                "Max Couillard": "WhiskeyBoys",
                "Max David": "max la terreur",
            },
        },
    )


async def admin_alertes_save(request):
    form = await request.form()
    url = form.get("webhook_url", "").strip()
    enabled = "1" if form.get("weekly_enabled") == "1" else "0"
    set_meta("discord_webhook_url", url)
    set_meta("discord_weekly_enabled", enabled)
    return RedirectResponse("/admin/alertes", status_code=302)


async def admin_alertes_save_mention(request):
    pooler_id = str(request.path_params["pooler_id"])
    form = await request.form()
    raw_id = form.get("discord_id", "").strip()

    mentions = discord_alerts.get_discord_mentions()
    categories = discord_alerts.get_discord_mention_categories()
    if raw_id:
        mentions[pooler_id] = raw_id
        categories[pooler_id] = {
            key: ("1" if form.get(f"cat_{key}") == "1" else "0")
            for key, _label, _hint in discord_alerts.MENTION_CATEGORIES
        }
    else:
        mentions.pop(pooler_id, None)
        categories.pop(pooler_id, None)
    set_meta("discord_mentions", json.dumps(mentions, ensure_ascii=False))
    set_meta("discord_mention_categories", json.dumps(categories, ensure_ascii=False))
    return RedirectResponse("/admin/alertes", status_code=302)


async def admin_alertes_test(request):
    ok, err = await asyncio.to_thread(
        discord_alerts.send_discord,
        "✅ Message de test — si tu vois ceci dans Discord, la connexion fonctionne.",
    )
    set_meta("discord_test_result", "ok" if ok else f"erreur : {err}")
    return RedirectResponse("/admin/alertes", status_code=302)


async def admin_alertes_test_personne(request):
    pooler_id = request.path_params["pooler_id"]
    mentions = discord_alerts.get_discord_mentions()
    discord_id = mentions.get(str(pooler_id))
    with get_conn() as conn:
        row = conn.execute("SELECT name FROM poolers WHERE id = ?", (pooler_id,)).fetchone()
    name = row["name"] if row else "?"

    if not discord_id:
        result = "erreur : aucun ID Discord configuré pour cette personne"
    else:
        content = (
            f"🔔 <@{discord_id}> — test de notification pour **{name}**. "
            "Si tu reçois une alerte (Discord ou sur ton téléphone), ça fonctionne !"
        )
        ok, err = await asyncio.to_thread(discord_alerts.send_discord, content)
        result = "ok" if ok else f"erreur : {err}"

    results = json.loads(get_meta("discord_mention_test_results") or "{}")
    results[str(pooler_id)] = result
    set_meta("discord_mention_test_results", json.dumps(results, ensure_ascii=False))
    return RedirectResponse("/admin/alertes", status_code=302)


async def admin_alertes_send_now(request):
    with get_conn() as conn:
        content, embeds = discord_alerts.build_weekly_digest(conn)
    ok, err = await asyncio.to_thread(discord_alerts.send_discord, content, None, embeds)
    set_meta("discord_send_now_result", "ok" if ok else f"erreur : {err}")
    if ok:
        set_meta("discord_weekly_last_sent", now_iso())
        set_meta("discord_weekly_last_status", "ok")
    return RedirectResponse("/admin/alertes", status_code=302)


async def trigger_refresh(request):
    summary = await asyncio.to_thread(nhl.refresh_all)
    if request.headers.get("accept", "").find("application/json") >= 0:
        return JSONResponse(summary)
    ref = request.headers.get("referer", "/")
    return RedirectResponse(ref, status_code=302)


async def healthz(request):
    return JSONResponse({"ok": True})


@asynccontextmanager
async def lifespan(app):
    init_db()
    task = asyncio.create_task(nhl.refresh_loop())
    digest_task = asyncio.create_task(discord_alerts.weekly_digest_loop())
    try:
        yield
    finally:
        task.cancel()
        digest_task.cancel()


routes = [
    Route("/", dashboard),
    Route("/equipe/{pooler_id}", team_detail),
    Route("/setup", setup),
    Route("/setup/assign", setup_assign, methods=["POST"]),
    Route("/ajustements", ajustements),
    Route("/ajustements/add", ajustements_add, methods=["POST"]),
    Route("/ajustements/{adj_id}/supprimer", ajustements_delete, methods=["POST"]),
    Route("/admin/stats", admin_stats),
    Route("/admin/stats/skater/{player_id}", admin_update_skater, methods=["POST"]),
    Route("/admin/stats/goalie/{player_id}", admin_update_goalie, methods=["POST"]),
    Route("/admin/stats/team/{team_code}", admin_update_team, methods=["POST"]),
    Route("/admin/note/{player_id}", admin_update_note, methods=["POST"]),
    Route("/admin/alertes", admin_alertes),
    Route("/admin/alertes/save", admin_alertes_save, methods=["POST"]),
    Route("/admin/alertes/mention/{pooler_id}", admin_alertes_save_mention, methods=["POST"]),
    Route("/admin/alertes/test", admin_alertes_test, methods=["POST"]),
    Route("/admin/alertes/envoyer", admin_alertes_send_now, methods=["POST"]),
    Route("/admin/alertes/tester/{pooler_id}", admin_alertes_test_personne, methods=["POST"]),
    Route("/stats-lnh", stats_lnh),
    Route("/calendrier", calendrier),
    Route("/comparaison", comparaison),
    Route("/statuts", statuts),
    Route("/api/search", api_search),
    Route("/refresh", trigger_refresh, methods=["POST"]),
    Route("/healthz", healthz),
    Mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static"),
]

app = Starlette(routes=routes, lifespan=lifespan)
