#!/usr/bin/env python3
"""
Build a ZenGM Baseball League File (JSON) from the public MLB Stats API.

Snapshot date: defaults to 2025-12-25 (offseason).
Roster source: 40-man roster at snapshot date; if <35 players, fills from fullRoster.
Ratings: derived heuristically from MLB Stats API 2025 season stats.

References:
- ZenGM Baseball customization docs: https://zengm.com/baseball/manual/customization/
- ZenGM Baseball league schema: https://baseball.zengm.com/files/league-schema.json
"""

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API = "https://statsapi.mlb.com/api/v1"


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def scale(x: float, x0: float, x1: float, y0: float = 0, y1: float = 100) -> float:
    if x1 == x0:
        return (y0 + y1) / 2
    t = (x - x0) / (x1 - x0)
    return clamp(y0 + t * (y1 - y0), min(y0, y1), max(y0, y1))


def inv_scale(x: float, x0: float, x1: float, y0: float = 0, y1: float = 100) -> float:
    # Larger x -> smaller score
    return scale(x, x0, x1, y1, y0)


def to_int_rating(x: float) -> int:
    return int(round(clamp(x, 0, 100)))


def fetch_json(path: str, *, params: dict[str, Any] | None = None, retries: int = 3) -> Any:
    url = f"{API}{path}"
    if params:
        url = f"{url}?{urlencode(params, doseq=True)}"

    headers = {
        "User-Agent": "zengm-roster-builder/1.0 (+https://baseball.zengm.com/)",
        "Accept": "application/json",
    }
    req = Request(url, headers=headers)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            # brief backoff
            time.sleep(0.75 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}") from last_err


def parse_ip(ip_str: str | None) -> float:
    """
    MLB Stats API inningsPitched format is like "123.2" where .1 == 1 out and .2 == 2 outs.
    """
    if not ip_str:
        return 0.0
    if "." not in ip_str:
        try:
            return float(ip_str)
        except ValueError:
            return 0.0
    whole, frac = ip_str.split(".", 1)
    try:
        w = int(whole)
        f = int(frac)
    except ValueError:
        return 0.0
    outs = 0
    if f == 0:
        outs = 0
    elif f == 1:
        outs = 1
    elif f == 2:
        outs = 2
    else:
        outs = 0
    return w + outs / 3.0


def inches_from_height_str(height: str | None) -> int | None:
    # "6' 7\"" or "6'7\"" or sometimes None
    if not height:
        return None
    s = height.replace(" ", "")
    if "'" not in s:
        return None
    try:
        feet_s, rest = s.split("'", 1)
        feet = int(feet_s)
        inches_s = rest.replace('"', "")
        inches = int(inches_s) if inches_s else 0
        return feet * 12 + inches
    except Exception:
        return None


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except Exception:
        return default


def pick_stat_split(person: dict[str, Any], group: str) -> dict[str, Any] | None:
    # The hydrate stats format is inconsistent, but generally:
    # person["stats"] = [{"type": {...}, "group": {...}, "splits": [{"stat": {...}}]}]
    for block in person.get("stats", []) or []:
        g = (block.get("group") or {}).get("displayName")
        if g and g.lower() == group.lower():
            splits = block.get("splits") or []
            if splits:
                return splits[0].get("stat") or {}
    return None


def rating_from_hitter_stats(h: dict[str, Any], primary_pos: str) -> tuple[int, int, int, int]:
    """
    Returns (con, hpw, eye, spd) 0-100.
    Based on 2025 season hitting stats.
    """
    ab = safe_int(h.get("atBats"))
    hits = safe_int(h.get("hits"))
    doubles = safe_int(h.get("doubles"))
    triples = safe_int(h.get("triples"))
    hr = safe_int(h.get("homeRuns"))
    bb = safe_int(h.get("baseOnBalls"))
    so = safe_int(h.get("strikeOuts"))
    sb = safe_int(h.get("stolenBases"))
    cs = safe_int(h.get("caughtStealing"))
    pa = safe_int(h.get("plateAppearances"), default=ab + bb + so)

    avg = safe_float(h.get("avg"))
    slg = safe_float(h.get("slg"))
    iso = max(0.0, slg - avg)

    bb_rate = bb / pa if pa > 0 else 0.0
    # SB per 600 PA with light penalty for getting caught
    sb_600 = (sb - 0.5 * cs) * 600 / pa if pa > 0 else 0.0

    con = to_int_rating(scale(avg, 0.200, 0.330, 30, 90))
    hpw = to_int_rating(scale(iso, 0.050, 0.350, 20, 90))
    eye = to_int_rating(scale(bb_rate, 0.03, 0.16, 25, 90))
    spd = to_int_rating(scale(sb_600, 0, 40, 30, 90))

    # Position-informed tweaks for speed (catchers/1B usually slower)
    if primary_pos in {"C", "1B"}:
        spd = max(20, int(round(spd * 0.85)))
    elif primary_pos in {"SS", "CF", "2B"}:
        spd = min(100, int(round(spd * 1.05)))

    return con, hpw, eye, spd


