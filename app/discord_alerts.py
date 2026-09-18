"""Discord webhook alerts for the pool.

Uses a Discord "incoming webhook" — a URL Vincent creates himself in a
channel's Integrations settings and pastes into /admin/alertes. No bot to
host, no token to manage: sending an alert is just one HTTP POST with a
JSON body, same pattern as every other outbound call in this app (see
nhl.py). Every call here is wrapped so a bad/missing URL or a Discord
outage is recorded and shown in the admin page, but never raises — a
Discord problem must never stop refresh_all() or the weekly digest job
from doing its real job (the pool's own stats).
"""
import json
import urllib.error
import urllib.request
from datetime import date as _date, timedelta as _timedelta

from . import scoring
from .db import get_conn, get_meta, set_meta, now_iso
from .teams import team_color, team_name

TIMEOUT = 10
MAX_CONTENT = 1900  # Discord's hard cap on a message is 2000 chars; leave headroom
RANK_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣"}
DEFAULT_COLOR = 0x5865F2  # Discord blurple — used when there's no leader team color to grab


def _post(webhook_url: str, content: str, embeds: list = None) -> int:
    body = {"username": "Pool Les chums"}
    if content:
        body["content"] = content[:MAX_CONTENT]
    if embeds:
        body["embeds"] = embeds
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "pool-les-chums/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status


def send_discord(content: str = "", webhook_url: str = None, embeds: list = None):
    """Best-effort send to the configured (or given) webhook.

    `content` is plain text (this is what actually triggers a notification
    for anyone @mentioned in it — Discord does not reliably ping for a
    mention that only appears inside an embed). `embeds` is an optional
    list of Discord embed dicts for the rich, styled part of the message.

    Returns (ok: bool, error: str | None). Never raises — a missing URL,
    a network error, or a non-2xx response all come back as (False, "..."),
    for the caller to log/display rather than crash on.
    """
    url = webhook_url or get_meta("discord_webhook_url")
    if not url:
        return False, "aucune URL de webhook configurée"
    if not content and not embeds:
        return False, "message vide"
    try:
        status = _post(url, content, embeds)
        return (200 <= status < 300), None
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def get_discord_mentions() -> dict:
    """{pooler_id (str) -> Discord numeric user ID (str)}, as configured on
    /admin/alertes. Discord only turns a mention into a real notification
    for the exact `<@USER_ID>` form — plain "@username" text in a webhook
    message does not ping anyone — so this stores IDs, not handles."""
    raw = get_meta("discord_mentions")
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


MENTION_CATEGORIES = [
    ("resume", "📊 Résumé hebdo", "mentionné sur sa ligne dans le résumé du lundi"),
    ("blessures", "🩹 Blessures", "pingé dès qu'un de ses joueurs est signalé blessé"),
]


def get_discord_mention_categories() -> dict:
    """{pooler_id (str) -> {category (str) -> "1"/"0"}}, as configured on
    /admin/alertes. Lets each pooler subscribe to specific kinds of pings
    independently — e.g. on for the weekly digest but off for injuries, or
    the reverse — without touching their Discord ID. A category missing
    for a pooler defaults to enabled (opt-out, not opt-in) once that
    pooler has an ID configured at all."""
    raw = get_meta("discord_mention_categories")
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def mention_category_on(categories: dict, pooler_id, category: str) -> bool:
    return categories.get(str(pooler_id), {}).get(category, "1") != "0"


def notify_injury(owners, player_name: str, detail: str = ""):
    """Ping every owning pooler (usually just one) who has the "blessures"
    category on and a Discord ID configured, right when one of their
    rostered players is newly flagged injured. `owners` is an iterable of
    rows/dicts with pooler_id and pooler_name. Best-effort per person — one
    person's send failing never stops the others, and this never raises."""
    mentions = get_discord_mentions()
    categories = get_discord_mention_categories()
    results = []
    for row in owners:
        pid = str(row["pooler_id"])
        discord_id = mentions.get(pid)
        if not discord_id or not mention_category_on(categories, pid, "blessures"):
            continue
        text = f"🩹 <@{discord_id}> — **{player_name}** ({row['pooler_name']}) vient d'être signalé blessé"
        if detail:
            text += f" — {detail}"
        ok, err = send_discord(text)
        results.append((pid, ok, err))
    return results


