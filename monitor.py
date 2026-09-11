#!/usr/bin/env python3
"""
JFK <-> SYD award monitor, open-date mode, four cabin views.

No fixed travel dates. The monitor sweeps the entire bookable calendar,
stores every observation as a tick, pairs every legal outbound/return
combination, and ranks them by total miles per person. Each cabin gets its
own leaderboard, its own thresholds and its own running best, so a cheap
economy print never hides a business one.

Two scan modes share one API budget:

  sweep  full calendar, both directions, every few hours
  focus  only the months around the current best combinations, every 15 min

Usage:
    python monitor.py --self-test         offline test, no network, no key
    python monitor.py --probe             one live call, dumps the raw response
    python monitor.py --sweep             one full calendar sweep
    python monitor.py --once              one focus poll
    python monitor.py --loop              scheduler, alternates sweep and focus
    python monitor.py --best              four leaderboards, one per cabin
    python monitor.py --best --cabin J    one cabin, --limit N rows per section
    python monitor.py --calibrate         propose thresholds from stored history
    python monitor.py --stats             observation history summary
    python monitor.py --budget            API call plan, theoretical and measured
    python monitor.py --test-alert        send one Pushover message
Add --dry-run to print alerts instead of sending them.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import redirect_stdout
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
import yaml

LOG = logging.getLogger("awardmon")

# Canonical cabin order. Every per-cabin loop in this file walks this tuple so
# the config file, the leaderboard and the alerts all agree on it.
CABIN_ORDER = ("Y", "W", "J", "F")
CABIN_NAMES = {"Y": "Economy", "W": "Premium Economy", "J": "Business", "F": "First"}

# seats.aero names its response fields with one letter cabin codes but filters
# rows with word values. Confirmed against two public client libraries, not
# against a live call. --probe prints the HTTP status if this turns out wrong.
CABIN_QUERY_VALUES = {"Y": "economy", "W": "premium", "J": "business", "F": "first"}

# Candidate daily quota headers. requests matches header names case
# insensitively, so these three cover the variants seen in the wild. The one
# that actually matches is remembered in state and shown by --budget.
QUOTA_HEADERS = ("x-ratelimit-remaining", "x-quota-remaining", "ratelimit-remaining")

# --calibrate refuses to propose thresholds on thinner history than this.
CALIBRATE_MIN_COMBOS = 200
CALIBRATE_MIN_DAYS = 14

# How deep into each cabin's ranked list evaluate() looks for alerts.
ALERT_SCAN_DEPTH = 20

FARE_BRAND_REMINDER = "Check fare brand before you commit. Main Basic is non-refundable after 24h."


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Leg:
    availability_id: str
    source: str
    origin: str
    destination: str
    depart_date: str
    cabin: str
    miles: int
    taxes_usd: float
    seats: int
    airlines: str
    direct: bool
    last_seen: str
    observed_at: str
    taxes_currency: str = "USD"

    def fingerprint(self) -> str:
        raw = f"{self.source}|{self.origin}|{self.destination}|{self.depart_date}|{self.cabin}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


@dataclass
class RoundTrip:
    outbound: Leg
    inbound: Leg

    @property
    def total_miles(self) -> int:
        return self.outbound.miles + self.inbound.miles

    @property
    def total_taxes(self) -> float:
        return round(self.outbound.taxes_usd + self.inbound.taxes_usd, 2)

    @property
    def nights(self) -> int:
        a = datetime.strptime(self.outbound.depart_date, "%Y-%m-%d")
        b = datetime.strptime(self.inbound.depart_date, "%Y-%m-%d")
        return (b - a).days

    @property
    def min_seats(self) -> int:
        pair = (self.outbound.seats, self.inbound.seats)
        return 0 if 0 in pair else min(pair)

    @property
    def mixed(self) -> bool:
        return self.outbound.cabin != self.inbound.cabin

    @property
    def cabin(self) -> str:
        """Cabin this trip is ranked under. A mixed pairing ranks under its
        lower cabin, because the experience is bounded by the worse leg."""
        if not self.mixed:
            return self.outbound.cabin
        return min((self.outbound.cabin, self.inbound.cabin), key=_cabin_rank)

    @property
    def cabin_code(self) -> str:
        return f"{self.outbound.cabin}/{self.inbound.cabin}" if self.mixed else self.outbound.cabin

    @property
    def route(self) -> str:
        return f"{self.outbound.origin}-{self.outbound.destination}"

    def key(self) -> str:
        """Dedupe key. Both fingerprints carry the cabin, so the same dates
        in a different cabin are a different alert."""
        raw = f"{self.outbound.fingerprint()}|{self.inbound.fingerprint()}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _cabin_rank(code: str) -> int:
    return CABIN_ORDER.index(code) if code in CABIN_ORDER else len(CABIN_ORDER)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Keys from the single-threshold config and where each one moved.
LEGACY_KEYS = {
    ("trip", "cabins"): "a cabins map with one block per cabin",
    ("trip", "sources"): "cabins.<code>.sources",
    ("alerting", "benchmark_miles"): "cabins.<code>.benchmark_miles",
    ("alerting", "floor_miles"): "cabins.<code>.floor_miles",
    ("alerting", "ceiling_miles"): "cabins.<code>.ceiling_miles",
    ("alerting", "new_best_margin_miles"): "cabins.<code>.new_best_margin_miles",
    ("alerting", "max_total_taxes_usd"): "cabins.<code>.max_total_taxes_usd",
    ("pushover", "priority"): "cabins.<code>.pushover_priority",
}

REQUIRED_SECTIONS = ("api", "horizon", "scan", "trip", "cabins", "alerting", "pushover", "storage")


def load_config(path: str) -> dict:
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    return validate_config(raw, source=path)


def validate_config(raw: dict | None, source: str = "config") -> dict:
    """Apply defaults, fail loudly on anything that would misbehave silently."""

    def fail(msg: str) -> None:
        raise SystemExit(f"{source}: {msg}")

    cfg = copy.deepcopy(raw or {})
    missing = [s for s in REQUIRED_SECTIONS if s not in cfg or cfg[s] is None]
    if missing:
        fail(f"missing section(s) {', '.join(missing)}")

    legacy = [f"{a}.{b} moved to {where}" for (a, b), where in LEGACY_KEYS.items()
              if isinstance(cfg.get(a), dict) and b in cfg[a]]
    if legacy:
        fail("this config predates per-cabin thresholds.\n  " + "\n  ".join(legacy))

    trip = cfg["trip"]
    party = trip.get("party_size")
    if not isinstance(party, int) or party < 1:
        fail("trip.party_size must be a positive integer")
    trip.setdefault("allow_mixed_cabin", False)
    trip.setdefault("require_same_source", True)
    trip.setdefault("allow_open_jaw", False)
    for key in ("outbound_origins", "outbound_destinations"):
        if not trip.get(key):
            fail(f"trip.{key} must list at least one airport")
        trip[key] = [str(a).upper() for a in trip[key]]
    if trip["min_trip_nights"] > trip["max_trip_nights"]:
        fail("trip.min_trip_nights is above trip.max_trip_nights")

    cabins_raw = cfg["cabins"]
    if not isinstance(cabins_raw, dict) or not cabins_raw:
        fail("cabins must be a map keyed by cabin code (Y, W, J, F)")
    unknown = [c for c in cabins_raw if c not in CABIN_ORDER]
    if unknown:
        fail(f"unknown cabin code(s) {', '.join(map(str, unknown))}. Use Y, W, J or F.")

    cabins: dict[str, dict] = {}
    for code in CABIN_ORDER:
        if code not in cabins_raw:
            continue
        c = dict(cabins_raw[code] or {})
        c["code"] = code
        c.setdefault("label", CABIN_NAMES[code])
        c.setdefault("enabled", True)
        c.setdefault("min_seats", party)
        c.setdefault("benchmark_miles", None)
        c.setdefault("floor_miles", None)
        c.setdefault("ceiling_miles", None)
        c.setdefault("max_total_taxes_usd", None)
        # An explicit null on these means the default, not a crash later.
        c["new_best_margin_miles"] = c.get("new_best_margin_miles") or 0
        c["pushover_priority"] = c.get("pushover_priority") or 0
        if not c.get("sources"):
            fail(f"cabins.{code}.sources must list at least one program")
        c["sources"] = [str(s).lower() for s in c["sources"]]
        floor, ceiling = c["floor_miles"], c["ceiling_miles"]
        if (floor is None) != (ceiling is None):
            fail(f"cabins.{code}: set both floor_miles and ceiling_miles, "
                 "or leave both null for observe-only")
        if floor is not None and floor > ceiling:
            fail(f"cabins.{code}: floor_miles {floor:,} is above ceiling_miles {ceiling:,}")
        if not isinstance(c["min_seats"], int) or c["min_seats"] < 1:
            fail(f"cabins.{code}.min_seats must be a positive integer")
        if c["min_seats"] > party:
            fail(f"cabins.{code}.min_seats {c['min_seats']} is above party_size {party}")
        if not -2 <= int(c["pushover_priority"]) <= 2:
            fail(f"cabins.{code}.pushover_priority must be between -2 and 2")
        cabins[code] = c
    cfg["cabins"] = cabins
    if not any(c["enabled"] for c in cabins.values()):
        fail("every cabin is disabled, nothing to scan")

    alerting = cfg["alerting"]
    alerting.setdefault("max_data_age_hours", 12)
    alerting.setdefault("require_seat_count", False)
    alerting.setdefault("improvement_threshold_miles", 0)
    alerting.setdefault("cooldown_hours", 6)

    api = cfg["api"]
    api.setdefault("budget_safety_margin", 0)
    api.setdefault("max_pages_per_query", 20)
    api.setdefault("page_size", 500)
    api.setdefault("timeout_seconds", 30)

    cfg["pushover"].setdefault("sound", "pushover")
    return cfg


def enabled_cabins(cfg: dict) -> list[dict]:
    return [c for c in cfg["cabins"].values() if c["enabled"]]


def query_plan(cfg: dict) -> tuple[list[str], list[str]]:
    """Cabin codes and the union of their sources for one API call."""
    cabins = enabled_cabins(cfg)
    codes = [c["code"] for c in cabins]
    sources: list[str] = []
    for c in cabins:
        for s in c["sources"]:
            if s not in sources:
                sources.append(s)
    return codes, sources


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    availability_id TEXT,
    source TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    cabin TEXT NOT NULL,
    miles INTEGER NOT NULL,
    taxes_usd REAL NOT NULL,
    taxes_currency TEXT NOT NULL DEFAULT 'USD',
    seats INTEGER NOT NULL,
    airlines TEXT,
    direct INTEGER,
    last_seen TEXT,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_fp ON observations(fingerprint, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_date ON observations(depart_date, miles);
CREATE INDEX IF NOT EXISTS idx_obs_cabin ON observations(cabin, observed_at);

CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY,
    best_miles INTEGER NOT NULL,
    last_alerted_at TEXT NOT NULL,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS api_usage (
    day TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0,
    quota_remaining INTEGER
);

CREATE TABLE IF NOT EXISTS state (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


def _row_to_leg(r: sqlite3.Row) -> Leg:
    return Leg(
        availability_id=r["availability_id"], source=r["source"],
        origin=r["origin"], destination=r["destination"],
        depart_date=r["depart_date"], cabin=r["cabin"], miles=r["miles"],
        taxes_usd=r["taxes_usd"], seats=r["seats"], airlines=r["airlines"] or "",
        direct=bool(r["direct"]), last_seen=r["last_seen"] or "",
        observed_at=r["observed_at"], taxes_currency=r["taxes_currency"] or "USD",
    )


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(observations)")}
        if "taxes_currency" not in cols:
            self.conn.execute(
                "ALTER TABLE observations ADD COLUMN taxes_currency TEXT NOT NULL DEFAULT 'USD'")

    # -- state ------------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT v FROM state WHERE k = ?", (key,)).fetchone()
        return json.loads(row["v"]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO state(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    # -- observations -----------------------------------------------------
    def record_legs(self, legs: Iterable[Leg]) -> int:
        rows = [
            (
                leg.fingerprint(), leg.availability_id, leg.source, leg.origin,
                leg.destination, leg.depart_date, leg.cabin, leg.miles,
                leg.taxes_usd, leg.taxes_currency, leg.seats, leg.airlines,
                int(leg.direct), leg.last_seen, leg.observed_at,
            )
            for leg in legs
        ]
        self.conn.executemany(
            """INSERT INTO observations
               (fingerprint, availability_id, source, origin, destination,
                depart_date, cabin, miles, taxes_usd, taxes_currency, seats,
                airlines, direct, last_seen, observed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def latest_legs(self, max_age_hours: float) -> list[Leg]:
        """Most recent observation per fingerprint, within the freshness window."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        rows = self.conn.execute(
            """SELECT o.* FROM observations o
               JOIN (SELECT fingerprint, MAX(id) AS mid
                     FROM observations WHERE observed_at >= ?
                     GROUP BY fingerprint) latest
                 ON o.id = latest.mid""",
            (cutoff,),
        ).fetchall()
        return [_row_to_leg(r) for r in rows]

    def history_legs(self) -> list[Leg]:
        """Most recent observation per fingerprint over the whole retained history."""
        rows = self.conn.execute(
            """SELECT o.* FROM observations o
               JOIN (SELECT fingerprint, MAX(id) AS mid FROM observations
                     GROUP BY fingerprint) latest
                 ON o.id = latest.mid"""
        ).fetchall()
        return [_row_to_leg(r) for r in rows]

    def history_span(self) -> dict[str, tuple[str, str, int]]:
        """Per cabin, first and last observation time and tick count."""
        rows = self.conn.execute(
            """SELECT cabin, MIN(observed_at) AS first, MAX(observed_at) AS last,
                      COUNT(*) AS n FROM observations GROUP BY cabin"""
        ).fetchall()
        return {r["cabin"]: (r["first"], r["last"], r["n"]) for r in rows}

    # -- api usage --------------------------------------------------------
    def count_api_call(self, quota_remaining: int | None) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.conn.execute(
            """INSERT INTO api_usage(day, calls, quota_remaining) VALUES (?, 1, ?)
               ON CONFLICT(day) DO UPDATE SET
                 calls = calls + 1,
                 quota_remaining = COALESCE(excluded.quota_remaining, api_usage.quota_remaining)""",
            (day, quota_remaining),
        )
        self.conn.commit()
        return self.calls_today()

    def calls_today(self) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self.conn.execute("SELECT calls FROM api_usage WHERE day = ?", (day,)).fetchone()
        return row["calls"] if row else 0

    def quota_remaining_today(self) -> int | None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT quota_remaining FROM api_usage WHERE day = ?", (day,)).fetchone()
        return row["quota_remaining"] if row else None

    # -- alerts -----------------------------------------------------------
    def previous_alert(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone()

    def save_alert(self, rt: RoundTrip) -> None:
        self.conn.execute(
            """INSERT INTO alerts(key, best_miles, last_alerted_at, payload)
               VALUES (?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 best_miles = MIN(alerts.best_miles, excluded.best_miles),
                 last_alerted_at = excluded.last_alerted_at,
                 payload = excluded.payload""",
            (rt.key(), rt.total_miles, datetime.now(timezone.utc).isoformat(),
             json.dumps({"out": asdict(rt.outbound), "in": asdict(rt.inbound)})),
        )
        self.conn.commit()

    def prune(self, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self.conn.execute("DELETE FROM observations WHERE observed_at < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount


# ---------------------------------------------------------------------------
# Date chunking
# ---------------------------------------------------------------------------


def horizon_chunks(cfg: dict, today: date | None = None) -> list[tuple[str, str]]:
    """Split the bookable horizon into API-sized date ranges."""
    today = today or datetime.now(timezone.utc).date()
    h = cfg["horizon"]
    start = today + timedelta(days=h["min_days_out"])
    end = today + timedelta(days=h["max_days_out"])
    step = timedelta(days=h["chunk_days"])

    chunks: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + step - timedelta(days=1), end)
        chunks.append((cursor.isoformat(), chunk_end.isoformat()))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def month_window(day: str, pad_days: int = 20) -> tuple[str, str]:
    d = datetime.strptime(day, "%Y-%m-%d").date()
    return ((d - timedelta(days=pad_days)).isoformat(),
            (d + timedelta(days=pad_days)).isoformat())


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0
    capped: bool = False          # hit max_pages_per_query with more pages left
    status: int | None = None     # last HTTP status seen


class SeatsAero:
    def __init__(self, cfg: dict, store: Store, api_key: str | None = None):
        self.cfg = cfg["api"]
        self.store = store
        key = api_key if api_key is not None else os.environ.get(self.cfg["key_env"], "").strip()
        if not key:
            raise SystemExit(f"Set {self.cfg['key_env']} to your seats.aero API key.")
        self.session = requests.Session()
        self.session.headers.update({
            "Partner-Authorization": key,
            "Accept": "application/json",
            "User-Agent": "personal-award-monitor/3.0",
        })

    def budget_left(self) -> int:
        cap = self.cfg["daily_call_budget"] - self.cfg["budget_safety_margin"]
        return max(0, cap - self.store.calls_today())

    def base_params(self, origins: list[str], destinations: list[str],
                    cabins: list[str], sources: list[str],
                    start_date: str, end_date: str, take: int | None = None) -> dict:
        return {
            "origin_airport": ",".join(origins),
            "destination_airport": ",".join(destinations),
            "cabin": ",".join(CABIN_QUERY_VALUES[c] for c in cabins),
            "sources": ",".join(sources),
            "start_date": start_date,
            "end_date": end_date,
            "take": take or self.cfg["page_size"],
        }

    def get(self, url: str, params: dict | None = None) -> requests.Response | None:
        """One counted API call. Returns None on transport failure."""
        try:
            resp = self.session.get(url, params=params, timeout=self.cfg["timeout_seconds"])
        except requests.RequestException as exc:
            LOG.error("Request failed: %s", exc)
            return None
        self.store.count_api_call(self._quota_from(resp))
        return resp

    def _quota_from(self, resp: requests.Response) -> int | None:
        for header in QUOTA_HEADERS:
            if header in resp.headers:
                if self.store.get_state("quota_header") != header:
                    LOG.info("Quota header is %s", header)
                    self.store.set_state("quota_header", header)
                try:
                    return int(resp.headers[header])
                except ValueError:
                    return None
        return None

    def cached_search(self, origins: list[str], destinations: list[str],
                      cabins: list[str], sources: list[str],
                      start_date: str, end_date: str) -> SearchResult:
        """Cached search with pagination. seats.aero pages with the cursor from
        the first response plus a skip equal to the rows already received. When
        the response carries a ready-made moreURL, that is followed instead."""
        url = f"{self.cfg['base_url']}/search"
        params0 = self.base_params(origins, destinations, cabins, sources, start_date, end_date)
        result = SearchResult()
        first_cursor: Any = None
        next_url: str | None = None
        skip = 0

        for page in range(self.cfg["max_pages_per_query"]):
            if self.budget_left() <= 0:
                LOG.warning("Daily API budget exhausted")
                break
            if next_url:
                resp = self.get(next_url)
            else:
                params = dict(params0)
                if first_cursor is not None:
                    params["cursor"] = first_cursor
                    params["skip"] = skip
                resp = self.get(url, params)
            if resp is None:
                break
            result.calls += 1
            result.status = resp.status_code

            if resp.status_code == 429:
                LOG.warning("Rate limited, backing off 60s")
                time.sleep(60)
                break
            if resp.status_code != 200:
                LOG.error("HTTP %s: %s", resp.status_code, resp.text[:250])
                break

            body = resp.json()
            batch = body.get("data") or []
            if page == 0 and batch:
                LOG.debug("raw availability sample: %s", json.dumps(batch[0])[:1500])
            result.rows.extend(batch)
            skip += len(batch)

            if not body.get("hasMore") or not batch:
                break
            more = body.get("moreURL")
            if more:
                next_url = urljoin(self.cfg["base_url"], more)
            else:
                if first_cursor is None:
                    first_cursor = body.get("cursor")
                if first_cursor is None:
                    LOG.warning("hasMore is set but no cursor or moreURL, stopping")
                    break
        else:
            result.capped = True
            LOG.warning("Page cap (%d) hit for %s to %s, results truncated. "
                        "Lower horizon.chunk_days or raise api.max_pages_per_query.",
                        self.cfg["max_pages_per_query"], start_date, end_date)
        return result


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_availability(obj: dict[str, Any], cabins: Iterable[str]) -> list[Leg]:
    """One seats.aero Availability object into one Leg per populated cabin.

    Field names follow the published Availability schema. Mileage comes from
    the integer <cabin>MileageCostRaw when present and the string
    <cabin>MileageCost otherwise. <cabin>TotalTaxes is an integer in the minor
    unit of TaxesCurrency, so it is divided by 100. Confirm that one row
    against delta.com on the first live run, see --probe."""
    route = obj.get("Route") or {}
    origin = route.get("OriginAirport") or obj.get("OriginAirport") or ""
    destination = route.get("DestinationAirport") or obj.get("DestinationAirport") or ""
    source = obj.get("Source") or route.get("Source") or "unknown"
    depart_date = (obj.get("Date") or "")[:10]
    last_seen = obj.get("ComputedLastSeen") or obj.get("UpdatedAt") or ""
    currency = obj.get("TaxesCurrency") or "USD"
    now = datetime.now(timezone.utc).isoformat()

    legs: list[Leg] = []
    for cabin in cabins:
        if not obj.get(f"{cabin}Available"):
            continue
        miles = _to_int(obj.get(f"{cabin}MileageCostRaw"))
        if miles <= 0:
            miles = _to_int(obj.get(f"{cabin}MileageCost"))
        if miles <= 0:
            continue
        legs.append(Leg(
            availability_id=str(obj.get("ID", "")),
            source=str(source).lower(), origin=origin, destination=destination,
            depart_date=depart_date, cabin=cabin, miles=miles,
            taxes_usd=round(_to_int(obj.get(f"{cabin}TotalTaxes")) / 100.0, 2),
            seats=_to_int(obj.get(f"{cabin}RemainingSeats")),
            airlines=obj.get(f"{cabin}Airlines") or "",
            direct=bool(obj.get(f"{cabin}Direct")),
            last_seen=last_seen, observed_at=now, taxes_currency=currency,
        ))
    return legs


# ---------------------------------------------------------------------------
# Pairing and ranking
# ---------------------------------------------------------------------------


def build_round_trips(legs: list[Leg], trip: dict) -> list[RoundTrip]:
    """Pair every outbound leg with every legal return leg."""
    out_origins = set(trip["outbound_origins"])
    out_dests = set(trip["outbound_destinations"])

    outbound = [l for l in legs if l.origin in out_origins and l.destination in out_dests]
    inbound = [l for l in legs if l.origin in out_dests and l.destination in out_origins]

    # Bucket returns by origin airport so the inner loop stays small.
    by_origin: dict[str, list[Leg]] = {}
    for leg in inbound:
        by_origin.setdefault(leg.origin, []).append(leg)

    min_n, max_n = trip["min_trip_nights"], trip["max_trip_nights"]
    same_source = trip["require_same_source"]
    open_jaw = trip["allow_open_jaw"]
    mixed_ok = trip.get("allow_mixed_cabin", False)

    pairs: list[RoundTrip] = []
    for out in outbound:
        candidates = inbound if open_jaw else by_origin.get(out.destination, [])
        for back in candidates:
            if same_source and out.source != back.source:
                continue
            if not mixed_ok and out.cabin != back.cabin:
                continue
            rt = RoundTrip(out, back)
            if not (min_n <= rt.nights <= max_n):
                continue
            pairs.append(rt)
    return pairs


def viable(rt: RoundTrip, cabin_cfg: dict, alert_cfg: dict) -> tuple[bool, str]:
    """Seat and tax feasibility for one cabin. Price is judged separately."""
    cap = cabin_cfg.get("max_total_taxes_usd")
    if cap is not None and rt.total_taxes > cap:
        return False, "taxes too high"
    seats = rt.min_seats
    if seats == 0:
        if alert_cfg["require_seat_count"]:
            return False, "seat count not published"
        return True, "seats unpublished, verify"
    if seats < cabin_cfg["min_seats"]:
        return False, f"only {seats} seats"
    return True, f"{seats} seats confirmed"


def alert_reason(rt: RoundTrip, prev_best: int | None, cabin_cfg: dict) -> str | None:
    """Decide whether this combination is worth waking Eric up for.
    Null thresholds mean the cabin is observe-only and never alerts."""
    floor, ceiling = cabin_cfg["floor_miles"], cabin_cfg["ceiling_miles"]
    if floor is None or ceiling is None:
        return None
    total = rt.total_miles
    if total > ceiling:
        return None
    if total <= floor:
        return "under floor"
    if prev_best is None:
        return "first qualifying combination"
    if prev_best - total >= cabin_cfg["new_best_margin_miles"]:
        return f"new best, beats {prev_best:,} by {prev_best - total:,}"
    return None


def should_alert(rt: RoundTrip, store: Store, alert_cfg: dict) -> bool:
    prior = store.previous_alert(rt.key())
    if prior is None:
        return True
    if prior["best_miles"] - rt.total_miles >= alert_cfg["improvement_threshold_miles"]:
        return True
    try:
        last = datetime.fromisoformat(prior["last_alerted_at"])
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - last > timedelta(hours=alert_cfg["cooldown_hours"]):
        return rt.total_miles <= prior["best_miles"]
    return False


def rank_by_cabin(cfg: dict, legs: list[Leg]) -> dict[str, list[tuple[RoundTrip, str]]]:
    """Every enabled cabin, ascending by total miles, viable pairings only.
    A cabin's list only holds pairings from that cabin's own sources."""
    trip, alert_cfg = cfg["trip"], cfg["alerting"]
    pairs = build_round_trips(legs, trip)
    ranked: dict[str, list[tuple[RoundTrip, str]]] = {c["code"]: [] for c in enabled_cabins(cfg)}
    for rt in pairs:
        ccfg = cfg["cabins"].get(rt.cabin)
        if ccfg is None or not ccfg["enabled"]:
            continue
        if rt.outbound.source not in ccfg["sources"] or rt.inbound.source not in ccfg["sources"]:
            continue
        ok, note = viable(rt, ccfg, alert_cfg)
        if ok:
            ranked[rt.cabin].append((rt, note))
    for rows in ranked.values():
        rows.sort(key=lambda x: (x[0].total_miles, x[0].outbound.depart_date))
    return ranked


def choose_focus_dates(ranked: dict[str, list[tuple[RoundTrip, str]]], cap: int) -> list[str]:
    """Top date from each cabin first, then fill by global rank, one per month."""
    focus: list[str] = []
    months: set[str] = set()

    def take(rt: RoundTrip) -> None:
        month = rt.outbound.depart_date[:7]
        if month in months or len(focus) >= cap:
            return
        months.add(month)
        focus.append(rt.outbound.depart_date)

    for code in CABIN_ORDER:
        rows = ranked.get(code)
        if rows:
            take(rows[0][0])
    everything = sorted((rt for rows in ranked.values() for rt, _ in rows),
                        key=lambda rt: rt.total_miles)
    for rt in everything:
        if len(focus) >= cap:
            break
        take(rt)
    return focus


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


def format_alert(rt: RoundTrip, seat_note: str, reason: str,
                 party: int, cabin_cfg: dict) -> tuple[str, str]:
    label = cabin_cfg["label"].upper()
    if rt.mixed:
        label += f" ({rt.cabin_code} mixed)"
    title = f"{rt.total_miles // 1000}k {label} {rt.outbound.source.upper()} {rt.route}"
    lines = [
        f"Out  {rt.outbound.depart_date}  {rt.outbound.origin}-{rt.outbound.destination}  "
        f"{rt.outbound.miles:,} mi{'  nonstop' if rt.outbound.direct else ''}",
        f"Back {rt.inbound.depart_date}  {rt.inbound.origin}-{rt.inbound.destination}  "
        f"{rt.inbound.miles:,} mi{'  nonstop' if rt.inbound.direct else ''}",
        "",
        f"{rt.total_miles:,} miles per person plus ${rt.total_taxes:,.2f}",
        f"x{party} = {rt.total_miles * party:,} miles for the party",
        f"{rt.nights} nights, {seat_note}",
    ]
    bench = cabin_cfg.get("benchmark_miles")
    if bench:
        gap = bench - rt.total_miles
        lines.append(f"vs {cabin_cfg['label'].lower()} benchmark {bench:,}: "
                     f"{'saves' if gap >= 0 else 'costs'} {abs(gap):,} per person")
    lines += [f"Trigger: {reason}", "", FARE_BRAND_REMINDER]
    return title, "\n".join(lines)


def send_pushover(cfg: dict, title: str, message: str, priority: int = 0) -> bool:
    user = os.environ.get(cfg["user_key_env"], "").strip()
    token = os.environ.get(cfg["app_token_env"], "").strip()
    if not user or not token:
        LOG.error("Pushover credentials missing")
        return False
    data = {
        "token": token, "user": user, "title": title, "message": message,
        "priority": priority, "sound": cfg.get("sound", "pushover"),
    }
    if priority == 2:
        # Emergency priority repeats until acknowledged and needs these two.
        data.update({"retry": 60, "expire": 3600})
    try:
        resp = requests.post(cfg["api_url"], timeout=15, data=data)
        if resp.status_code == 200:
            return True
        LOG.error("Pushover %s: %s", resp.status_code, resp.text[:200])
    except requests.RequestException as exc:
        LOG.error("Pushover failed: %s", exc)
    return False


# ---------------------------------------------------------------------------
# Scan cycles
# ---------------------------------------------------------------------------


def fetch_window(client: SeatsAero, store: Store, cfg: dict,
                 start: str, end: str) -> tuple[int, int, bool]:
    """Both directions for one date window. Returns legs stored, API calls
    used and whether either direction hit the page cap."""
    trip = cfg["trip"]
    codes, sources = query_plan(cfg)
    legs_total, calls, capped = 0, 0, False
    for origins, dests in (
        (trip["outbound_origins"], trip["outbound_destinations"]),
        (trip["outbound_destinations"], trip["outbound_origins"]),
    ):
        res = client.cached_search(origins, dests, codes, sources, start, end)
        legs = [leg for obj in res.rows for leg in parse_availability(obj, codes)]
        store.record_legs(legs)
        legs_total += len(legs)
        calls += res.calls
        capped = capped or res.capped
    return legs_total, calls, capped


def evaluate(cfg: dict, store: Store, dry_run: bool) -> dict[str, list[RoundTrip]]:
    """Rank everything currently known, per cabin, and alert on what qualifies."""
    trip, alert_cfg = cfg["trip"], cfg["alerting"]
    party = trip["party_size"]

    legs = store.latest_legs(alert_cfg["max_data_age_hours"])
    ranked = rank_by_cabin(cfg, legs)
    LOG.info("%d fresh legs, viable pairings per cabin: %s", len(legs),
             ", ".join(f"{code} {len(rows)}" for code, rows in ranked.items()))

    for code, rows in ranked.items():
        if not rows:
            continue
        ccfg = cfg["cabins"][code]
        state_key = f"best_total_miles:{code}"
        prev_best = store.get_state(state_key)
        best_rt = rows[0][0]

        # rows is ascending, so walk it with a running best. Without this every
        # combination compares against the same stale figure and a cold start
        # alerts on the whole leaderboard instead of just the winner. The best
        # is per cabin so a cheap economy pairing never suppresses business.
        running_best = prev_best
        for rt, note in rows[:ALERT_SCAN_DEPTH]:
            reason = alert_reason(rt, running_best, ccfg)
            if not reason or not should_alert(rt, store, alert_cfg):
                continue
            title, message = format_alert(rt, note, reason, party, ccfg)
            if dry_run:
                print(f"\n--- WOULD ALERT (priority {ccfg['pushover_priority']}) ---\n{title}\n{message}")
                store.save_alert(rt)
            elif send_pushover(cfg["pushover"], title, message, ccfg["pushover_priority"]):
                store.save_alert(rt)
                LOG.info("Alerted: %s (%s)", title, reason)
            running_best = min(running_best or rt.total_miles, rt.total_miles)

        if prev_best is None or best_rt.total_miles < prev_best:
            store.set_state(state_key, best_rt.total_miles)

    focus = choose_focus_dates(ranked, cfg["scan"]["max_focus_windows"])
    if focus:
        store.set_state("focus_dates", focus)

    return {code: [rt for rt, _ in rows] for code, rows in ranked.items()}


def run_sweep(cfg: dict, store: Store, client: SeatsAero, dry_run: bool) -> None:
    chunks = horizon_chunks(cfg)
    codes, sources = query_plan(cfg)
    LOG.info("Sweep starting, %d windows, cabins %s, %d sources, %d calls left today",
             len(chunks), "".join(codes), len(sources), client.budget_left())
    windows_done, calls_total, capped_windows = 0, 0, 0
    for start, end in chunks:
        if client.budget_left() < 4:
            LOG.warning("Stopping sweep early to protect focus polling")
            break
        legs, calls, capped = fetch_window(client, store, cfg, start, end)
        windows_done += 1
        calls_total += calls
        capped_windows += int(capped)
        LOG.info("  %s to %s: %d legs, %d calls%s", start, end, legs, calls,
                 ", PAGE CAP HIT" if capped else "")
    if windows_done:
        store.set_state("sweep_stats", {
            "at": datetime.now(timezone.utc).isoformat(),
            "windows": windows_done,
            "calls": calls_total,
            "calls_per_window": round(calls_total / windows_done, 2),
            "capped_windows": capped_windows,
        })
    store.set_state("last_sweep", datetime.now(timezone.utc).isoformat())
    evaluate(cfg, store, dry_run)


def run_focus(cfg: dict, store: Store, client: SeatsAero, dry_run: bool) -> None:
    focus_dates = store.get_state("focus_dates") or []
    if not focus_dates:
        LOG.info("No focus windows yet, running a sweep instead")
        run_sweep(cfg, store, client, dry_run)
        return
    calls_total = 0
    for day in focus_dates:
        start, end = month_window(day)
        _, calls, _ = fetch_window(client, store, cfg, start, end)
        calls_total += calls
    LOG.info("Focus poll of %d windows done in %d calls, %d calls left",
             len(focus_dates), calls_total, client.budget_left())
    evaluate(cfg, store, dry_run)


def run_probe(cfg: dict, client: SeatsAero) -> None:
    """One live call. Prints everything needed to reconcile the parser
    against the real API, then stops. Costs exactly one call."""
    trip = cfg["trip"]
    codes, sources = query_plan(cfg)
    start, end = horizon_chunks(cfg)[0]
    params = client.base_params(trip["outbound_origins"], trip["outbound_destinations"],
                                codes, sources, start, end, take=25)
    print(f"\nPROBE  one cached search call")
    for k, v in params.items():
        print(f"  {k:20} {v}")

    t0 = time.monotonic()
    resp = client.get(f"{client.cfg['base_url']}/search", params)
    if resp is None:
        print("\nTransport failure, see log above.")
        return
    print(f"\nHTTP {resp.status_code} in {time.monotonic() - t0:.2f}s")
    print("Response headers")
    matched = next((h for h in QUOTA_HEADERS if h in resp.headers), None)
    for k, v in resp.headers.items():
        tag = "   <- daily quota" if matched and k.lower() == matched else ""
        print(f"  {k}: {v}{tag}")
    if not matched:
        print("  (no header in QUOTA_HEADERS matched, pick the right one from the list above)")
    if resp.status_code != 200:
        print(f"\nBody: {resp.text[:800]}")
        print("\nA 400 here usually means a query value is wrong. "
              "The cabin filter sends word values, see CABIN_QUERY_VALUES.")
        return

    body = resp.json()
    print(f"\nBody keys  {', '.join(body.keys())}")
    for k in ("count", "hasMore", "cursor", "moreURL"):
        print(f"  {k:10} {body.get(k, 'MISSING')!r}")
    rows = body.get("data") or []
    if not rows:
        print("\nNo rows in this window. Try again with a different horizon.min_days_out.")
        return

    obj = rows[0]
    print(f"\nFirst availability object ({len(rows)} in page)")
    print(json.dumps(obj, indent=2)[:4000])

    print("\nField reconciliation")
    route = obj.get("Route") or {}
    checks = [
        ("ID", obj.get("ID")),
        ("Route.OriginAirport", route.get("OriginAirport")),
        ("Route.DestinationAirport", route.get("DestinationAirport")),
        ("Source", obj.get("Source")),
        ("Date", obj.get("Date")),
        ("ComputedLastSeen", obj.get("ComputedLastSeen")),
        ("UpdatedAt", obj.get("UpdatedAt")),
        ("TaxesCurrency", obj.get("TaxesCurrency")),
    ]
    for name, val in checks:
        print(f"  {name:26} {'ok' if val not in (None, '') else 'MISSING':8} {val!r}")
    for code in CABIN_ORDER:
        fields = ["Available", "MileageCost", "MileageCostRaw", "RemainingSeats",
                  "TotalTaxes", "Airlines", "Direct"]
        bits = []
        for f in fields:
            key = f"{code}{f}"
            bits.append(f"{f}={obj[key]!r}" if key in obj else f"{f}=MISSING")
        print(f"  {code}  " + "  ".join(bits))

    populated = {c: sum(1 for r in rows if r.get(f"{c}Available")) for c in CABIN_ORDER}
    print("\nRows with availability per cabin in this page  "
          + "  ".join(f"{c} {n}" for c, n in populated.items()))

    legs = parse_availability(obj, codes)
    print(f"\nParser output for the first object, {len(legs)} leg(s)")
    for leg in legs:
        print(f"  {leg.cabin} {leg.origin}-{leg.destination} {leg.depart_date} {leg.source:12} "
              f"{leg.miles:>8,} mi  taxes {leg.taxes_usd:,.2f} {leg.taxes_currency}  "
              f"seats {leg.seats}  direct {leg.direct}")
    print("\nCompare one row against delta.com. If the taxes are off by 100x, "
          "TotalTaxes is not in the minor unit.")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

BEST_HEADER = (f"{'out':11} {'back':11} {'nts':>4} {'route':9} {'prog':14} "
               f"{'miles':>8} {'vs bench':>9} {'seats':>6} {'party':>10}")


def cabin_headline(c: dict) -> tuple[str, str]:
    """Two lines. The first says what the cabin is and what it is measured
    against, the second says exactly when it will alert."""
    bench = c["benchmark_miles"]
    first = f"{c['label'].upper()}  " + (f"benchmark {bench:,} per person" if bench
                                         else "no benchmark")
    gate = f"seats for {c['min_seats']}"
    if c["max_total_taxes_usd"] is not None:
        gate += f", taxes under ${c['max_total_taxes_usd']:,.0f}"
    if c["floor_miles"] is None:
        first += ", observe-only"
        second = f"{gate}  |  no alerts until thresholds are set, run --calibrate"
    else:
        second = (f"{gate}  |  alerts at or under {c['floor_miles']:,}, "
                  f"on a new best by {c['new_best_margin_miles']:,}, "
                  f"never above {c['ceiling_miles']:,}")
    return first, second


def print_best_section(c: dict, rows: list[tuple[RoundTrip, str]], legs_seen: int,
                       party: int, trip: dict, limit: int, show_cabins: bool) -> None:
    first, second = cabin_headline(c)
    print(first)
    print(second)
    if not rows:
        if legs_seen == 0:
            print("  no availability recorded")
        else:
            print(f"  {legs_seen} legs seen, none pair into a viable round trip "
                  f"(seats for {c['min_seats']}, {trip['min_trip_nights']} to "
                  f"{trip['max_trip_nights']} nights, same program both ways)")
        print()
        return
    bench = c["benchmark_miles"]
    print(BEST_HEADER + ("  cabins" if show_cabins else ""))
    print("-" * (len(BEST_HEADER) + (8 if show_cabins else 0)))
    for rt, _ in rows[:limit]:
        seats = str(rt.min_seats or "?")
        vs = f"{bench - rt.total_miles:>+9,}" if bench else f"{'-':>9}"
        line = (f"{rt.outbound.depart_date:11} {rt.inbound.depart_date:11} "
                f"{rt.nights:>4} {rt.route:9} {rt.outbound.source:14} "
                f"{rt.total_miles:>8,} {vs} {seats:>6} {rt.total_miles * party:>10,}")
        if show_cabins:
            line += f"  {rt.cabin_code}"
        print(line)
    shown = min(limit, len(rows))
    tail = f"{shown} of {len(rows):,} viable combinations"
    if bench:
        under = sum(1 for rt, _ in rows if rt.total_miles < bench)
        tail += f", {under:,} beat the benchmark"
    print(f"  {tail}")
    print()


def show_best(cfg: dict, store: Store, limit: int = 15, cabin: str | None = None) -> None:
    trip, alert_cfg = cfg["trip"], cfg["alerting"]
    party = trip["party_size"]
    age_hours = alert_cfg["max_data_age_hours"] * 24
    legs = store.latest_legs(age_hours)
    ranked = rank_by_cabin(cfg, legs)
    seen = {code: sum(1 for l in legs if l.cabin == code) for code in CABIN_ORDER}

    cabins = enabled_cabins(cfg)
    if cabin:
        cabins = [c for c in cabins if c["code"] == cabin]
        if not cabins:
            raise SystemExit(f"--cabin {cabin} is not an enabled cabin. Enabled: "
                             + ", ".join(c["code"] for c in enabled_cabins(cfg)))

    routes = f"{'/'.join(trip['outbound_origins'])} to {'/'.join(trip['outbound_destinations'])}"
    print(f"\nCheapest round trips for {party}, {routes}, per person, "
          f"data from the last {age_hours // 24} days")
    if not legs:
        print("Nothing recorded yet. Run --sweep first.")
    print()
    for c in cabins:
        print_best_section(c, ranked[c["code"]], seen[c["code"]], party, trip, limit,
                           show_cabins=trip.get("allow_mixed_cabin", False))


def show_stats(cfg: dict, store: Store) -> None:
    rows = store.conn.execute(
        """SELECT cabin, source, origin, destination, COUNT(*) AS n,
                  MIN(miles) AS lo, CAST(AVG(miles) AS INT) AS avg, MAX(miles) AS hi,
                  MIN(observed_at) AS first, MAX(observed_at) AS last
           FROM observations GROUP BY cabin, source, origin, destination
           ORDER BY cabin, lo"""
    ).fetchall()
    if not rows:
        print("No observations yet.")
        return
    print(f"\nObservation history, one-way legs per person\n")
    print(f"{'cab':4} {'source':14} {'route':9} {'ticks':>7} {'min':>9} {'avg':>9} "
          f"{'max':>9}  {'first seen':10}  last seen")
    print("-" * 92)
    current = None
    for r in sorted(rows, key=lambda r: (_cabin_rank(r["cabin"]), r["lo"])):
        if r["cabin"] != current:
            current = r["cabin"]
            print(f"{CABIN_NAMES.get(current, current)}")
        print(f"{r['cabin']:4} {r['source']:14} {r['origin']}-{r['destination']:5} "
              f"{r['n']:>7} {r['lo']:>9,} {r['avg']:>9,} {r['hi']:>9,}  "
              f"{r['first'][:10]}  {r['last'][:10]}")
    print()


def budget_plan(cfg: dict, store: Store) -> dict:
    """Theoretical plan plus the measured pagination from the last sweep."""
    chunks = horizon_chunks(cfg)
    scan = cfg["scan"]
    sweeps = 24 / scan["sweep_interval_hours"]
    focus_polls = (24 * 60) / scan["focus_interval_minutes"] - sweeps
    cap = cfg["api"]["daily_call_budget"] - cfg["api"]["budget_safety_margin"]
    stats = store.get_state("sweep_stats") or {}
    measured = stats.get("calls_per_window")
    per_window = measured if measured else 2.0
    sweep_calls = len(chunks) * per_window * sweeps
    focus_calls = scan["max_focus_windows"] * per_window * focus_polls
    return {
        "windows": len(chunks), "sweeps": sweeps, "focus_polls": focus_polls,
        "cap": cap, "measured": measured, "per_window": per_window,
        "sweep_calls": sweep_calls, "focus_calls": focus_calls,
        "planned": sweep_calls + focus_calls, "stats": stats,
    }


def show_budget(cfg: dict, store: Store) -> None:
    p = budget_plan(cfg, store)
    h, scan, api = cfg["horizon"], cfg["scan"], cfg["api"]
    codes, sources = query_plan(cfg)
    print(f"\nHorizon     {h['min_days_out']} to {h['max_days_out']} days out, "
          f"{p['windows']} windows of {h['chunk_days']} days")
    print(f"Per call    cabins {''.join(codes)}, {len(sources)} sources, "
          f"{api['page_size']} rows per page, up to {api['max_pages_per_query']} pages")
    if p["measured"]:
        stats = p["stats"]
        capped = stats.get("capped_windows", 0)
        print(f"Pagination  measured {p['per_window']:.2f} calls per window over "
              f"{stats['windows']} windows on {stats['at'][:16].replace('T', ' ')} UTC"
              + (f", {capped} hit the page cap" if capped else ""))
    else:
        print("Pagination  not measured yet, assuming one page per direction "
              "(2 calls per window). Run --sweep to measure.")
    print(f"Sweeps      {p['sweeps']:.0f}/day x {p['windows']} windows x "
          f"{p['per_window']:.2f} calls = {p['sweep_calls']:.0f} calls")
    print(f"Focus       {p['focus_polls']:.0f}/day x {scan['max_focus_windows']} windows x "
          f"{p['per_window']:.2f} calls = {p['focus_calls']:.0f} calls")
    print(f"Planned     {p['planned']:.0f} of {p['cap']} usable "
          f"({api['daily_call_budget']} cap minus {api['budget_safety_margin']} margin)")
    used = store.calls_today()
    quota = store.quota_remaining_today()
    header = store.get_state("quota_header")
    line = f"Used today  {used} calls"
    if quota is not None and header:
        line += f", {header} reports {quota} left"
    print(line)
    if p["planned"] > p["cap"]:
        print("\nOVER BUDGET. Raise horizon.chunk_days, scan.sweep_interval_hours "
              "or scan.focus_interval_minutes, then run --budget again.")
    elif p["measured"] and p["per_window"] > 2.5:
        print("\nPagination is eating the margin. Lower horizon.chunk_days so each "
              "query covers fewer dates, or raise scan.focus_interval_minutes.")
    print()


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


@dataclass
class Calibration:
    cabin: str
    combos: int = 0
    first_seen: str | None = None
    last_seen: str | None = None
    span_days: float = 0.0
    minimum: int | None = None
    p10: float | None = None
    median: float | None = None
    p90: float | None = None
    floor: int | None = None
    ceiling: int | None = None
    benchmark_pct: float | None = None
    refusal: str | None = None


def percentile(values: list[int], p: float) -> float:
    """Linear interpolation between closest ranks, same as numpy's default."""
    if not values:
        raise ValueError("percentile of empty list")
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(s[int(k)])
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def round_to(value: float, step: int) -> int:
    return int(round(value / step) * step)


def calibrate(cfg: dict, store: Store) -> dict[str, Calibration]:
    """Percentiles of viable round trip totals per cabin over the whole
    retained history, latest price per leg. Refuses on thin history."""
    legs = store.history_legs()
    ranked = rank_by_cabin(cfg, legs)
    span = store.history_span()
    out: dict[str, Calibration] = {}
    for c in enabled_cabins(cfg):
        code = c["code"]
        cal = Calibration(cabin=code)
        totals = [rt.total_miles for rt, _ in ranked[code]]
        cal.combos = len(totals)
        if code in span:
            first, last, _ = span[code]
            cal.first_seen, cal.last_seen = first[:10], last[:10]
            cal.span_days = (datetime.fromisoformat(last) - datetime.fromisoformat(first)
                             ).total_seconds() / 86400
        problems = []
        if cal.combos < CALIBRATE_MIN_COMBOS:
            problems.append(f"{CALIBRATE_MIN_COMBOS} viable combinations (have {cal.combos})")
        if cal.span_days < CALIBRATE_MIN_DAYS:
            problems.append(f"{CALIBRATE_MIN_DAYS} days of history (have {cal.span_days:.0f})")
        if totals:
            cal.minimum = min(totals)
            cal.p10 = percentile(totals, 10)
            cal.median = percentile(totals, 50)
            cal.p90 = percentile(totals, 90)
            if c["benchmark_miles"]:
                below = sum(1 for t in totals if t < c["benchmark_miles"])
                cal.benchmark_pct = 100.0 * below / len(totals)
        if problems:
            cal.refusal = "need " + " and ".join(problems)
        else:
            cal.floor = round_to(cal.p10, 5000)
            cal.ceiling = round_to(cal.median, 5000)
            if cal.floor >= cal.ceiling:
                cal.refusal = (f"prices are too tightly clustered, p10 {cal.p10:,.0f} and "
                               f"median {cal.median:,.0f} round to the same figure")
                cal.floor = cal.ceiling = None
        out[code] = cal
    return out


def show_calibrate(cfg: dict, store: Store) -> None:
    results = calibrate(cfg, store)
    pad = max(len(c["label"]) for c in enabled_cabins(cfg)) + 2
    print("\nCALIBRATE  thresholds proposed from stored round trip totals")
    print(f"Viable combinations only (seats for the party, taxes under the cabin cap), "
          f"latest price per leg, whole retained history.\n")
    proposals: list[tuple[dict, Calibration]] = []
    for c in enabled_cabins(cfg):
        cal = results[c["code"]]
        label = c["label"].upper().ljust(pad)
        indent = " " * pad
        when = (f"{cal.first_seen} to {cal.last_seen}" if cal.first_seen else "no observations")
        print(f"{label}{cal.combos:,} combinations, {cal.span_days:.0f} days of history ({when})")
        if cal.minimum is not None:
            print(f"{indent}min {cal.minimum:,}   p10 {cal.p10:,.0f}   "
                  f"median {cal.median:,.0f}   p90 {cal.p90:,.0f}")
        if cal.benchmark_pct is not None:
            print(f"{indent}{cal.benchmark_pct:.0f}% of combinations beat the "
                  f"{c['benchmark_miles']:,} benchmark")
        if cal.refusal:
            print(f"{indent}refused, {cal.refusal}")
        else:
            print(f"{indent}proposed floor {cal.floor:,} (p10 rounded to 5,000), "
                  f"ceiling {cal.ceiling:,} (median rounded to 5,000)")
            proposals.append((c, cal))
        print()

    if not proposals:
        print("Nothing to propose yet. Keep the loop running and try again later.")
        return
    print("Paste into config.yaml under cabins, then run --self-test and --budget.\n")
    print("cabins:")
    for c, cal in proposals:
        print(f"  {c['code']}:")
        print(f"    floor_miles: {cal.floor:<12}# p10 {cal.p10:,.0f}")
        print(f"    ceiling_miles: {cal.ceiling:<10}# median {cal.median:,.0f}")
    print()


# ---------------------------------------------------------------------------
# Self test
# ---------------------------------------------------------------------------

SELF_TEST_CONFIG = """
api:
  base_url: "https://seats.aero/partnerapi"
  key_env: "SEATS_AERO_API_KEY"
  daily_call_budget: 1000
  budget_safety_margin: 100
  timeout_seconds: 5
  max_pages_per_query: 3
  page_size: 500
horizon: {min_days_out: 45, max_days_out: 331, chunk_days: 31}
scan: {sweep_interval_hours: 6, focus_interval_minutes: 15, max_focus_windows: 3}
trip:
  party_size: 4
  outbound_origins: [JFK, EWR]
  outbound_destinations: [SYD, MEL]
  min_trip_nights: 10
  max_trip_nights: 35
  require_same_source: true
  allow_open_jaw: false
  allow_mixed_cabin: false
cabins:
  Y:
    sources: [delta, qantas]
    benchmark_miles: 66200
    floor_miles: 60000
    ceiling_miles: 72000
    new_best_margin_miles: 3000
    max_total_taxes_usd: 400
    pushover_priority: 1
  W:
    sources: [delta, qantas]
    new_best_margin_miles: 8000
    max_total_taxes_usd: 500
  J:
    sources: [delta, qantas]
    floor_miles: 250000
    ceiling_miles: 320000
    new_best_margin_miles: 20000
    max_total_taxes_usd: 800
  F:
    sources: [qantas]
    new_best_margin_miles: 40000
    max_total_taxes_usd: 1200
    min_seats: 2
alerting:
  max_data_age_hours: 12
  require_seat_count: false
  improvement_threshold_miles: 3000
  cooldown_hours: 6
pushover:
  user_key_env: PUSHOVER_USER_KEY
  app_token_env: PUSHOVER_APP_TOKEN
  api_url: "https://api.pushover.net/1/messages.json"
  sound: persistent
storage: {db_path: ":memory:", retain_observation_days: 400}
"""


class _FakeResponse:
    def __init__(self, body: dict, status: int = 200, headers: dict | None = None):
        self._body, self.status_code = body, status
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body


class _FakeSession:
    """Stands in for requests.Session. Records every call it receives."""
    def __init__(self, pages: list[_FakeResponse]):
        self.pages, self.calls = list(pages), []

    def get(self, url: str, params: dict | None = None, timeout: float = 0) -> _FakeResponse:
        self.calls.append((url, dict(params or {})))
        return self.pages.pop(0)


def _expect_fail(fn, needle: str) -> None:
    try:
        fn()
    except SystemExit as exc:
        assert needle in str(exc), f"expected '{needle}' in: {exc}"
        return
    raise AssertionError(f"expected failure mentioning '{needle}'")


def self_test() -> None:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    cfg = validate_config(yaml.safe_load(SELF_TEST_CONFIG), source="self-test config")
    store = Store(":memory:")

    # -- config -------------------------------------------------------------
    assert list(cfg["cabins"]) == ["Y", "W", "J", "F"]
    assert cfg["cabins"]["W"]["min_seats"] == 4 and cfg["cabins"]["F"]["min_seats"] == 2
    assert cfg["cabins"]["W"]["floor_miles"] is None
    codes, sources = query_plan(cfg)
    assert codes == ["Y", "W", "J", "F"] and sources == ["delta", "qantas"], (codes, sources)

    def broken(mutate):
        raw = yaml.safe_load(SELF_TEST_CONFIG)
        mutate(raw)
        return lambda: validate_config(raw, source="cfg")

    _expect_fail(broken(lambda r: r["cabins"].update({"P": {"sources": ["delta"]}})), "unknown cabin")
    _expect_fail(broken(lambda r: r["cabins"]["Y"].update({"floor_miles": 80000})), "above ceiling")
    _expect_fail(broken(lambda r: r["cabins"]["J"].update({"ceiling_miles": None})), "both")
    _expect_fail(broken(lambda r: r["trip"].update({"cabins": ["Y"]})), "predates")
    _expect_fail(broken(lambda r: r["cabins"]["F"].update({"min_seats": 9})), "above party_size")
    print("config ok: defaults applied, four bad configs rejected loudly")

    # -- horizon ------------------------------------------------------------
    chunks = horizon_chunks(cfg, today=date(2026, 8, 15))
    assert chunks[0][0] == "2026-09-29", chunks[0]
    assert chunks[-1][1] == "2027-07-12", chunks[-1]
    print(f"horizon ok: {len(chunks)} windows, {chunks[0][0]} to {chunks[-1][1]}")

    # -- parsing ------------------------------------------------------------
    def av(oid, source, o, d, day, **cabins):
        obj = {
            "ID": oid, "Source": source, "Date": day, "TaxesCurrency": "USD",
            "Route": {"OriginAirport": o, "DestinationAirport": d, "Source": source},
            "ComputedLastSeen": now,
        }
        for code in CABIN_ORDER:
            obj[f"{code}Available"] = False
            obj[f"{code}MileageCost"] = "0"
            obj[f"{code}MileageCostRaw"] = 0
            obj[f"{code}RemainingSeats"] = 0
            obj[f"{code}TotalTaxes"] = 0
            obj[f"{code}Airlines"] = ""
        for code, (miles, seats, taxes) in cabins.items():
            obj[f"{code}Available"] = True
            obj[f"{code}MileageCost"] = str(miles)
            obj[f"{code}MileageCostRaw"] = miles
            obj[f"{code}RemainingSeats"] = seats
            obj[f"{code}TotalTaxes"] = taxes
            obj[f"{code}Airlines"] = "DL" if source == "delta" else "QF"
        return obj

    sample = av("p1", "delta", "JFK", "SYD", "2027-02-10",
                Y=(33100, 6, 6625), W=(60000, 3, 8000), J=(140000, 4, 20000))
    sample["JDirect"] = True
    legs = parse_availability(sample, CABIN_ORDER)
    assert [l.cabin for l in legs] == ["Y", "W", "J"], legs
    assert legs[0].miles == 33100 and legs[0].taxes_usd == 66.25 and legs[0].taxes_currency == "USD"
    assert legs[2].direct is True and legs[1].direct is False
    string_only = dict(sample)
    del string_only["YMileageCostRaw"]
    assert parse_availability(string_only, ["Y"])[0].miles == 33100, "string fallback"
    assert parse_availability(dict(sample, YAvailable=False), ["Y"]) == []
    print("parse ok: four cabin fields, Raw preferred over string, taxes in minor units")

    # -- fixtures -----------------------------------------------------------
    raw = [
        av("o1", "delta", "JFK", "SYD", "2027-02-10", Y=(33100, 6, 6600)),     # April benchmark
        av("o2", "delta", "JFK", "SYD", "2027-03-14", Y=(27000, 5, 6600)),     # cheaper outbound
        av("i1", "delta", "SYD", "JFK", "2027-03-04", Y=(33100, 4, 6600)),     # 22 nights from o1
        av("i2", "delta", "SYD", "JFK", "2027-04-05", Y=(27000, 4, 6600)),     # 22 nights from o2
        av("i3", "delta", "SYD", "JFK", "2027-03-16", Y=(20000, 2, 6600)),     # cheap, only 2 seats
        av("j1", "delta", "JFK", "SYD", "2027-05-02", J=(140000, 4, 30000)),   # business out
        av("j2", "delta", "SYD", "JFK", "2027-05-24", J=(145000, 4, 30000)),   # business back
        av("j3", "delta", "SYD", "JFK", "2027-05-30", Y=(30000, 4, 6600)),     # economy back in May
        av("w1", "delta", "JFK", "SYD", "2027-06-01", W=(70000, 4, 9000)),     # premium out
        av("w2", "delta", "SYD", "JFK", "2027-06-20", W=(70000, 4, 9000)),     # premium back
        av("f1", "qantas", "JFK", "SYD", "2027-07-01", F=(200000, 2, 50000)),  # first, 2 seats
        av("f2", "qantas", "SYD", "JFK", "2027-07-20", F=(200000, 2, 50000)),
        av("f3", "delta", "JFK", "SYD", "2027-07-02", F=(150000, 4, 50000)),   # delta is not an F source
        av("f4", "delta", "SYD", "JFK", "2027-07-21", F=(150000, 4, 50000)),
    ]
    legs = [leg for obj in raw for leg in parse_availability(obj, CABIN_ORDER)]
    assert len(legs) == len(raw), len(legs)
    store.record_legs(legs)

    # -- pairing ------------------------------------------------------------
    fresh = store.latest_legs(48)
    pairs = build_round_trips(fresh, cfg["trip"])
    assert all(not p.mixed for p in pairs), "mixed cabin pairing leaked through"
    totals = sorted({p.total_miles for p in pairs})
    assert 54000 in totals and 285000 in totals and 140000 in totals, totals
    mixed_trip = dict(cfg["trip"], allow_mixed_cabin=True)
    mixed_pairs = build_round_trips(fresh, mixed_trip)
    mixed = [p for p in mixed_pairs if p.mixed]
    assert mixed and all(p.cabin == "Y" for p in mixed if "Y" in p.cabin_code), "mixed ranks under lower cabin"
    assert len(mixed_pairs) > len(pairs)
    print(f"pairing ok: {len(pairs)} same-cabin combos, {len(mixed)} mixed rejected by default")

    # -- viability per cabin ------------------------------------------------
    ranked = rank_by_cabin(cfg, fresh)
    assert [rt.total_miles for rt, _ in ranked["Y"]] == [54000, 66200], ranked["Y"]
    assert ranked["W"][0][0].total_miles == 140000
    assert ranked["J"][0][0].total_miles == 285000
    assert [rt.outbound.source for rt, _ in ranked["F"]] == ["qantas"], "delta F must be filtered by sources"
    assert ranked["F"][0][0].min_seats == 2, "F min_seats override"
    print("ranking ok: Y needs 4 seats, F accepts 2, F ignores non-F sources")

    # -- alert gating -------------------------------------------------------
    y_best, j_best, w_best = ranked["Y"][0][0], ranked["J"][0][0], ranked["W"][0][0]
    assert alert_reason(y_best, None, cfg["cabins"]["Y"]) == "under floor"
    assert alert_reason(ranked["Y"][1][0], 54000, cfg["cabins"]["Y"]) is None, "66,200 above 60,000 floor with 54,000 best"
    expensive = RoundTrip(legs[0], legs[2])
    over = dict(cfg["cabins"]["Y"], ceiling_miles=65000)
    assert alert_reason(expensive, None, over) is None, "above ceiling"
    assert alert_reason(w_best, None, cfg["cabins"]["W"]) is None, "null thresholds never alert"
    assert alert_reason(j_best, None, cfg["cabins"]["J"]) == "first qualifying combination"
    assert y_best.key() != RoundTrip(legs[5], legs[6]).key()
    same_dates_y = Leg(**dict(asdict(legs[5]), cabin="Y"))
    same_dates_y_back = Leg(**dict(asdict(legs[6]), cabin="Y"))
    assert RoundTrip(legs[5], legs[6]).key() != RoundTrip(same_dates_y, same_dates_y_back).key(), \
        "dedupe key must include cabin"
    print("alert gating ok: floor fires, ceiling suppresses, null thresholds silent, key carries cabin")

    # -- evaluate, per cabin running best -----------------------------------
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = evaluate(cfg, store, dry_run=True)
    out = buf.getvalue()
    assert result["Y"][0].total_miles == 54000
    assert "54k ECONOMY DELTA JFK-SYD" in out, out
    assert "285k BUSINESS DELTA JFK-SYD" in out, "cheap economy suppressed the business alert"
    assert "PREMIUM ECONOMY" not in out and "FIRST" not in out, "observe-only cabins alerted"
    assert out.count("WOULD ALERT") == 2, out
    assert "(priority 1)" in out and "(priority 0)" in out
    assert "vs economy benchmark 66,200: saves 12,200" in out
    assert FARE_BRAND_REMINDER in out
    assert store.get_state("best_total_miles:Y") == 54000
    assert store.get_state("best_total_miles:J") == 285000
    assert store.get_state("best_total_miles:W") == 140000, "observe-only cabins still track a best"
    focus = store.get_state("focus_dates")
    assert focus[:3] == ["2027-03-14", "2027-06-01", "2027-05-02"], focus
    print(f"evaluate ok: Y and J alert independently, W and F observe, focus {focus}")

    # second pass must be quiet, everything is deduped
    buf = io.StringIO()
    with redirect_stdout(buf):
        evaluate(cfg, store, dry_run=True)
    assert "WOULD ALERT" not in buf.getvalue(), "alerts repeated on the second pass"
    print("dedupe ok: second pass is silent")

    # -- leaderboard rendering ----------------------------------------------
    buf = io.StringIO()
    with redirect_stdout(buf):
        show_best(cfg, store)
    out = buf.getvalue()
    for label in ("ECONOMY  benchmark 66,200", "PREMIUM ECONOMY  no benchmark, observe-only",
                  "BUSINESS  no benchmark", "FIRST  no benchmark, observe-only"):
        assert label in out, f"missing section {label!r}\n{out}"
    assert "+12,200" in out and "216,000" in out, out       # vs bench and party total
    assert "1,140,000" in out, "party total for business"
    assert out.index("ECONOMY") < out.index("PREMIUM") < out.index("BUSINESS") < out.index("FIRST")
    buf = io.StringIO()
    with redirect_stdout(buf):
        show_best(cfg, store, cabin="F")
    only_f = buf.getvalue()
    assert "FIRST" in only_f and "ECONOMY" not in only_f and "BUSINESS" not in only_f
    empty = Store(":memory:")
    buf = io.StringIO()
    with redirect_stdout(buf):
        show_best(cfg, empty)
    assert buf.getvalue().count("no availability recorded") == 4, buf.getvalue()
    print("leaderboard ok: four sections in order, --cabin F alone, empty sections say so")

    # -- pagination protocol ------------------------------------------------
    client = SeatsAero(cfg, store, api_key="test-key")
    hdr = {"x-ratelimit-remaining": "990"}
    client.session = _FakeSession([
        _FakeResponse({"data": [raw[0], raw[1]], "count": 2, "hasMore": True, "cursor": 1700000000}, headers=hdr),
        _FakeResponse({"data": [raw[2]], "count": 1, "hasMore": True, "cursor": 1700000099}, headers=hdr),
        _FakeResponse({"data": [], "count": 0, "hasMore": False, "cursor": 1700000099}, headers=hdr),
    ])
    res = client.cached_search(["JFK"], ["SYD"], ["Y", "J"], ["delta"], "2027-02-01", "2027-03-03")
    assert len(res.rows) == 3 and res.calls == 3 and not res.capped, (len(res.rows), res.calls)
    calls = client.session.calls
    assert "cursor" not in calls[0][1] and calls[0][1]["cabin"] == "economy,business", calls[0]
    assert calls[1][1]["cursor"] == 1700000000 and calls[1][1]["skip"] == 2, calls[1]
    assert calls[2][1]["cursor"] == 1700000000 and calls[2][1]["skip"] == 3, "cursor must stay the first one"
    assert store.get_state("quota_header") == "x-ratelimit-remaining"
    assert store.quota_remaining_today() == 990 and store.calls_today() == 3

    client.session = _FakeSession([
        _FakeResponse({"data": [raw[0]], "hasMore": True, "cursor": 5,
                       "moreURL": "/partnerapi/search?cursor=5&skip=1&x=1"}),
        _FakeResponse({"data": [raw[1]], "hasMore": False}),
    ])
    res = client.cached_search(["JFK"], ["SYD"], ["Y"], ["delta"], "2027-02-01", "2027-03-03")
    assert res.calls == 2 and client.session.calls[1][0] == "https://seats.aero/partnerapi/search?cursor=5&skip=1&x=1"

    client.session = _FakeSession([
        _FakeResponse({"data": [raw[0]], "hasMore": True, "cursor": 1}) for _ in range(5)])
    res = client.cached_search(["JFK"], ["SYD"], ["Y"], ["delta"], "2027-02-01", "2027-03-03")
    assert res.capped and res.calls == cfg["api"]["max_pages_per_query"]
    client.session = _FakeSession([_FakeResponse({"message": "bad cabin"}, status=400)])
    res = client.cached_search(["JFK"], ["SYD"], ["Y"], ["delta"], "2027-02-01", "2027-03-03")
    assert res.rows == [] and res.status == 400
    print("pagination ok: first cursor kept, skip accumulates, moreURL followed, page cap flagged")

    # -- budget with measured pagination ------------------------------------
    assert budget_plan(cfg, store)["per_window"] == 2.0
    store.set_state("sweep_stats", {"at": now, "windows": 10, "calls": 26,
                                    "calls_per_window": 2.6, "capped_windows": 1})
    plan = budget_plan(cfg, store)
    assert plan["per_window"] == 2.6 and plan["sweep_calls"] == 10 * 2.6 * 4, plan
    buf = io.StringIO()
    with redirect_stdout(buf):
        show_budget(cfg, store)
    assert "measured 2.60 calls per window" in buf.getvalue(), buf.getvalue()
    print("budget ok: plan multiplies by measured calls per window")

    # -- calibration --------------------------------------------------------
    cals = calibrate(cfg, store)
    assert all(c.refusal for c in cals.values()), "thin history must be refused"
    assert "200 viable combinations" in cals["Y"].refusal and "14 days" in cals["Y"].refusal
    assert cals["F"].combos == 1

    hist = Store(":memory:")
    old = (now_dt - timedelta(days=20)).isoformat()
    synthetic: list[Leg] = []
    for stamp in (old, now):
        for i in range(20):
            out_day = (date(2027, 3, 1) + timedelta(days=i)).isoformat()
            synthetic.append(Leg("s", "delta", "JFK", "SYD", out_day, "Y", 27000 + 700 * i,
                                 66.0, 4, "DL", False, stamp, stamp))
        for i in range(26):
            back_day = (date(2027, 3, 15) + timedelta(days=i)).isoformat()
            synthetic.append(Leg("s", "delta", "SYD", "JFK", back_day, "Y", 27000 + 500 * i,
                                 66.0, 4, "DL", False, stamp, stamp))
        hist.record_legs(synthetic)
        synthetic.clear()
    cal = calibrate(cfg, hist)["Y"]
    assert cal.refusal is None, cal.refusal
    assert cal.combos >= CALIBRATE_MIN_COMBOS and cal.span_days >= CALIBRATE_MIN_DAYS, cal
    assert cal.p10 > 40300, "percentile computed over single legs, not round trip totals"
    assert cal.floor == round_to(cal.p10, 5000) and cal.ceiling == round_to(cal.median, 5000)
    assert cal.floor < cal.ceiling and 55000 <= cal.floor <= 70000, cal
    assert percentile([1, 2, 3, 4], 50) == 2.5 and percentile([10], 90) == 10.0
    assert round_to(58600, 5000) == 60000 and round_to(57400, 5000) == 55000
    buf = io.StringIO()
    with redirect_stdout(buf):
        show_calibrate(cfg, hist)
    out = buf.getvalue()
    assert f"floor_miles: {cal.floor}" in out and "refused, need" in out, out
    print(f"calibrate ok: refuses thin history, proposes Y floor {cal.floor:,} "
          f"ceiling {cal.ceiling:,} from {cal.combos} combos over {cal.span_days:.0f} days")

    print("\nAll self-tests passed.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="seats.aero open-date award monitor, four cabin views. "
                    "Add --dry-run to print alerts instead of sending them.")
    ap.add_argument("--config", default="config.yaml", help="path to config, default config.yaml")
    modes = ap.add_argument_group("modes")
    modes.add_argument("--once", action="store_true", help="one focus poll")
    modes.add_argument("--sweep", action="store_true", help="one full calendar sweep")
    modes.add_argument("--loop", action="store_true", help="scheduler, sweep then focus polls")
    modes.add_argument("--probe", action="store_true", help="one live call, dump the raw response")
    modes.add_argument("--best", action="store_true", help="leaderboards, one per cabin")
    modes.add_argument("--calibrate", action="store_true", help="propose thresholds from history")
    modes.add_argument("--stats", action="store_true", help="observation history summary")
    modes.add_argument("--budget", action="store_true", help="API call plan and measured usage")
    modes.add_argument("--self-test", action="store_true", help="offline test, no network")
    modes.add_argument("--test-alert", action="store_true", help="send one Pushover message")
    opts = ap.add_argument_group("options")
    opts.add_argument("--cabin", choices=CABIN_ORDER, help="--best for one cabin only")
    opts.add_argument("--limit", type=int, default=15, help="--best rows per section, default 15")
    opts.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    opts.add_argument("--verbose", action="store_true", help="debug logging, logs one raw row per call")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    if args.self_test:
        # Runs against an in-memory store and its own config so it never
        # touches the real database. The real config is still validated.
        if os.path.exists(args.config):
            load_config(args.config)
            print(f"config ok: {args.config} validates")
        self_test()
        return

    cfg = load_config(args.config)
    store = Store(cfg["storage"]["db_path"])

    if args.best:
        show_best(cfg, store, limit=args.limit, cabin=args.cabin)
        return
    if args.calibrate:
        show_calibrate(cfg, store)
        return
    if args.stats:
        show_stats(cfg, store)
        return
    if args.budget:
        show_budget(cfg, store)
        return
    if args.test_alert:
        print("sent" if send_pushover(cfg["pushover"], "Award monitor online",
                                      "Watching the whole calendar, JFK to SYD.", 0) else "failed")
        return

    client = SeatsAero(cfg, store)

    if args.probe:
        run_probe(cfg, client)
        return
    if args.sweep:
        run_sweep(cfg, store, client, args.dry_run)
        return
    if args.once:
        run_focus(cfg, store, client, args.dry_run)
        return

    if args.loop:
        sweep_gap = timedelta(hours=cfg["scan"]["sweep_interval_hours"])
        interval = cfg["scan"]["focus_interval_minutes"] * 60
        LOG.info("Loop started. Sweep every %sh, focus every %smin.",
                 cfg["scan"]["sweep_interval_hours"], cfg["scan"]["focus_interval_minutes"])
        while True:
            try:
                last = store.get_state("last_sweep")
                due = True
                if last:
                    last_dt = datetime.fromisoformat(last)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    due = datetime.now(timezone.utc) - last_dt >= sweep_gap
                if due:
                    run_sweep(cfg, store, client, args.dry_run)
                else:
                    run_focus(cfg, store, client, args.dry_run)
                store.prune(cfg["storage"]["retain_observation_days"])
            except Exception:
                LOG.exception("Cycle failed, continuing")
            time.sleep(interval)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