def rating_from_pitcher_stats(p: dict[str, Any], role_hint: str) -> tuple[int, int, int, int]:
    """
    Returns (ppw, ctl, mov, endu) 0-100.
    Based on 2025 season pitching stats.
    """
    ip = parse_ip(p.get("inningsPitched"))
    so = safe_int(p.get("strikeOuts"))
    bb = safe_int(p.get("baseOnBalls"))
    hr = safe_int(p.get("homeRuns"))
    gs = safe_int(p.get("gamesStarted"))

    k9 = so * 9 / ip if ip > 0 else 0.0
    bb9 = bb * 9 / ip if ip > 0 else 0.0
    hr9 = hr * 9 / ip if ip > 0 else 0.0

    ppw = to_int_rating(scale(k9, 5.0, 13.0, 30, 90))
    ctl = to_int_rating(inv_scale(bb9, 1.0, 5.0, 30, 90))
    mov = to_int_rating(inv_scale(hr9, 0.5, 1.8, 30, 90))

    # Endurance: starters by IP/GS, relievers by total IP
    if gs >= 5 or role_hint == "SP":
        ip_per_start = ip / gs if gs > 0 else 0.0
        endu = to_int_rating(scale(ip_per_start, 4.0, 7.0, 40, 90))
    else:
        endu = to_int_rating(scale(ip, 20.0, 80.0, 35, 75))

    return ppw, ctl, mov, endu


def defensive_baseline(primary_pos: str) -> tuple[int, int, int, int]:
    """
    Returns (gnd, fly, thr, cat) baseline 0-100 before errors adjustment.
    """
    # Conservative defaults; ZenGM will still apply position penalties if you play someone OOP.
    if primary_pos == "P":
        return 45, 45, 45, 0
    if primary_pos == "C":
        return 50, 50, 50, 70
    if primary_pos in {"SS", "2B"}:
        return 65, 55, 60, 0
    if primary_pos == "3B":
        return 60, 50, 65, 0
    if primary_pos == "1B":
        return 55, 45, 40, 0
    if primary_pos in {"CF"}:
        return 55, 70, 55, 0
    if primary_pos in {"LF", "RF", "OF"}:
        return 50, 65, 60 if primary_pos == "RF" else 55, 0
    if primary_pos == "DH":
        return 40, 40, 40, 0
    # fallback
    return 50, 50, 50, 0


def fielding_error_adjustment(f: dict[str, Any] | None) -> float:
    if not f:
        return 0.0
    chances = safe_int(f.get("chances"))
    errors = safe_int(f.get("errors"))
    if chances <= 0:
        return 0.0
    err_rate = errors / chances
    # 0% errors => 0 penalty; 3% errors => about -10; clamp.
    return clamp(scale(err_rate, 0.0, 0.03, 0, -10), -12, 0)