def _closest_snapshot_at_or_before(conn, days_ago: int):
    """Timestamp of the most recent snapshot batch taken at least
    `days_ago` days back, or None if history doesn't go back that far yet."""
    cutoff = (_date.today() - _timedelta(days=days_ago)).isoformat() + "T23:59:59"
    row = conn.execute(
        "SELECT DISTINCT taken_at FROM pooler_snapshots WHERE taken_at <= ? "
        "ORDER BY taken_at DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    return row["taken_at"] if row else None


def build_weekly_digest(conn):
    """(content, embed) for the weekly digest.

    `content` is a short plain-text line that @mentions whoever has
    notifications on — this is the part that actually makes phones buzz.
    `embed` is a single styled Discord embed (colored bar, title, fields)
    carrying the actual standings/hot-player/schedule content — the "fun"
    part with emojis, laid out as fields rather than a wall of text.
    Degrades gracefully piece by piece: no history yet, or no schedule
    cached yet, just means that field is skipped, never an error.
    """
    standings = scoring.compute_all(conn)
    ranked = sorted(standings.items(), key=lambda kv: kv[1]["total"], reverse=True)
    mentions = get_discord_mentions()
    categories = get_discord_mention_categories()

    week_ts = _closest_snapshot_at_or_before(conn, 7)
    prev_totals = {}
    if week_ts:
        prev_totals = {
            r["pooler_id"]: r["total"]
            for r in conn.execute(
                "SELECT pooler_id, total FROM pooler_snapshots WHERE taken_at = ?", (week_ts,)
            ).fetchall()
        }
    prev_rank = {}
    if prev_totals:
        prev_order = sorted(prev_totals.items(), key=lambda kv: kv[1], reverse=True)
        prev_rank = {pid: i + 1 for i, (pid, _) in enumerate(prev_order)}

    # -- content: who gets pinged --
    ping_ids = []
    for pid, _t in ranked:
        discord_id = mentions.get(str(pid))
        if discord_id and mention_category_on(categories, pid, "resume"):
            ping_ids.append(discord_id)
    content = ""
    if ping_ids:
        content = "🏒 Nouveau résumé hebdo du pool ! " + " ".join(f"<@{i}>" for i in ping_ids)

    # -- embed: standings field --
    standings_lines = []
    for i, (pid, t) in enumerate(ranked, start=1):
        medal = RANK_MEDALS.get(i, f"{i}.")
        delta_txt = ""
        if pid in prev_totals:
            delta = t["total"] - prev_totals[pid]
            sign = "+" if delta >= 0 else ""
            delta_txt = f" ({sign}{delta:.0f})"
            old_rank = prev_rank.get(pid)
            if old_rank and old_rank != i:
                arrow = "📈" if old_rank > i else "📉"
                delta_txt += f" {arrow}"
        standings_lines.append(f"{medal} **{t['name']}** — {t['total']:.0f} pts{delta_txt}")
    fields = [{"name": "🏆 Classement", "value": "\n".join(standings_lines), "inline": False}]

    if not week_ts:
        fields.append({
            "name": "🆕 Premier résumé",
            "value": "Pas encore une semaine d'historique pour comparer — reviens lundi prochain pour voir le mouvement !",
            "inline": False,
        })
    else:
        prev_points = {
            r["player_id"]: r["points"]
            for r in conn.execute(
                "SELECT player_id, points FROM player_snapshots WHERE taken_at = ?", (week_ts,)
            ).fetchall()
        }
        flat = scoring.flat_roster(standings)
        best = None
        for r in flat:
            pid2 = r["player"]["id"]
            if pid2 in prev_points:
                delta = r["points"] - prev_points[pid2]
                if delta > 0 and (best is None or delta > best["delta"]):
                    best = {**r, "delta": delta}
        if best:
            fields.append({
                "name": "🔥 Joueur le plus chaud",
                "value": f"**{best['player']['name']}** ({best['pooler_name']}) — +{best['delta']:.0f} pts cette semaine",
                "inline": True,
            })

    raw = get_meta("schedule_week")
    schedule = json.loads(raw) if raw else []
    if schedule:
        owners_by_team = {}
        for pooler_id, t in standings.items():
            for slot, s in t["slots"].items():
                pl = s["player"]
                if pl:
                    owners_by_team.setdefault(pl["team"], set()).add(t["name"])

        team_dates = {}
        total_games = 0
        for day in schedule:
            for g in day.get("games", []):
                total_games += 1
                team_dates.setdefault(g["away"], set()).add(day["date"])
                team_dates.setdefault(g["home"], set()).add(day["date"])

        b2b_teams = set()
        for team, dates in team_dates.items():
            sorted_dates = sorted(dates)
            for j in range(1, len(sorted_dates)):
                d1 = _date.fromisoformat(sorted_dates[j - 1])
                d2 = _date.fromisoformat(sorted_dates[j])
                if (d2 - d1).days == 1:
                    b2b_teams.add(team)

        schedule_value = f"🥅 {total_games} matchs à venir dans la LNH cette semaine"
        b2b_owned = sorted({name for team in b2b_teams for name in owners_by_team.get(team, [])})
        if b2b_owned:
            schedule_value += f"\n⏩ Back-to-back à surveiller : {', '.join(b2b_owned)}"
        fields.append({"name": "📅 Cette semaine", "value": schedule_value, "inline": True})

    # A little personality: color the embed with the current leader's NHL
    # team, so the message looks a bit different pool to pool / week to
    # week instead of always the same flat blue.
    color = DEFAULT_COLOR
    leader_team_name = ""
    if ranked:
        leader = ranked[0][1]
        t1 = leader["slots"].get("T1", {}).get("player")
        if t1 and t1.get("team"):
            hex_color = team_color(t1["team"])
            leader_team_name = team_name(t1["team"])
            try:
                color = int(hex_color.lstrip("#"), 16)
            except (ValueError, AttributeError):
                pass

    embed = {
        "title": "🏒 Résumé hebdo — Pool Les chums",
        "description": f"Semaine du {_date.today().isoformat()} • en tête : {leader_team_name}" if leader_team_name else "",
        "color": color,
        "fields": fields,
        "footer": {"text": "Pool Les chums 2026-27 🏒 résumé automatique, chaque lundi 8h"},
    }
    return content, [embed]


async def weekly_digest_loop():
    """Background task: sleep until next Monday 8h (heure de Montréal), send
    the weekly digest if configured+enabled, then repeat every 7 days.
    Mirrors nhl.refresh_loop()'s shape — a long-lived asyncio task started
    once in the app's lifespan."""
    import asyncio
    from datetime import datetime

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Montreal")
    except Exception:  # noqa: BLE001
        tz = None

    while True:
        now = datetime.now(tz) if tz else datetime.now()
        days_ahead = (0 - now.weekday()) % 7  # Monday == 0
        target = (now + _timedelta(days=days_ahead)).replace(hour=8, minute=0, second=0, microsecond=0)
        if target <= now:
            target += _timedelta(days=7)
        await asyncio.sleep(max((target - now).total_seconds(), 1))

        try:
            if get_meta("discord_weekly_enabled") == "1" and get_meta("discord_webhook_url"):
                with get_conn() as conn:
                    content, embeds = build_weekly_digest(conn)
                ok, err = send_discord(content, embeds=embeds)
                set_meta("discord_weekly_last_sent", now_iso())
                set_meta("discord_weekly_last_status", "ok" if ok else f"erreur: {err}")
        except Exception as e:  # noqa: BLE001
            set_meta("discord_weekly_last_status", f"erreur: {e}")