def build_teams(snapshot_season: int) -> list[dict[str, Any]]:
    data = fetch_json("/teams", params={"sportId": 1, "season": snapshot_season})
    teams = data.get("teams", [])

    # Stable ordering: by MLB abbreviation
    teams_sorted = sorted(teams, key=lambda t: (t.get("abbreviation") or t.get("teamName") or t.get("name") or ""))

    div_map = {
        "American League East": 0,
        "American League Central": 1,
        "American League West": 2,
        "National League East": 3,
        "National League Central": 4,
        "National League West": 5,
    }

    out: list[dict[str, Any]] = []
    for i, t in enumerate(teams_sorted):
        league = ((t.get("league") or {}).get("name")) or ""
        division = ((t.get("division") or {}).get("name")) or ""
        cid = 0 if league == "American League" else 1
        did = div_map.get(division, 0)
        region = t.get("locationName") or (t.get("name", "").split(" ", 1)[0] if t.get("name") else "Team")
        name = t.get("teamName") or (t.get("name", "").split(" ", 1)[-1] if t.get("name") else f"Team{i}")
        # MLB Stats API sometimes uses 2-letter abbreviations (AZ/SF/SD). Prefer 3-letter when possible.
        abbrev = t.get("abbreviation") or (t.get("abbrev") or f"T{i:02d}")
        if isinstance(abbrev, str) and len(abbrev) != 3:
            team_code = (t.get("teamCode") or "").upper()
            if len(team_code) == 3:
                abbrev = team_code
        out.append(
            {
                "tid": i,
                "cid": cid,
                "did": did,
                "region": region,
                "name": name,
                "abbrev": abbrev,
                # Note: not including population/stadium/logos to avoid guessing.
            }
        )
    return out


def fetch_roster_person_ids(team_id: int, snapshot_date: str) -> list[int]:
    r40 = fetch_json(
        f"/teams/{team_id}/roster",
        params={"rosterType": "40Man", "date": snapshot_date},
    )
    roster = r40.get("roster", []) or []
    ids = [safe_int((x.get("person") or {}).get("id")) for x in roster]
    ids = [i for i in ids if i > 0]

    if len(ids) >= 35:
        return ids

    # Fill to 35 (ZenGM manual warns <35 causes problems) from fullRoster.
    full = fetch_json(
        f"/teams/{team_id}/roster",
        params={"rosterType": "fullRoster", "date": snapshot_date},
    )
    existing = set(ids)
    for x in full.get("roster", []) or []:
        pid = safe_int((x.get("person") or {}).get("id"))
        if pid > 0 and pid not in existing:
            ids.append(pid)
            existing.add(pid)
        if len(ids) >= 35:
            break
    return ids


def chunked(seq: list[int], size: int) -> list[list[int]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def fetch_people(people_ids: list[int], stats_season: int) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for chunk in chunked(people_ids, 200):
        params = {
            "personIds": ",".join(str(x) for x in chunk),
            # pull core bio + season totals for three stat groups
            "hydrate": f"stats(group=[hitting,pitching,fielding],type=[season],season={stats_season})",
        }
        data = fetch_json("/people", params=params)
        for p in data.get("people", []) or []:
            pid = safe_int(p.get("id"))
            if pid > 0:
                out[pid] = p
        # be nice to the API
        time.sleep(0.1)
    return out


def playing_time_score(person: dict[str, Any]) -> tuple[float, float, bool]:
    """
    Returns (pa, ip, is_pitcher).
    Used only to pick a projected 26-man roster from offseason 40-man rosters.
    """
    primary_pos = ((person.get("primaryPosition") or {}).get("abbreviation")) or "?"
    hit_stats = pick_stat_split(person, "hitting") or {}
    pit_stats = pick_stat_split(person, "pitching") or {}

    pa = safe_float(hit_stats.get("plateAppearances"), 0.0)
    ip = parse_ip(pit_stats.get("inningsPitched")) if pit_stats else 0.0
    is_pitcher = primary_pos == "P" or ip >= 10.0
    return pa, ip, is_pitcher


def select_projected_active_roster(
    pids: list[int],
    people: dict[int, dict[str, Any]],
    roster_size: int,
    *,
    target_pitchers: int = 13,
) -> list[int]:
    """
    ZenGM Baseball does not have a minors/rights system like MLB. If you want a hard 26-man roster
    in ZenGM, you must only assign 26 players to each team.

    Because 2025-12-25 is the offseason, the real 26-man active rosters for 2026 are not known.
    So we select a *projected* 26-man roster from the team's 40-man roster using 2025 playing time.
    """
    # De-dup while preserving order
    seen: set[int] = set()
    uniq: list[int] = []
    for pid in pids:
        if pid not in seen:
            uniq.append(pid)
            seen.add(pid)

    scored: list[tuple[int, float, float, bool]] = []
    for pid in uniq:
        person = people.get(pid)
        if not person:
            scored.append((pid, 0.0, 0.0, False))
            continue
        pa, ip, is_p = playing_time_score(person)
        scored.append((pid, pa, ip, is_p))

    pitchers = [(pid, ip) for pid, _pa, ip, is_p in scored if is_p]
    hitters = [(pid, pa) for pid, pa, _ip, is_p in scored if not is_p]

    pitchers.sort(key=lambda x: x[1], reverse=True)
    hitters.sort(key=lambda x: x[1], reverse=True)

    want_p = min(target_pitchers, roster_size)
    want_h = max(0, roster_size - want_p)

    chosen: list[int] = [pid for pid, _ in pitchers[:want_p]] + [pid for pid, _ in hitters[:want_h]]

    # Fill any shortfall from remaining best-by-playing-time regardless of type.
    if len(chosen) < roster_size:
        remaining = [x for x in scored if x[0] not in set(chosen)]
        # combined score: PA + 3*IP (roughly makes 50 IP comparable to 150 PA)
        remaining.sort(key=lambda x: (x[1] + 3.0 * x[2]), reverse=True)
        for pid, _pa, _ip, _is_p in remaining:
            chosen.append(pid)
            if len(chosen) >= roster_size:
                break

    return chosen[:roster_size]


def build_player(pid: int, person: dict[str, Any], tid: int, season_for_ratings: int) -> dict[str, Any]:
    first = person.get("firstName") or (person.get("useName") or "").strip() or "Player"
    last = person.get("lastName") or (person.get("lastInitName") or "").split(" ", 1)[-1].strip() or "Unknown"
    primary_pos = ((person.get("primaryPosition") or {}).get("abbreviation")) or "?"

    height_in = inches_from_height_str(person.get("height"))
    # Map typical MLB range (5'5"=65 to 6'7"=79) into ~30..70.
    hgt_rating = 50
    if height_in is not None:
        hgt_rating = to_int_rating(scale(height_in, 65, 79, 35, 70))

    hit_stats = pick_stat_split(person, "hitting") or {}
    pit_stats = pick_stat_split(person, "pitching") or {}
    fld_stats = pick_stat_split(person, "fielding")

    # Decide role: primary position first, then by presence of pitching stats/IP.
    ip = parse_ip(pit_stats.get("inningsPitched")) if pit_stats else 0.0
    is_pitcher = primary_pos == "P" or ip >= 10.0

    # Role hint for endurance calc
    role_hint = "RP"
    if safe_int(pit_stats.get("gamesStarted")) >= 10:
        role_hint = "SP"

    if is_pitcher:
        ppw, ctl, mov, endu = rating_from_pitcher_stats(pit_stats, role_hint)
        # Pitchers still need batting/fielding numbers; keep conservative.
        con, hpw, eye, spd = 20, 15, 15, 25
    else:
        con, hpw, eye, spd = rating_from_hitter_stats(hit_stats, primary_pos)
        # Position players should have weak pitching.
        ppw, ctl, mov, endu = 15, 15, 15, 20

    gnd, fly, thr, cat = defensive_baseline(primary_pos)
    adj = fielding_error_adjustment(fld_stats)
    gnd = to_int_rating(gnd + adj)
    fly = to_int_rating(fly + adj)

    # If catcher, try to incorporate CS% if available in hitting splits (sometimes present).
    if primary_pos == "C":
        cs = safe_int(hit_stats.get("caughtStealing"))
        sb = safe_int(hit_stats.get("stolenBases"))
        attempts = cs + sb
        if attempts >= 10:
            cs_rate = cs / attempts
            cat = to_int_rating(scale(cs_rate, 0.15, 0.45, 45, 90))

    ratings_obj = {
        "hgt": hgt_rating,
        "spd": spd,
        "hpw": hpw,
        "con": con,
        "eye": eye,
        "gnd": gnd,
        "fly": fly,
        "thr": thr,
        "cat": cat,
        "ppw": ppw,
        "ctl": ctl,
        "mov": mov,
        "endu": endu,
        # Leave ovr/pot/pos/skills out; ZenGM will compute them on import (per docs).
    }

    # Keep player objects minimal to maximize import compatibility.
    out: dict[str, Any] = {
        "firstName": first,
        "lastName": last,
        "tid": tid,
        "ratings": [ratings_obj],
    }

    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-date", default="2025-12-25")
    parser.add_argument("--stats-season", type=int, default=2025)
    parser.add_argument("--starting-season", type=int, default=2026)
    parser.add_argument("--roster-size", type=int, default=26)
    parser.add_argument("--out", default="/workspace/output/zengm_mlb_2025-12-25.json")
    parser.add_argument("--validate-schema", default="/workspace/output/league-schema.json")
    args = parser.parse_args()

    # Basic validation of date string
    try:
        date.fromisoformat(args.snapshot_date)
    except ValueError as e:
        raise SystemExit(f"Invalid --snapshot-date: {args.snapshot_date}") from e

    # Build teams and determine mapping from MLB API teamId -> ZenGM tid
    teams_raw = fetch_json("/teams", params={"sportId": 1, "season": args.stats_season}).get("teams", []) or []
    teams_sorted = sorted(teams_raw, key=lambda t: (t.get("abbreviation") or t.get("teamName") or t.get("name") or ""))
    mlb_team_id_to_tid: dict[int, int] = {}
    for i, t in enumerate(teams_sorted):
        mlb_team_id_to_tid[safe_int(t.get("id"))] = i

    teams = build_teams(args.stats_season)

    # Rosters (we use 40-man snapshots as the source of truth for team control in the offseason,
    # then select a projected "active roster" of args.roster_size players per team).
    team_to_person_ids: dict[int, list[int]] = {}
    all_person_ids: set[int] = set()
    for t in teams_sorted:
        mlb_team_id = safe_int(t.get("id"))
        tid = mlb_team_id_to_tid[mlb_team_id]
        ids = fetch_roster_person_ids(mlb_team_id, args.snapshot_date)
        team_to_person_ids[tid] = ids
        all_person_ids.update(ids)

    people = fetch_people(sorted(all_person_ids), args.stats_season)

    team_active: dict[int, list[int]] = {}
    team_inactive: dict[int, list[int]] = {}
    for tid, pids in team_to_person_ids.items():
        active = select_projected_active_roster(pids, people, args.roster_size)
        active_set = set(active)
        inactive = [pid for pid in pids if pid not in active_set]
        team_active[tid] = active
        team_inactive[tid] = inactive

    players: list[dict[str, Any]] = []
    missing_people: list[int] = []
    # Active roster players stay on their teams. Inactive 40-man players become free agents,
    # because ZenGM Baseball does not support MLB-style team control over non-roster players.
    for tid, pids in team_active.items():
        for pid in pids:
            person = people.get(pid)
            if not person:
                missing_people.append(pid)
                continue
            players.append(build_player(pid, person, tid, args.stats_season))

    for tid, pids in team_inactive.items():
        for pid in pids:
            person = people.get(pid)
            if not person:
                missing_people.append(pid)
                continue
            players.append(build_player(pid, person, -1, args.stats_season))

    if missing_people:
        print(f"Warning: missing {len(missing_people)} people records", file=sys.stderr)

    league: dict[str, Any] = {
        "version": 43,
        "startingSeason": args.starting_season,
        "gameAttributes": {
            "season": args.starting_season,
            "startingSeason": args.starting_season,
            "phase": 0,  # preseason
            "userTid": 0,
            "minRosterSize": args.roster_size,
            "maxRosterSize": args.roster_size,
            "confs": [
                {"cid": 0, "name": "American League"},
                {"cid": 1, "name": "National League"},
            ],
            "divs": [
                {"did": 0, "cid": 0, "name": "East"},
                {"did": 1, "cid": 0, "name": "Central"},
                {"did": 2, "cid": 0, "name": "West"},
                {"did": 3, "cid": 1, "name": "East"},
                {"did": 4, "cid": 1, "name": "Central"},
                {"did": 5, "cid": 1, "name": "West"},
            ],
        },
        "teams": teams,
        "players": players,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(league, ensure_ascii=False, indent=2), encoding="utf-8")

    # Optional schema validation (best-effort)
    try:
        import jsonschema  # type: ignore

        schema_path = Path(args.validate_schema)
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.validate(instance=league, schema=schema)
        print(f"OK: wrote + validated {out_path}", file=sys.stderr)
    except ModuleNotFoundError:
        print("Note: jsonschema not installed, skipping validation (pip install jsonschema)", file=sys.stderr)
    except Exception as e:
        print(f"Schema validation failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

