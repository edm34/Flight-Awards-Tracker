#!/usr/bin/env python3
"""
Open-date award monitor, several destinations, four cabin views.

No fixed travel dates. The monitor sweeps the entire bookable calendar for
every trip in the config, stores every observation as a tick, pairs every
legal outbound/return combination, and ranks them by total miles per person.
Each trip and cabin gets its own leaderboard, thresholds and running best, so
a cheap economy print to Mexico never hides a business print to Sydney.

Two scan modes share one API budget:

  sweep  full calendar, every trip, both directions
  focus  only the months around each trip's best combinations

Usage:
    python monitor.py --self-test         offline test, no network, no key
    python monitor.py --probe             one live call, dumps the raw response
    python monitor.py --sweep             one full calendar sweep
    python monitor.py --once              one focus poll
    python monitor.py --loop              scheduler, alternates sweep and focus
    python monitor.py --best              overview, then every trip and cabin
    python monitor.py --best --trip asia --cabin J --limit 40
    python monitor.py --overview          where to go right now, one table
    python monitor.py --calibrate         propose thresholds from stored history
    python monitor.py --export FILE       write the dashboard data file
    python monitor.py --stats             observation history summary
    python monitor.py --budget            API call plan, theoretical and measured
    python monitor.py --test-alert        send one Pushover message
Add --dry-run to print alerts instead of sending them.
"""

from __future__ import annotations

import argparse
import bisect
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
# the config file, the leaderboards, the alerts and the dashboard all agree.
CABIN_ORDER = ("Y", "W", "J", "F")
CABIN_NAMES = {"Y": "Economy", "W": "Premium Economy", "J": "Business", "F": "First"}

# seats.aero names its response fields with one letter cabin codes but filters
# rows with word values under a "cabins" parameter. Both confirmed live on
# 11 Sep 2026, the server's own moreURL spells it this way.
CABIN_QUERY_VALUES = {"Y": "economy", "W": "premium", "J": "business", "F": "first"}

# Daily quota header, confirmed live. The API also sends x-ratelimit-limit
# (1000) and x-ratelimit-reset (seconds until the UTC midnight reset).
QUOTA_HEADER = "x-ratelimit-remaining"

# --calibrate refuses to propose thresholds on thinner history than this.
CALIBRATE_MIN_COMBOS = 200
CALIBRATE_MIN_DAYS = 14

# The overview compares today's best against the median of the last few
# weeks of snapshots, once there are at least this many days of them.
TYPICAL_WINDOW_DAYS = 21
TYPICAL_MIN_DAYS = 14

# How deep into each ranked list evaluate() looks for alerts.
ALERT_SCAN_DEPTH = 20

# Qualifying pairings beyond the cheapest are listed in the same message, one
# line each, up to this many. Pushover caps a message at 1024 characters.
DIGEST_ROWS = 6

# The state database is force-pushed to a git branch. GitHub refuses a file
# over 100 MiB, and the save step failed on exactly that once. Warn early.
DB_SIZE_WARN_MB = 70

# Rows per trip and cabin written to the dashboard file.
DASHBOARD_ROWS = 15
DASHBOARD_HISTORY_DAYS = 45
DASHBOARD_HISTORY_FULL_DAYS = 2

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
    trip: str = ""

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
        """Binding seat count across both legs. Zero means at least one
        program published nothing, which is not the same as zero seats."""
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
        """Dedupe key. Both fingerprints carry route and cabin, so the same
        dates in a different cabin or to a different city are a different
        alert."""
        raw = f"{self.outbound.fingerprint()}|{self.inbound.fingerprint()}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _cabin_rank(code: str) -> int:
    return CABIN_ORDER.index(code) if code in CABIN_ORDER else len(CABIN_ORDER)


def seat_status(rt: RoundTrip, min_seats: int) -> tuple[str, str]:
    """(status, note). confirmed means both programs published a count of
    at least min_seats. unpublished means at least one leg's program prints
    no count at all, which is a maybe, not a yes. short means a published
    count below what the party needs."""
    seats = rt.min_seats
    if seats == 0:
        return "unpublished", "seats unpublished, verify before booking"
    if seats < min_seats:
        return "short", f"only {seats} seats"
    return "confirmed", f"{seats} seats confirmed"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REQUIRED_SECTIONS = ("api", "horizon", "scan", "origins", "trips", "cabins",
                     "alerting", "pushover", "storage")

# Keys a cabin block may carry at the top level (defaults for every trip) and
# the price keys that only make sense per trip, since Mexico and Sydney are
# not priced on the same scale.
CABIN_DEFAULT_KEYS = ("label", "enabled", "sources", "min_seats", "max_total_taxes_usd",
                      "pushover_priority", "new_best_margin_miles")
CABIN_PRICE_KEYS = ("benchmark_miles", "floor_miles", "ceiling_miles")


def load_config(path: str) -> dict:
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    return validate_config(raw, source=path)


def validate_config(raw: dict | None, source: str = "config") -> dict:
    """Apply defaults, fail loudly on anything that would misbehave silently."""

    def fail(msg: str) -> None:
        raise SystemExit(f"{source}: {msg}")

    cfg = copy.deepcopy(raw or {})
    if "trip" in cfg:
        fail("this config predates trips. The trip section became party_size, "
             "origins, pairing and a trips map. See README.md, Trips.")
    missing = [s for s in REQUIRED_SECTIONS if s not in cfg or cfg[s] is None]
    if missing:
        fail(f"missing section(s) {', '.join(missing)}")

    party = cfg.get("party_size")
    if not isinstance(party, int) or party < 1:
        fail("party_size must be a positive integer")
    origins = cfg["origins"]
    if not origins:
        fail("origins must list at least one airport")
    cfg["origins"] = [str(a).upper() for a in origins]

    pairing = cfg.setdefault("pairing", {}) or {}
    pairing.setdefault("require_same_source", True)
    pairing.setdefault("allow_open_jaw", False)
    pairing.setdefault("allow_mixed_cabin", False)
    cfg["pairing"] = pairing

    # Cabin defaults. No prices here, those are per trip.
    cabins_raw = cfg["cabins"]
    if not isinstance(cabins_raw, dict) or not cabins_raw:
        fail("cabins must be a map keyed by cabin code (Y, W, J, F)")
    unknown = [c for c in cabins_raw if c not in CABIN_ORDER]
    if unknown:
        fail(f"unknown cabin code(s) {', '.join(map(str, unknown))}. Use Y, W, J or F.")
    for code, block in cabins_raw.items():
        block = block or {}
        priced = [k for k in CABIN_PRICE_KEYS if block.get(k) is not None]
        if priced:
            fail(f"cabins.{code} carries {', '.join(priced)}. Prices live per trip, "
                 f"under trips.<key>.cabins.{code}, because destinations are not "
                 "priced on the same scale.")
        cabins_raw[code] = block

    trips_raw = cfg["trips"]
    if not isinstance(trips_raw, dict) or not trips_raw:
        fail("trips must be a map with at least one trip")
    trips: dict[str, dict] = {}
    seen_dest: dict[str, str] = {}
    for key, t in trips_raw.items():
        t = dict(t or {})
        t["key"] = str(key)
        t.setdefault("label", str(key).title())
        t.setdefault("enabled", True)
        dests = t.get("destinations")
        if not dests:
            fail(f"trips.{key}.destinations must list at least one airport")
        t["destinations"] = [str(a).upper() for a in dests]
        for d in t["destinations"]:
            if d in cfg["origins"]:
                fail(f"trips.{key}: {d} is also an origin")
            if d in seen_dest:
                fail(f"trips.{key}: {d} already belongs to trips.{seen_dest[d]}")
            seen_dest[d] = str(key)
        for k in ("min_trip_nights", "max_trip_nights"):
            if not isinstance(t.get(k), int) or t[k] < 1:
                fail(f"trips.{key}.{k} must be a positive integer")
        if t["min_trip_nights"] > t["max_trip_nights"]:
            fail(f"trips.{key}: min_trip_nights is above max_trip_nights")

        overrides = t.get("cabins") or {}
        bad = [c for c in overrides if c not in CABIN_ORDER]
        if bad:
            fail(f"trips.{key}.cabins has unknown cabin code(s) {', '.join(map(str, bad))}")
        resolved: dict[str, dict] = {}
        for code in CABIN_ORDER:
            if code not in cabins_raw and code not in overrides:
                continue
            c = dict(cabins_raw.get(code) or {})
            c.update(overrides.get(code) or {})
            c["code"] = code
            c.setdefault("label", CABIN_NAMES[code])
            c.setdefault("enabled", True)
            c.setdefault("min_seats", party)
            c.setdefault("max_total_taxes_usd", None)
            for k in CABIN_PRICE_KEYS:
                c.setdefault(k, None)
            # An explicit null on these means the default, not a crash later.
            c["new_best_margin_miles"] = c.get("new_best_margin_miles") or 0
            c["pushover_priority"] = c.get("pushover_priority") or 0
            if not c.get("sources"):
                fail(f"trips.{key}.cabins.{code} has no sources. Set them on cabins.{code} "
                     "or on the trip.")
            c["sources"] = [str(s).lower() for s in c["sources"]]
            floor, ceiling = c["floor_miles"], c["ceiling_miles"]
            if (floor is None) != (ceiling is None):
                fail(f"trips.{key}.cabins.{code}: set both floor_miles and ceiling_miles, "
                     "or leave both null for observe-only")
            if floor is not None and floor > ceiling:
                fail(f"trips.{key}.cabins.{code}: floor_miles {floor:,} is above "
                     f"ceiling_miles {ceiling:,}")
            if not isinstance(c["min_seats"], int) or c["min_seats"] < 1:
                fail(f"trips.{key}.cabins.{code}.min_seats must be a positive integer")
            if c["min_seats"] > party:
                fail(f"trips.{key}.cabins.{code}.min_seats {c['min_seats']} is above "
                     f"party_size {party}")
            if not -2 <= int(c["pushover_priority"]) <= 2:
                fail(f"trips.{key}.cabins.{code}.pushover_priority must be between -2 and 2")
            resolved[code] = c
        if not any(c["enabled"] for c in resolved.values()):
            fail(f"trips.{key}: every cabin is disabled")
        t["cabins"] = resolved
        trips[str(key)] = t
    if not any(t["enabled"] for t in trips.values()):
        fail("every trip is disabled, nothing to scan")
    cfg["trips"] = trips
    cfg["dest_to_trip"] = seen_dest

    alerting = cfg["alerting"]
    alerting.setdefault("max_data_age_hours", 12)
    alerting.setdefault("require_seat_count", False)
    alerting.setdefault("improvement_threshold_miles", 0)
    alerting.setdefault("cooldown_hours", 6)

    scan = cfg["scan"]
    scan.setdefault("mode", "loop")
    if scan["mode"] not in ("loop", "scheduled"):
        fail(f"scan.mode must be loop or scheduled, not {scan['mode']!r}")
    scan.setdefault("sweeps_per_day", 4)
    scan.setdefault("polls_per_day", 48)
    scan.setdefault("max_focus_windows", 4)
    scan.setdefault("focus_pad_days", 10)

    api = cfg["api"]
    api.setdefault("budget_safety_margin", 0)
    api.setdefault("max_pages_per_query", 20)
    api.setdefault("page_size", 500)
    api.setdefault("timeout_seconds", 30)

    cfg["pushover"].setdefault("sound", "pushover")
    return cfg


def enabled_trips(cfg: dict) -> list[dict]:
    return [t for t in cfg["trips"].values() if t["enabled"]]


def enabled_cabins(trip: dict) -> list[dict]:
    return [c for c in trip["cabins"].values() if c["enabled"]]


def query_plan(trip: dict) -> tuple[list[str], list[str]]:
    """Cabin codes and the union of their sources for one API call."""
    cabins = enabled_cabins(trip)
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
    observed_at TEXT NOT NULL,
    last_confirmed_at TEXT
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

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    trip TEXT NOT NULL,
    cabin TEXT NOT NULL,
    best_miles INTEGER,
    viable INTEGER NOT NULL,
    unconfirmed INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(trip, cabin, at);
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
        if "last_confirmed_at" not in cols:
            self.conn.execute("ALTER TABLE observations ADD COLUMN last_confirmed_at TEXT")
            self.conn.execute("UPDATE observations SET last_confirmed_at = observed_at")
            self.conn.commit()
            dropped = self.collapse_repeated_ticks()
            if dropped:
                LOG.info("Collapsed %d repeated ticks into their first sighting", dropped)

    def collapse_repeated_ticks(self) -> int:
        """One-time repair for a database written a row per sighting. Runs of
        consecutive identical ticks for one leg collapse into the first, and
        that row's last_confirmed_at becomes the last repeat's observed_at.
        Nothing that changed is touched. The state branch had grown past
        GitHub's file limit on four days of this."""
        rows = self.conn.execute(
            """SELECT id, fingerprint, miles, taxes_usd, seats, airlines, direct, observed_at
               FROM observations ORDER BY fingerprint, id""").fetchall()
        delete: list[tuple[int]] = []
        touch: dict[int, str] = {}
        kept: sqlite3.Row | None = None
        for r in rows:
            same = (kept is not None and kept["fingerprint"] == r["fingerprint"]
                    and (kept["miles"], kept["taxes_usd"], kept["seats"], kept["airlines"] or "",
                         kept["direct"]) == (r["miles"], r["taxes_usd"], r["seats"],
                                             r["airlines"] or "", r["direct"]))
            if same:
                delete.append((r["id"],))
                touch[kept["id"]] = r["observed_at"]
            else:
                kept = r
        if delete:
            self.conn.executemany("DELETE FROM observations WHERE id = ?", delete)
            self.conn.executemany("UPDATE observations SET last_confirmed_at = ? WHERE id = ?",
                                  [(at, rid) for rid, at in touch.items()])
            self.conn.commit()
            self.conn.execute("VACUUM")
        return len(delete)

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
    def record_legs(self, legs: Iterable[Leg], record_unchanged: bool = False) -> tuple[int, int]:
        """Store what a scan saw. A leg that is new, or whose price, taxes,
        seats, airlines or routing changed since its latest tick, gets a new
        tick. A leg that is exactly as last seen only has last_confirmed_at
        touched on its latest tick. The price history stays complete, every
        change is a row, without a copy of every unchanged row every half
        hour. Four trips of a year are eighty thousand legs a sweep and the
        database lives on a git branch. record_unchanged restores a tick per
        sighting. Returns (ticks written, ticks touched)."""
        legs = list(legs)
        if not legs:
            return 0, 0
        wanted = {leg.fingerprint(): leg for leg in legs}
        latest: dict[str, sqlite3.Row] = {}
        if not record_unchanged:
            for r in self.conn.execute(
                    """SELECT o.id, o.fingerprint, o.miles, o.taxes_usd, o.seats, o.airlines, o.direct
                       FROM observations o
                       JOIN (SELECT fingerprint, MAX(id) AS mid FROM observations GROUP BY fingerprint) l
                         ON o.id = l.mid"""):
                if r["fingerprint"] in wanted:
                    latest[r["fingerprint"]] = r
        inserts, touches = [], []
        for fp, leg in wanted.items():
            cur = latest.get(fp)
            if cur is not None and (cur["miles"], cur["taxes_usd"], cur["seats"], cur["airlines"] or "",
                                    bool(cur["direct"])) == (leg.miles, leg.taxes_usd, leg.seats,
                                                             leg.airlines, leg.direct):
                touches.append((leg.observed_at, leg.last_seen, cur["id"]))
                continue
            inserts.append((
                fp, leg.availability_id, leg.source, leg.origin, leg.destination,
                leg.depart_date, leg.cabin, leg.miles, leg.taxes_usd, leg.taxes_currency,
                leg.seats, leg.airlines, int(leg.direct), leg.last_seen, leg.observed_at,
                leg.observed_at,
            ))
        if inserts:
            self.conn.executemany(
                """INSERT INTO observations
                   (fingerprint, availability_id, source, origin, destination,
                    depart_date, cabin, miles, taxes_usd, taxes_currency, seats,
                    airlines, direct, last_seen, observed_at, last_confirmed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                inserts,
            )
        if touches:
            self.conn.executemany(
                "UPDATE observations SET last_confirmed_at = ?, last_seen = ? WHERE id = ?", touches)
        self.conn.commit()
        return len(inserts), len(touches)

    def latest_legs(self, max_age_hours: float) -> list[Leg]:
        """Most recent observation per fingerprint, within the freshness window."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        rows = self.conn.execute(
            """SELECT o.* FROM observations o
               JOIN (SELECT fingerprint, MAX(id) AS mid FROM observations
                     GROUP BY fingerprint) latest
                 ON o.id = latest.mid
               WHERE COALESCE(o.last_confirmed_at, o.observed_at) >= ?""",
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
        """Per cabin, first sighting, last confirmation and tick count."""
        rows = self.conn.execute(
            """SELECT cabin, MIN(observed_at) AS first,
                      MAX(COALESCE(last_confirmed_at, observed_at)) AS last,
                      COUNT(*) AS n FROM observations GROUP BY cabin"""
        ).fetchall()
        return {r["cabin"]: (r["first"], r["last"], r["n"]) for r in rows}

    # -- snapshots --------------------------------------------------------
    def record_snapshot(self, at: str, trip: str, cabin: str, best: int | None,
                        viable: int, unconfirmed: int) -> None:
        self.conn.execute(
            "INSERT INTO snapshots(at, trip, cabin, best_miles, viable, unconfirmed) "
            "VALUES (?,?,?,?,?,?)", (at, trip, cabin, best, viable, unconfirmed))
        self.conn.commit()

    def snapshot_history(self, trip: str, cabin: str, days: int) -> list[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return self.conn.execute(
            """SELECT at, best_miles, viable, unconfirmed FROM snapshots
               WHERE trip = ? AND cabin = ? AND at >= ? ORDER BY at""",
            (trip, cabin, cutoff)).fetchall()

    def typical_best(self, trip: str, cabin: str) -> float | None:
        """Median best over the last TYPICAL_WINDOW_DAYS of snapshots, only
        when the snapshots span at least TYPICAL_MIN_DAYS."""
        rows = [r for r in self.snapshot_history(trip, cabin, TYPICAL_WINDOW_DAYS)
                if r["best_miles"] is not None]
        if len(rows) < 2:
            return None
        span = (datetime.fromisoformat(rows[-1]["at"]) - datetime.fromisoformat(rows[0]["at"]))
        if span.total_seconds() / 86400 < TYPICAL_MIN_DAYS:
            return None
        return percentile([r["best_miles"] for r in rows], 50)

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
             json.dumps({"out": asdict(rt.outbound), "in": asdict(rt.inbound), "trip": rt.trip})),
        )
        self.conn.commit()

    def prune(self, days: int, snapshot_days: int = 400, today: date | None = None) -> int:
        """Keep the database bounded. Legs whose departure date has passed go.
        Change ticks older than the retention go, except each leg's latest
        tick, which ranking and calibration read and which keeps its first
        sighting date. Snapshots, the dashboard's history, keep longer, they
        are tiny. VACUUM so the file on the state branch actually shrinks."""
        today = today or datetime.now(timezone.utc).date()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        flown = self.conn.execute(
            "DELETE FROM observations WHERE depart_date < ?", (today.isoformat(),)).rowcount
        stale = self.conn.execute(
            """DELETE FROM observations
               WHERE COALESCE(last_confirmed_at, observed_at) < ?
                 AND id NOT IN (SELECT MAX(id) FROM observations GROUP BY fingerprint)""",
            (cutoff,)).rowcount
        snap_cutoff = (datetime.now(timezone.utc) - timedelta(days=snapshot_days)).isoformat()
        self.conn.execute("DELETE FROM snapshots WHERE at < ?", (snap_cutoff,))
        self.conn.commit()
        if flown or stale:
            self.conn.execute("VACUUM")
        return flown + stale


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


def month_window(day: str, pad_days: int = 10) -> tuple[str, str]:
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
            "User-Agent": "personal-award-monitor/4.0",
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
            "cabins": ",".join(CABIN_QUERY_VALUES[c] for c in cabins),
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
        if QUOTA_HEADER not in resp.headers:
            return None
        if self.store.get_state("quota_header") != QUOTA_HEADER:
            self.store.set_state("quota_header", QUOTA_HEADER)
        try:
            return int(resp.headers[QUOTA_HEADER])
        except ValueError:
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

    Field names were reconciled against a live response on 11 Sep 2026.
    <cabin>MileageCost is a string of digits, <cabin>TotalTaxes an integer in
    the minor unit of TaxesCurrency (40860 USD is $408.60), UpdatedAt is the
    freshness stamp. Every field also has a *Raw twin holding the value
    before seats.aero's own quality filter. The parser reads the filtered
    set, which is what the seats.aero site shows."""
    route = obj.get("Route") or {}
    origin = route.get("OriginAirport") or ""
    destination = route.get("DestinationAirport") or ""
    source = obj.get("Source") or route.get("Source") or "unknown"
    depart_date = (obj.get("Date") or "")[:10]
    last_seen = obj.get("UpdatedAt") or ""
    currency = obj.get("TaxesCurrency") or "USD"
    now = datetime.now(timezone.utc).isoformat()

    legs: list[Leg] = []
    for cabin in cabins:
        if not obj.get(f"{cabin}Available"):
            continue
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


def build_round_trips(legs: list[Leg], origins: list[str], destinations: list[str],
                      min_nights: int, max_nights: int, pairing: dict,
                      trip_key: str = "") -> list[RoundTrip]:
    """Pair every outbound leg with every legal return leg for one trip.

    Returns are bucketed by everything a pairing must match on (arrival
    airport unless open jaw, program unless sources may differ, cabin unless
    mixed cabins are allowed) and sorted by date, so each outbound only walks
    the returns inside its night window. Four trips of a year each are tens
    of thousands of legs, and the naive cross product took minutes."""
    out_origins, out_dests = set(origins), set(destinations)
    outbound = [l for l in legs if l.origin in out_origins and l.destination in out_dests]
    inbound = [l for l in legs if l.origin in out_dests and l.destination in out_origins]

    same_source = pairing["require_same_source"]
    open_jaw = pairing["allow_open_jaw"]
    mixed_ok = pairing["allow_mixed_cabin"]

    def bucket_key(leg: Leg, airport: str) -> tuple:
        return (airport if not open_jaw else "",
                leg.source if same_source else "",
                leg.cabin if not mixed_ok else "")

    buckets: dict[tuple, list[Leg]] = {}
    for leg in inbound:
        buckets.setdefault(bucket_key(leg, leg.origin), []).append(leg)
    dates: dict[tuple, list[str]] = {}
    for key, group in buckets.items():
        group.sort(key=lambda l: l.depart_date)
        dates[key] = [l.depart_date for l in group]

    pairs: list[RoundTrip] = []
    for out in outbound:
        key = bucket_key(out, out.destination)
        group = buckets.get(key)
        if not group:
            continue
        day = datetime.strptime(out.depart_date, "%Y-%m-%d").date()
        lo = (day + timedelta(days=min_nights)).isoformat()
        hi = (day + timedelta(days=max_nights)).isoformat()
        ds = dates[key]
        for back in group[bisect.bisect_left(ds, lo):bisect.bisect_right(ds, hi)]:
            pairs.append(RoundTrip(out, back, trip_key))
    return pairs


def viable(rt: RoundTrip, cabin_cfg: dict, alert_cfg: dict) -> tuple[bool, str]:
    """Seat and tax feasibility for one cabin. Price is judged separately.
    An unpublished seat count passes unless require_seat_count is set, and
    the note says so, because a maybe must never read as a yes."""
    cap = cabin_cfg.get("max_total_taxes_usd")
    if cap is not None and rt.total_taxes > cap:
        return False, "taxes too high"
    status, note = seat_status(rt, cabin_cfg["min_seats"])
    if status == "unpublished":
        if alert_cfg["require_seat_count"]:
            return False, "seat count not published"
        return True, note
    if status == "short":
        return False, note
    return True, note


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


Ranked = dict[str, dict[str, list[tuple[RoundTrip, str]]]]


def rank_all(cfg: dict, legs: list[Leg]) -> Ranked:
    """Every enabled trip and cabin, ascending by total miles, viable pairings
    only. A cabin's list only holds pairings from that cabin's own sources.
    This is the one place pairings become ranked lists, so alerts, the
    leaderboards, calibration and the dashboard can't drift apart."""
    alert_cfg, pairing = cfg["alerting"], cfg["pairing"]
    ranked: Ranked = {}
    for trip in enabled_trips(cfg):
        key = trip["key"]
        ranked[key] = {c["code"]: [] for c in enabled_cabins(trip)}
        pairs = build_round_trips(legs, cfg["origins"], trip["destinations"],
                                  trip["min_trip_nights"], trip["max_trip_nights"],
                                  pairing, key)
        for rt in pairs:
            ccfg = trip["cabins"].get(rt.cabin)
            if ccfg is None or not ccfg["enabled"]:
                continue
            if rt.outbound.source not in ccfg["sources"] or rt.inbound.source not in ccfg["sources"]:
                continue
            ok, note = viable(rt, ccfg, alert_cfg)
            if ok:
                ranked[key][rt.cabin].append((rt, note))
        for rows in ranked[key].values():
            rows.sort(key=lambda x: (x[0].total_miles, x[0].outbound.depart_date))
    return ranked


def choose_focus_windows(ranked: Ranked, cap: int) -> list[dict]:
    """Where to re-poll between sweeps. Every trip's economy top first, then
    every trip's premium top, and so on, so a later trip is never starved by
    an earlier one's four cabins. Then fill by global rank. One window per
    trip and month, capped."""
    focus: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def take(rt: RoundTrip) -> None:
        month = (rt.trip, rt.outbound.depart_date[:7])
        if month in seen or len(focus) >= cap:
            return
        seen.add(month)
        focus.append({"trip": rt.trip, "date": rt.outbound.depart_date})

    for code in CABIN_ORDER:
        for cabins in ranked.values():
            rows = cabins.get(code)
            if rows:
                take(rows[0][0])
    everything = sorted((rt for cabins in ranked.values() for rows in cabins.values()
                         for rt, _ in rows), key=lambda rt: rt.total_miles)
    for rt in everything:
        if len(focus) >= cap:
            break
        take(rt)
    return focus


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


def format_alert(rt: RoundTrip, seat_note: str, reason: str, party: int,
                 trip: dict, cabin_cfg: dict) -> tuple[str, str]:
    label = cabin_cfg["label"].upper()
    if rt.mixed:
        label += f" ({rt.cabin_code} mixed)"
    title = (f"{rt.total_miles // 1000}k {trip['label'].upper()} {label} "
             f"{rt.outbound.source.upper()} {rt.route}")
    status, _ = seat_status(rt, cabin_cfg["min_seats"])
    if status != "confirmed":
        title += ", seats unconfirmed"
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
    if status != "confirmed":
        lines.append(f"{cabin_cfg['min_seats']} seats together are NOT confirmed. "
                     "The program publishes no count, check before you count on it.")
    bench = cabin_cfg.get("benchmark_miles")
    if bench:
        gap = bench - rt.total_miles
        lines.append(f"vs {cabin_cfg['label'].lower()} benchmark {bench:,}: "
                     f"{'saves' if gap >= 0 else 'costs'} {abs(gap):,} per person")
    lines += [f"Trigger: {reason}", "", FARE_BRAND_REMINDER]
    return title, "\n".join(lines)


def format_digest(hits: list[tuple[RoundTrip, str, str]], party: int,
                  trip: dict, cabin_cfg: dict) -> tuple[str, str]:
    """One message per trip and cabin per pass. The cheapest qualifying
    pairing in full, then the other qualifying dates one line each. Twenty
    return dates at one price are one message, not twenty. The first live
    sweep sent twenty and that is why this exists."""
    rt, note, reason = hits[0]
    title, body = format_alert(rt, note, reason, party, trip, cabin_cfg)
    if len(hits) == 1:
        return title, body
    title += f" +{len(hits) - 1} more"
    lines = body.split("\n")
    extra = ["Also qualifying, per person"]
    for other, _, _ in hits[1:DIGEST_ROWS + 1]:
        seats = f"  {other.min_seats} seats" if other.min_seats else "  seats ?"
        extra.append(f"{other.outbound.depart_date} > {other.inbound.depart_date}  "
                     f"{other.outbound.source} {other.route}  {other.total_miles:,}{seats}")
    rest = len(hits) - 1 - DIGEST_ROWS
    if rest > 0:
        extra.append(f"and {rest} more, run --best --trip {trip['key']} --cabin {cabin_cfg['code']}")
    # body ends with a blank line and the fare brand reminder. Keep them last.
    return title, "\n".join(lines[:-2] + extra + lines[-2:])


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


def fetch_window(client: SeatsAero, store: Store, cfg: dict, trip: dict,
                 start: str, end: str) -> tuple[int, int, bool]:
    """Both directions for one trip and one date window. Returns legs stored,
    API calls used and whether either direction hit the page cap."""
    codes, sources = query_plan(trip)
    legs_total, calls, capped = 0, 0, False
    for origins, dests in ((cfg["origins"], trip["destinations"]),
                           (trip["destinations"], cfg["origins"])):
        res = client.cached_search(origins, dests, codes, sources, start, end)
        legs = [leg for obj in res.rows for leg in parse_availability(obj, codes)]
        store.record_legs(legs, cfg["storage"].get("record_unchanged", False))
        legs_total += len(legs)
        calls += res.calls
        capped = capped or res.capped
    return legs_total, calls, capped


def evaluate(cfg: dict, store: Store, dry_run: bool) -> Ranked:
    """Rank everything currently known, per trip and cabin, alert on what
    qualifies, record a snapshot of every list for the price history."""
    alert_cfg, party = cfg["alerting"], cfg["party_size"]
    legs = store.latest_legs(alert_cfg["max_data_age_hours"])
    ranked = rank_all(cfg, legs)
    now = datetime.now(timezone.utc).isoformat()
    LOG.info("%d fresh legs, viable pairings: %s", len(legs), ", ".join(
        f"{t} " + "/".join(f"{c}{len(r)}" for c, r in cabins.items())
        for t, cabins in ranked.items()))

    for trip_key, cabins in ranked.items():
        trip = cfg["trips"][trip_key]
        for code, rows in cabins.items():
            ccfg = trip["cabins"][code]
            unconfirmed = sum(1 for rt, _ in rows if seat_status(rt, ccfg["min_seats"])[0] != "confirmed")
            store.record_snapshot(now, trip_key, code, rows[0][0].total_miles if rows else None,
                                  len(rows), unconfirmed)
            if not rows:
                continue
            state_key = f"best_total_miles:{trip_key}:{code}"
            prev_best = store.get_state(state_key)
            best_rt = rows[0][0]

            # rows is ascending, so walk it with a running best. Without this
            # every combination compares against the same stale figure and a
            # cold start alerts on the whole leaderboard instead of just the
            # winner. The best is per trip and cabin so a cheap economy pairing
            # never suppresses business, and Mexico never suppresses Sydney.
            running_best = prev_best
            hits: list[tuple[RoundTrip, str, str]] = []
            for rt, note in rows[:ALERT_SCAN_DEPTH]:
                reason = alert_reason(rt, running_best, ccfg)
                if not reason or not should_alert(rt, store, alert_cfg):
                    continue
                hits.append((rt, note, reason))
                running_best = min(running_best or rt.total_miles, rt.total_miles)

            # Everything that qualified goes out as one message.
            if hits:
                title, message = format_digest(hits, party, trip, ccfg)
                if dry_run:
                    print(f"\n--- WOULD ALERT (priority {ccfg['pushover_priority']}) ---\n{title}\n{message}")
                    sent = True
                else:
                    sent = send_pushover(cfg["pushover"], title, message, ccfg["pushover_priority"])
                    if sent:
                        LOG.info("Alerted: %s, %d pairing(s)", title, len(hits))
                if sent:
                    for rt, _, _ in hits:
                        store.save_alert(rt)

            if prev_best is None or best_rt.total_miles < prev_best:
                store.set_state(state_key, best_rt.total_miles)

    focus = choose_focus_windows(ranked, cfg["scan"]["max_focus_windows"])
    if focus:
        store.set_state("focus_windows", focus)
    return ranked


def prune_history(cfg: dict, store: Store) -> int:
    """Drop observations past the retention window. Runs at the end of every
    sweep and focus poll so scheduled mode, which never enters the loop,
    still keeps the database bounded."""
    dropped = store.prune(cfg["storage"]["retain_observation_days"],
                          cfg["storage"].get("retain_snapshot_days", 400))
    if dropped:
        LOG.info("Pruned %d observations, flown dates and change ticks older than %d days",
                 dropped, cfg["storage"]["retain_observation_days"])
    path = cfg["storage"]["db_path"]
    if path != ":memory:" and os.path.exists(path):
        size_mb = os.path.getsize(path) / 1e6
        if size_mb > DB_SIZE_WARN_MB:
            LOG.warning("Database is %.0f MB. GitHub refuses files over 100 MiB and the state "
                        "branch would stop saving. Lower retain_observation_days or move state "
                        "off git.", size_mb)
    return dropped


def run_sweep(cfg: dict, store: Store, client: SeatsAero, dry_run: bool) -> None:
    chunks = horizon_chunks(cfg)
    trips = enabled_trips(cfg)
    LOG.info("Sweep starting, %d windows x %d trips, %d calls left today",
             len(chunks), len(trips), client.budget_left())
    windows_done, calls_total, capped_windows = 0, 0, 0
    for start, end in chunks:
        for trip in trips:
            if client.budget_left() < 4:
                LOG.warning("Stopping sweep early to protect focus polling")
                break
            legs, calls, capped = fetch_window(client, store, cfg, trip, start, end)
            windows_done += 1
            calls_total += calls
            capped_windows += int(capped)
            LOG.info("  %s to %s %-10s %5d legs, %d calls%s", start, end, trip["key"],
                     legs, calls, ", PAGE CAP HIT" if capped else "")
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
    prune_history(cfg, store)


def run_focus(cfg: dict, store: Store, client: SeatsAero, dry_run: bool) -> None:
    windows = store.get_state("focus_windows") or []
    windows = [w for w in windows if w.get("trip") in cfg["trips"] and cfg["trips"][w["trip"]]["enabled"]]
    if not windows:
        LOG.info("No focus windows yet, running a sweep instead")
        run_sweep(cfg, store, client, dry_run)
        return
    calls_total = 0
    pad = cfg["scan"].get("focus_pad_days", 10)
    for w in windows:
        start, end = month_window(w["date"], pad)
        _, calls, _ = fetch_window(client, store, cfg, cfg["trips"][w["trip"]], start, end)
        calls_total += calls
    LOG.info("Focus poll of %d windows done in %d calls, %d calls left",
             len(windows), calls_total, client.budget_left())
    evaluate(cfg, store, dry_run)
    prune_history(cfg, store)


def run_probe(cfg: dict, client: SeatsAero) -> None:
    """One live call. Prints everything needed to reconcile the parser
    against the real API, then stops. Costs exactly one call."""
    trip = enabled_trips(cfg)[0]
    codes, sources = query_plan(trip)
    start, end = horizon_chunks(cfg)[0]
    params = client.base_params(cfg["origins"], trip["destinations"], codes, sources,
                                start, end, take=25)
    print(f"\nPROBE  one cached search call, trip {trip['key']}")
    for k, v in params.items():
        print(f"  {k:20} {v}")

    t0 = time.monotonic()
    resp = client.get(f"{client.cfg['base_url']}/search", params)
    if resp is None:
        print("\nTransport failure, see log above.")
        return
    print(f"\nHTTP {resp.status_code} in {time.monotonic() - t0:.2f}s")
    print("Response headers")
    for k, v in resp.headers.items():
        tag = "   <- daily quota" if k.lower() == QUOTA_HEADER else ""
        print(f"  {k}: {v}{tag}")
    if QUOTA_HEADER not in resp.headers:
        print(f"  ({QUOTA_HEADER} not present, budget tracking will run blind)")
    if resp.status_code != 200:
        print(f"\nBody: {resp.text[:800]}")
        print("\nA 400 here usually means a query value is wrong. "
              "The cabins filter sends word values, see CABIN_QUERY_VALUES.")
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
        ("UpdatedAt", obj.get("UpdatedAt")),
        ("TaxesCurrency", obj.get("TaxesCurrency")),
    ]
    for name, val in checks:
        print(f"  {name:26} {'ok' if val not in (None, '') else 'MISSING':8} {val!r}")
    for code in CABIN_ORDER:
        fields = ["Available", "MileageCost", "RemainingSeats", "TotalTaxes",
                  "Airlines", "Direct"]
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
    print("\nCompare one row against the program's own site. If the taxes are off "
          "by 100x, TotalTaxes is not in the minor unit.")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

BEST_HEADER = (f"{'out':11} {'back':11} {'nts':>4} {'route':9} {'prog':14} "
               f"{'miles':>8} {'vs bench':>9} {'seats':>6} {'party':>10}")


def seats_cell(rt: RoundTrip, min_seats: int) -> str:
    status, _ = seat_status(rt, min_seats)
    return str(rt.min_seats) if status == "confirmed" else "?"


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


def print_best_section(trip: dict, c: dict, rows: list[tuple[RoundTrip, str]],
                       legs_seen: int, party: int, limit: int, show_cabins: bool) -> None:
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
        vs = f"{bench - rt.total_miles:>+9,}" if bench else f"{'-':>9}"
        line = (f"{rt.outbound.depart_date:11} {rt.inbound.depart_date:11} "
                f"{rt.nights:>4} {rt.route:9} {rt.outbound.source:14} "
                f"{rt.total_miles:>8,} {vs} {seats_cell(rt, c['min_seats']):>6} "
                f"{rt.total_miles * party:>10,}")
        if show_cabins:
            line += f"  {rt.cabin_code}"
        print(line)
    shown = min(limit, len(rows))
    tail = f"{shown} of {len(rows):,} viable combinations"
    if bench:
        under = sum(1 for rt, _ in rows if rt.total_miles < bench)
        tail += f", {under:,} beat the benchmark"
    unconfirmed = sum(1 for rt, _ in rows if seat_status(rt, c["min_seats"])[0] != "confirmed")
    if unconfirmed:
        tail += f", {unconfirmed:,} with no published seat count (shown as ?)"
    print(f"  {tail}")
    print()


def overview_rows(cfg: dict, store: Store, ranked: Ranked) -> list[dict]:
    """One row per enabled trip. Cheapest viable per cabin with seat status,
    and the gap against the recent typical best when there is history."""
    out = []
    for trip in enabled_trips(cfg):
        cabins = ranked.get(trip["key"], {})
        row = {"key": trip["key"], "label": trip["label"],
               "destinations": trip["destinations"], "cabins": {}}
        for code, rows in cabins.items():
            ccfg = trip["cabins"][code]
            cell: dict[str, Any] = {"viable": len(rows), "best": None, "seats": None,
                                    "status": None, "vs_typical": None, "vs_bench": None}
            if rows:
                rt = rows[0][0]
                status, _ = seat_status(rt, ccfg["min_seats"])
                cell.update({"best": rt.total_miles, "seats": rt.min_seats, "status": status})
                typical = store.typical_best(trip["key"], code)
                if typical:
                    cell["vs_typical"] = (rt.total_miles - typical) / typical
                if ccfg["benchmark_miles"]:
                    cell["vs_bench"] = ccfg["benchmark_miles"] - rt.total_miles
            row["cabins"][code] = cell
        out.append(row)
    # Cheapest economy first, trips with nothing at the bottom.
    out.sort(key=lambda r: (r["cabins"].get("Y", {}).get("best") is None,
                            r["cabins"].get("Y", {}).get("best") or 0))
    return out


def print_overview(cfg: dict, store: Store, ranked: Ranked) -> None:
    rows = overview_rows(cfg, store, ranked)
    party = cfg["party_size"]
    print(f"WHERE TO GO RIGHT NOW  cheapest viable round trip per person, party of {party}")
    print("? means the program publishes no seat count, so the seats are not confirmed. "
          "vs typical compares to the median best of the last 3 weeks.\n")
    print(f"{'trip':16} " + " ".join(f"{CABIN_NAMES[c]:>19}" for c in CABIN_ORDER))
    print("-" * (17 + 20 * len(CABIN_ORDER)))
    for r in rows:
        cells = []
        for code in CABIN_ORDER:
            cell = r["cabins"].get(code)
            if not cell or cell["best"] is None:
                cells.append(f"{'-':>19}")
                continue
            mark = "" if cell["status"] == "confirmed" else " ?"
            typ = cell["vs_typical"]
            gap = f" {typ:+.0%}" if typ is not None else ""
            cells.append(f"{cell['best']:>12,}{mark:2}{gap:>5}")
        print(f"{r['label']:16} " + " ".join(cells))
    print()
    # Per cabin, the trip that wins outright. Reads like a tip sheet.
    tips = []
    for code in CABIN_ORDER:
        best = [(r["cabins"][code]["best"], r["label"]) for r in rows
                if code in r["cabins"] and r["cabins"][code]["best"] is not None]
        if best:
            miles, label = min(best)
            tips.append(f"{CABIN_NAMES[code].lower()} {label} at {miles:,}")
    if tips:
        print("Cheapest by cabin  " + ", ".join(tips))
    print()


def show_best(cfg: dict, store: Store, limit: int = 15, trip_key: str | None = None,
              cabin: str | None = None) -> None:
    party, alert_cfg = cfg["party_size"], cfg["alerting"]
    age_hours = alert_cfg["max_data_age_hours"] * 24
    legs = store.latest_legs(age_hours)
    ranked = rank_all(cfg, legs)

    trips = enabled_trips(cfg)
    if trip_key:
        trips = [t for t in trips if t["key"] == trip_key]
        if not trips:
            raise SystemExit(f"--trip {trip_key} is not an enabled trip. Enabled: "
                             + ", ".join(t["key"] for t in enabled_trips(cfg)))

    # A GitHub job summary renders markdown, which would collapse the columns.
    # Fence the whole report there. Plain terminals get plain text.
    fence = os.environ.get("GITHUB_ACTIONS") == "true"
    if fence:
        print("```")
    print(f"\nCheapest round trips for {party} from {'/'.join(cfg['origins'])}, per person, "
          f"data from the last {age_hours // 24} days\n")
    if not legs:
        print("Nothing recorded yet. Run --sweep first.\n")
    if not trip_key and not cabin:
        print_overview(cfg, store, ranked)
    for trip in trips:
        cabins = enabled_cabins(trip)
        if cabin:
            cabins = [c for c in cabins if c["code"] == cabin]
            if not cabins:
                raise SystemExit(f"--cabin {cabin} is not enabled for {trip['key']}. Enabled: "
                                 + ", ".join(c["code"] for c in enabled_cabins(trip)))
        print(f"=== {trip['label'].upper()}  {'/'.join(trip['destinations'])}, "
              f"{trip['min_trip_nights']} to {trip['max_trip_nights']} nights\n")
        seen = {code: sum(1 for l in legs if l.cabin == code and
                          (l.destination in trip["destinations"] or l.origin in trip["destinations"]))
                for code in CABIN_ORDER}
        for c in cabins:
            print_best_section(trip, c, ranked[trip["key"]][c["code"]], seen[c["code"]],
                               party, limit, show_cabins=cfg["pairing"]["allow_mixed_cabin"])
    if fence:
        print("```")


def show_overview(cfg: dict, store: Store) -> None:
    legs = store.latest_legs(cfg["alerting"]["max_data_age_hours"] * 24)
    print()
    print_overview(cfg, store, rank_all(cfg, legs))


def show_calendar(cfg: dict, store: Store, trip_key: str | None, cabin: str | None,
                  direction: str = "out") -> None:
    """Twelve months of departure (or return) days for one trip and cabin.
    Each cell is the cheapest viable round trip in thousands of miles per
    person, with ? when the seats are not confirmed, a dot when a one-way leg
    exists but nothing pairs, and blank outside the scanned horizon."""
    trips = enabled_trips(cfg)
    trip = next((t for t in trips if t["key"] == trip_key), trips[0]) if trip_key else trips[0]
    if trip_key and trip["key"] != trip_key:
        raise SystemExit(f"--trip {trip_key} is not an enabled trip. Enabled: "
                         + ", ".join(t["key"] for t in trips))
    code = cabin or "Y"
    c = trip["cabins"].get(code)
    if c is None or not c["enabled"]:
        raise SystemExit(f"--cabin {code} is not enabled for {trip['key']}")
    legs = store.latest_legs(cfg["alerting"]["max_data_age_hours"] * 24)
    rows = rank_all(cfg, legs)[trip["key"]][code]
    cal = calendar_data(cfg, trip, c, rows, legs)
    best = {r[0]: r for r in cal[direction]}
    oneway = {r[0] for r in cal["legs_" + direction]}
    chunks = horizon_chunks(cfg)
    lo, hi = chunks[0][0], chunks[-1][1]

    what = "departure" if direction == "out" else "return"
    print(f"\n{trip['label'].upper()} {c['label'].upper()}  cheapest viable round trip by {what} day, "
          f"thousands of miles per person, party of {cfg['party_size']}")
    print(f"? seats not confirmed   . one-way leg only, nothing pairs   blank outside {lo} to {hi}\n")
    today = datetime.now(timezone.utc).date().replace(day=1)
    month = today
    for _ in range(12):
        nxt = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        print(f"{month.strftime('%B %Y'):20}  Mo   Tu   We   Th   Fr   Sa   Su")
        line = " " * 22 + "     " * month.weekday()
        d = month
        while d < nxt:
            iso = d.isoformat()
            if iso < lo or iso > hi:
                cell = "  "
            elif iso in best:
                cell = f"{best[iso][1] // 1000:>2}{'?' if best[iso][2] != 'confirmed' else ' '}"
            elif iso in oneway:
                cell = " ."
            else:
                cell = "  "
            line += f"{cell:>4} "
            if d.weekday() == 6:
                print(line)
                line = " " * 22
            d += timedelta(days=1)
        if line.strip():
            print(line)
        print()
        month = nxt


def show_stats(cfg: dict, store: Store) -> None:
    rows = store.conn.execute(
        """SELECT cabin, source, origin, destination, COUNT(*) AS n,
                  MIN(miles) AS lo, CAST(AVG(miles) AS INT) AS avg, MAX(miles) AS hi,
                  MIN(observed_at) AS first, MAX(observed_at) AS last
           FROM observations GROUP BY cabin, source, origin, destination"""
    ).fetchall()
    if not rows:
        print("No observations yet.")
        return
    d2t = cfg["dest_to_trip"]

    def trip_of(r) -> str:
        return d2t.get(r["destination"]) or d2t.get(r["origin"]) or "other"

    print(f"\nObservation history, one-way legs per person\n")
    print(f"{'cab':4} {'source':14} {'route':9} {'ticks':>7} {'min':>9} {'avg':>9} "
          f"{'max':>9}  {'first seen':10}  last seen")
    print("-" * 92)
    current = None
    for r in sorted(rows, key=lambda r: (trip_of(r), _cabin_rank(r["cabin"]), r["lo"])):
        head = (trip_of(r), r["cabin"])
        if head != current:
            current = head
            label = cfg["trips"].get(head[0], {}).get("label", head[0])
            print(f"{label}, {CABIN_NAMES.get(head[1], head[1])}")
        print(f"{r['cabin']:4} {r['source']:14} {r['origin']}-{r['destination']:5} "
              f"{r['n']:>7} {r['lo']:>9,} {r['avg']:>9,} {r['hi']:>9,}  "
              f"{r['first'][:10]}  {r['last'][:10]}")
    print()


def budget_plan(cfg: dict, store: Store) -> dict:
    """Calls per day in either mode, multiplied by the pagination measured on
    the last sweep. Nothing here makes a live call. A window is one trip and
    one date range, two directions.

    loop       one process, sweeps every sweep_interval_hours and focus polls
               every focus_interval_minutes in between
    scheduled  GitHub Actions runs --sweep sweeps_per_day times and --once
               polls_per_day times, mirroring the crons in .github/workflows
    """
    chunks = horizon_chunks(cfg)
    scan = cfg["scan"]
    cap = cfg["api"]["daily_call_budget"] - cfg["api"]["budget_safety_margin"]
    stats = store.get_state("sweep_stats") or {}
    measured = stats.get("calls_per_window")
    per_window = measured if measured else 2.0
    trips = len(enabled_trips(cfg))
    if scan["mode"] == "scheduled":
        sweeps = float(scan["sweeps_per_day"])
        focus_polls = float(scan["polls_per_day"])
    else:
        sweeps = 24 / scan["sweep_interval_hours"]
        focus_polls = (24 * 60) / scan["focus_interval_minutes"] - sweeps
    windows = len(chunks) * trips
    per_sweep = windows * per_window
    per_poll = scan["max_focus_windows"] * per_window
    sweep_calls = per_sweep * sweeps
    focus_calls = per_poll * focus_polls
    return {
        "mode": scan["mode"], "windows": windows, "chunks": len(chunks), "trips": trips,
        "sweeps": sweeps, "focus_polls": focus_polls, "cap": cap, "measured": measured,
        "per_window": per_window, "per_sweep": per_sweep, "per_poll": per_poll,
        "sweep_calls": sweep_calls, "focus_calls": focus_calls,
        "planned": sweep_calls + focus_calls, "stats": stats,
    }


def show_budget(cfg: dict, store: Store) -> None:
    p = budget_plan(cfg, store)
    h, scan, api = cfg["horizon"], cfg["scan"], cfg["api"]
    if p["mode"] == "scheduled":
        print(f"\nMode        scheduled, GitHub Actions runs --once {p['focus_polls']:.0f} "
              f"times and --sweep {p['sweeps']:.0f} times a day")
    else:
        print(f"\nMode        loop, one process sweeping every {scan['sweep_interval_hours']}h "
              f"and polling every {scan['focus_interval_minutes']}min")
    print(f"Horizon     {h['min_days_out']} to {h['max_days_out']} days out, "
          f"{p['chunks']} date ranges of {h['chunk_days']} days x {p['trips']} trips "
          f"= {p['windows']} windows")
    for trip in enabled_trips(cfg):
        codes, sources = query_plan(trip)
        print(f"  {trip['key']:11} {'/'.join(trip['destinations']):16} cabins {''.join(codes)}, "
              f"{len(sources)} sources")
    print(f"Per call    {api['page_size']} rows per page, up to {api['max_pages_per_query']} pages")
    if p["measured"]:
        stats = p["stats"]
        capped = stats.get("capped_windows", 0)
        print(f"Pagination  measured {p['per_window']:.2f} calls per window over "
              f"{stats['windows']} windows on {stats['at'][:16].replace('T', ' ')} UTC"
              + (f", {capped} hit the page cap" if capped else ""))
    else:
        print("Pagination  not measured yet, assuming one page per direction "
              "(2 calls per window). Run --sweep to measure.")
    print(f"Per sweep   {p['windows']} windows x {p['per_window']:.2f} calls = "
          f"{p['per_sweep']:.0f} calls")
    print(f"Per poll    {scan['max_focus_windows']} focus windows x {p['per_window']:.2f} calls = "
          f"{p['per_poll']:.0f} calls")
    print(f"Sweeps      {p['sweeps']:.0f}/day x {p['per_sweep']:.0f} = {p['sweep_calls']:.0f} calls")
    print(f"Polls       {p['focus_polls']:.0f}/day x {p['per_poll']:.0f} = {p['focus_calls']:.0f} calls")
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
        fix = ("Raise horizon.chunk_days, lower scan.max_focus_windows or disable a trip"
               if p["mode"] == "scheduled" else
               "Raise horizon.chunk_days, scan.sweep_interval_hours or scan.focus_interval_minutes")
        print(f"\nOVER BUDGET. {fix}, then run --budget again.")
    elif p["measured"] and p["per_window"] > 2.5:
        print("\nPagination is eating the margin. Lower horizon.chunk_days so each "
              "query covers fewer dates, or poll less often.")
    print()


# ---------------------------------------------------------------------------
# Dashboard export
# ---------------------------------------------------------------------------


def compact_history(rows: list, now: datetime) -> list[dict]:
    """Every snapshot from the last two days, then one point per day, the
    day's cheapest, further back. Enough for a sparkline without pushing a
    megabyte to the state branch fifty times a day."""
    cutoff = (now - timedelta(days=DASHBOARD_HISTORY_FULL_DAYS)).isoformat()
    by_day: dict[str, dict] = {}
    out: list[dict] = []
    for r in rows:
        point = {"at": r["at"], "best": r["best_miles"], "viable": r["viable"]}
        if r["at"] >= cutoff:
            out.append(point)
            continue
        day = r["at"][:10]
        cur = by_day.get(day)
        if cur is None or (point["best"] is not None and (cur["best"] is None or point["best"] < cur["best"])):
            by_day[day] = point
    return [by_day[d] for d in sorted(by_day)] + out


def calendar_data(cfg: dict, trip: dict, c: dict, rows: list[tuple[RoundTrip, str]],
                  legs: list[Leg]) -> dict:
    """Per day, the cheapest viable round trip that departs that day and the
    cheapest that returns that day, plus the cheapest one-way leg per day in
    each direction. rows is ascending, so the first pairing seen for a date
    is that date's cheapest. Lists of lists to keep the file small."""
    out_best: dict[str, list] = {}
    back_best: dict[str, list] = {}
    for rt, _ in rows:
        status = seat_status(rt, c["min_seats"])[0]
        d = rt.outbound.depart_date
        if d not in out_best:
            out_best[d] = [d, rt.total_miles, status, rt.inbound.depart_date, rt.outbound.source]
        d = rt.inbound.depart_date
        if d not in back_best:
            back_best[d] = [d, rt.total_miles, status, rt.outbound.depart_date, rt.outbound.source]
    legs_out: dict[str, list] = {}
    legs_back: dict[str, list] = {}
    origins, dests = set(cfg["origins"]), set(trip["destinations"])
    for l in legs:
        if l.cabin != c["code"] or l.source not in c["sources"]:
            continue
        if l.origin in origins and l.destination in dests:
            target = legs_out
        elif l.origin in dests and l.destination in origins:
            target = legs_back
        else:
            continue
        cur = target.get(l.depart_date)
        if cur is None or l.miles < cur[1]:
            target[l.depart_date] = [l.depart_date, l.miles, l.seats, l.source]
    return {"out": sorted(out_best.values()), "back": sorted(back_best.values()),
            "legs_out": sorted(legs_out.values()), "legs_back": sorted(legs_back.values())}


def dashboard_data(cfg: dict, store: Store) -> dict:
    """Everything the dashboard shows, as plain JSON. Same ranking as --best."""
    party, alert_cfg = cfg["party_size"], cfg["alerting"]
    age_hours = alert_cfg["max_data_age_hours"] * 24
    legs = store.latest_legs(age_hours)
    ranked = rank_all(cfg, legs)
    plan = budget_plan(cfg, store)
    now = datetime.now(timezone.utc)

    trips_out = []
    for trip in enabled_trips(cfg):
        cabins_out = []
        for c in enabled_cabins(trip):
            rows = ranked[trip["key"]][c["code"]]
            legs_seen = sum(1 for l in legs if l.cabin == c["code"] and
                            (l.destination in trip["destinations"] or l.origin in trip["destinations"]))
            typical = store.typical_best(trip["key"], c["code"])
            history = compact_history(
                store.snapshot_history(trip["key"], c["code"], DASHBOARD_HISTORY_DAYS), now)
            rows_out = []
            for rt, note in rows[:DASHBOARD_ROWS]:
                status, _ = seat_status(rt, c["min_seats"])
                rows_out.append({
                    "out": rt.outbound.depart_date, "back": rt.inbound.depart_date,
                    "nights": rt.nights, "route": rt.route, "source": rt.outbound.source,
                    "airlines": rt.outbound.airlines, "cabins": rt.cabin_code,
                    "miles": rt.total_miles, "party": rt.total_miles * party,
                    "taxes": rt.total_taxes, "seats": rt.min_seats, "seat_status": status,
                    "vs_bench": (c["benchmark_miles"] - rt.total_miles) if c["benchmark_miles"] else None,
                    "nonstop": rt.outbound.direct and rt.inbound.direct,
                })
            cabins_out.append({
                "code": c["code"], "label": c["label"],
                "benchmark": c["benchmark_miles"], "floor": c["floor_miles"],
                "ceiling": c["ceiling_miles"], "min_seats": c["min_seats"],
                "max_taxes": c["max_total_taxes_usd"],
                "observe_only": c["floor_miles"] is None,
                "viable": len(rows), "legs_seen": legs_seen,
                "unconfirmed": sum(1 for r in rows_out if r["seat_status"] != "confirmed"),
                "unconfirmed_total": sum(1 for rt, _ in rows
                                         if seat_status(rt, c["min_seats"])[0] != "confirmed"),
                "best": rows[0][0].total_miles if rows else None,
                "typical": typical, "rows": rows_out, "history": history,
                "calendar": calendar_data(cfg, trip, c, rows, legs),
            })
        trips_out.append({
            "key": trip["key"], "label": trip["label"],
            "destinations": trip["destinations"],
            "nights": [trip["min_trip_nights"], trip["max_trip_nights"]],
            "cabins": cabins_out,
        })

    last_sweep = store.get_state("last_sweep")
    chunks = horizon_chunks(cfg)
    return {
        "generated_at": now.isoformat(),
        "horizon": {"from": chunks[0][0], "to": chunks[-1][1]},
        "party_size": party, "origins": cfg["origins"],
        "data_age_days": age_hours // 24,
        "last_sweep": last_sweep,
        "fresh_legs": len(legs),
        "budget": {
            "mode": plan["mode"], "planned": round(plan["planned"]), "cap": plan["cap"],
            "daily_cap": cfg["api"]["daily_call_budget"],
            "used_today": store.calls_today(), "quota_remaining": store.quota_remaining_today(),
            "calls_per_window": plan["per_window"], "measured": bool(plan["measured"]),
        },
        "overview": overview_rows(cfg, store, ranked),
        "trips": trips_out,
    }


def export_dashboard(cfg: dict, store: Store, path: str) -> None:
    data = dashboard_data(cfg, store)
    with open(path, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    LOG.info("Dashboard data written to %s, %d trips", path, len(data["trips"]))


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


@dataclass
class Calibration:
    trip: str
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


def percentile(values: list[int] | list[float], p: float) -> float:
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


def calibrate(cfg: dict, store: Store) -> dict[tuple[str, str], Calibration]:
    """Percentiles of viable round trip totals per trip and cabin over the
    whole retained history, latest price per leg. Refuses on thin history."""
    legs = store.history_legs()
    ranked = rank_all(cfg, legs)
    span = store.history_span()
    out: dict[tuple[str, str], Calibration] = {}
    for trip in enabled_trips(cfg):
        for c in enabled_cabins(trip):
            code = c["code"]
            cal = Calibration(trip=trip["key"], cabin=code)
            totals = [rt.total_miles for rt, _ in ranked[trip["key"]][code]]
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
            out[(trip["key"], code)] = cal
    return out


def show_calibrate(cfg: dict, store: Store) -> None:
    results = calibrate(cfg, store)
    pad = max(len(c["label"]) for t in enabled_trips(cfg) for c in enabled_cabins(t)) + 2
    print("\nCALIBRATE  thresholds proposed from stored round trip totals")
    print(f"Viable combinations only (seats for the party, taxes under the cabin cap), "
          f"latest price per leg, whole retained history.\n")
    proposals: dict[str, list[tuple[dict, Calibration]]] = {}
    for trip in enabled_trips(cfg):
        print(f"=== {trip['label'].upper()}")
        for c in enabled_cabins(trip):
            cal = results[(trip["key"], c["code"])]
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
                proposals.setdefault(trip["key"], []).append((c, cal))
        print()

    if not proposals:
        print("Nothing to propose yet. Keep the schedule running and try again later.")
        return
    print("Paste into config.yaml under trips, then run --self-test and --budget.\n")
    print("trips:")
    for key, items in proposals.items():
        print(f"  {key}:")
        print(f"    cabins:")
        for c, cal in items:
            print(f"      {c['code']}:")
            print(f"        floor_miles: {cal.floor:<12}# p10 {cal.p10:,.0f}")
            print(f"        ceiling_miles: {cal.ceiling:<10}# median {cal.median:,.0f}")
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
party_size: 4
origins: [JFK, EWR]
pairing: {require_same_source: true, allow_open_jaw: false, allow_mixed_cabin: false}
cabins:
  Y: {sources: [delta, qantas], max_total_taxes_usd: 400, pushover_priority: 1, new_best_margin_miles: 3000}
  W: {sources: [delta, qantas], max_total_taxes_usd: 500, new_best_margin_miles: 8000}
  J: {sources: [delta, qantas], max_total_taxes_usd: 800, new_best_margin_miles: 20000}
  F: {sources: [qantas], max_total_taxes_usd: 1200, new_best_margin_miles: 40000, min_seats: 2}
trips:
  australia:
    label: Australia
    destinations: [SYD, MEL]
    min_trip_nights: 10
    max_trip_nights: 35
    cabins:
      Y: {benchmark_miles: 66200, floor_miles: 60000, ceiling_miles: 72000}
      J: {floor_miles: 250000, ceiling_miles: 320000}
  mexico:
    label: Mexico
    destinations: [MEX, SJD]
    min_trip_nights: 5
    max_trip_nights: 14
    cabins:
      Y: {floor_miles: 20000, ceiling_miles: 30000, max_total_taxes_usd: 250}
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
    """Stands in for requests.Session. Records every call it receives. With
    repeat_last the final page answers every call after the list runs out."""
    def __init__(self, pages: list[_FakeResponse], repeat_last: bool = False):
        self.pages, self.calls, self.repeat_last = list(pages), [], repeat_last

    def get(self, url: str, params: dict | None = None, timeout: float = 0) -> _FakeResponse:
        self.calls.append((url, dict(params or {})))
        if len(self.pages) == 1 and self.repeat_last:
            return self.pages[0]
        return self.pages.pop(0)


def _expect_fail(fn, needle: str) -> None:
    try:
        fn()
    except SystemExit as exc:
        assert needle in str(exc), f"expected '{needle}' in: {exc}"
        return
    raise AssertionError(f"expected failure mentioning '{needle}'")


def _quiet(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


def self_test() -> None:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    cfg = validate_config(yaml.safe_load(SELF_TEST_CONFIG), source="self-test config")
    store = Store(":memory:")

    # -- config -------------------------------------------------------------
    au, mx = cfg["trips"]["australia"], cfg["trips"]["mexico"]
    assert list(au["cabins"]) == ["Y", "W", "J", "F"] and list(mx["cabins"]) == ["Y", "W", "J", "F"]
    assert au["cabins"]["Y"]["floor_miles"] == 60000 and mx["cabins"]["Y"]["floor_miles"] == 20000
    assert mx["cabins"]["Y"]["max_total_taxes_usd"] == 250, "trip override wins"
    assert au["cabins"]["Y"]["max_total_taxes_usd"] == 400, "global default holds"
    assert au["cabins"]["W"]["floor_miles"] is None and mx["cabins"]["J"]["floor_miles"] is None
    assert au["cabins"]["F"]["min_seats"] == 2 and au["cabins"]["W"]["min_seats"] == 4
    assert cfg["dest_to_trip"] == {"SYD": "australia", "MEL": "australia", "MEX": "mexico", "SJD": "mexico"}
    assert query_plan(au) == (["Y", "W", "J", "F"], ["delta", "qantas"])

    def broken(mutate):
        raw = yaml.safe_load(SELF_TEST_CONFIG)
        mutate(raw)
        return lambda: validate_config(raw, source="cfg")

    _expect_fail(broken(lambda r: r["cabins"].update({"P": {"sources": ["delta"]}})), "unknown cabin")
    _expect_fail(broken(lambda r: r["cabins"]["Y"].update({"floor_miles": 1})), "Prices live per trip")
    _expect_fail(broken(lambda r: r["trips"]["australia"]["cabins"]["Y"].update({"floor_miles": 80000})), "above ceiling")
    _expect_fail(broken(lambda r: r["trips"]["australia"]["cabins"]["J"].update({"ceiling_miles": None})), "both")
    _expect_fail(broken(lambda r: r.update({"trip": {"party_size": 4}})), "predates trips")
    _expect_fail(broken(lambda r: r["trips"]["mexico"]["destinations"].append("SYD")), "already belongs")
    _expect_fail(broken(lambda r: r["trips"]["mexico"]["destinations"].append("JFK")), "also an origin")
    _expect_fail(broken(lambda r: r["cabins"]["F"].update({"min_seats": 9})), "above party_size")
    _expect_fail(broken(lambda r: r["scan"].update({"mode": "cron"})), "scan.mode")
    print("config ok: trips resolve cabin defaults, nine bad configs rejected loudly")

    # -- horizon ------------------------------------------------------------
    chunks = horizon_chunks(cfg, today=date(2026, 8, 15))
    assert chunks[0][0] == "2026-09-29", chunks[0]
    assert chunks[-1][1] == "2027-07-12", chunks[-1]
    print(f"horizon ok: {len(chunks)} date ranges, {chunks[0][0]} to {chunks[-1][1]}")

    # -- parsing ------------------------------------------------------------
    def av(oid, source, o, d, day, **cabins):
        """An Availability object in the exact shape the live API returned on
        11 Sep 2026. Every cabin field has a *Raw twin and a Direct variant,
        MileageCost is a string, UpdatedAt is the freshness stamp."""
        obj = {
            "ID": oid, "RouteID": f"r-{oid}", "Source": source, "Date": day,
            "ParsedDate": f"{day}T00:00:00Z", "TaxesCurrency": "USD",
            "Route": {"ID": f"r-{oid}", "OriginAirport": o, "OriginRegion": "",
                      "DestinationAirport": d, "DestinationRegion": "",
                      "Distance": 9950, "Source": source},
            "CreatedAt": "2025-10-31T21:02:21.568283Z", "UpdatedAt": now,
            "AvailabilityTrips": [],
        }
        for code in CABIN_ORDER:
            miles, seats, taxes = cabins.get(code, (0, 0, 0))
            airline = ("DL" if source == "delta" else "QF") if miles else ""
            for prefix in ("", "Direct"):
                for suffix in ("", "Raw"):
                    k = f"{code}{prefix}"
                    obj[f"{k}Available{suffix}"] = bool(miles) and not prefix
                    obj[f"{k}MileageCost{suffix}"] = (str(miles) if not suffix else miles) if not prefix else 0
                    obj[f"{k}RemainingSeats{suffix}"] = seats if not prefix else 0
                    obj[f"{k}TotalTaxes{suffix}"] = taxes if not prefix else 0
                    obj[f"{k}Airlines{suffix}"] = airline if not prefix else ""
                    obj[f"{k}{suffix}"] = False
        return obj

    sample = av("p1", "delta", "JFK", "SYD", "2027-02-10",
                Y=(33100, 6, 6625), W=(60000, 3, 8000), J=(140000, 4, 20000))
    sample["JDirect"] = True
    legs = parse_availability(sample, CABIN_ORDER)
    assert [l.cabin for l in legs] == ["Y", "W", "J"], legs
    assert legs[0].miles == 33100 and legs[0].taxes_usd == 66.25 and legs[0].taxes_currency == "USD"
    assert legs[0].last_seen == now and legs[0].airlines == "DL"
    assert legs[2].direct is True and legs[1].direct is False
    assert parse_availability(dict(sample, YAvailable=False), ["Y"]) == []
    assert parse_availability(dict(sample, YMileageCost="0"), ["Y"]) == []
    live = {"ID": "x", "Route": {"OriginAirport": "BOS", "DestinationAirport": "SYD", "Source": "qantas"},
            "Date": "2026-10-26", "YAvailable": True, "YMileageCost": "69900",
            "YRemainingSeats": 1, "YTotalTaxes": 40860, "YAirlines": "EK", "YDirect": False,
            "TaxesCurrency": "USD", "Source": "qantas", "UpdatedAt": "2026-09-10T18:45:54.293483Z"}
    leg = parse_availability(live, CABIN_ORDER)
    assert len(leg) == 1 and leg[0].miles == 69900 and leg[0].taxes_usd == 408.60, leg
    assert leg[0].seats == 1 and leg[0].last_seen.startswith("2026-09-10")
    print("parse ok: live response shape, string mileage, taxes in minor units, UpdatedAt")

    # -- fixtures, two trips ------------------------------------------------
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
        av("m1", "delta", "JFK", "MEX", "2027-01-10", Y=(9000, 0, 3000)),      # mexico, no seat count
        av("m2", "delta", "MEX", "JFK", "2027-01-17", Y=(9000, 0, 3000)),      # 7 nights
        av("m3", "delta", "JFK", "SJD", "2027-02-01", Y=(12500, 4, 3000)),
        av("m4", "delta", "SJD", "JFK", "2027-02-09", Y=(12500, 4, 3000)),     # 8 nights, confirmed
        av("m5", "delta", "MEX", "JFK", "2027-03-01", Y=(9000, 4, 3000)),      # 50 nights from m1, too long
    ]
    legs = [leg for obj in raw for leg in parse_availability(obj, CABIN_ORDER)]
    assert len(legs) == len(raw), len(legs)
    store.record_legs(legs)

    # -- pairing per trip ---------------------------------------------------
    fresh = store.latest_legs(48)
    au_pairs = build_round_trips(fresh, cfg["origins"], au["destinations"], 10, 35, cfg["pairing"], "australia")
    assert all(not p.mixed and p.trip == "australia" for p in au_pairs)
    assert all(p.outbound.destination in ("SYD", "MEL") for p in au_pairs), "trip must not see other cities"
    totals = sorted({p.total_miles for p in au_pairs})
    assert 54000 in totals and 285000 in totals and 140000 in totals, totals
    mixed_pairing = dict(cfg["pairing"], allow_mixed_cabin=True)
    mixed = [p for p in build_round_trips(fresh, cfg["origins"], au["destinations"], 10, 35, mixed_pairing) if p.mixed]
    assert mixed and all(p.cabin == "Y" for p in mixed if "Y" in p.cabin_code), "mixed ranks under lower cabin"
    brute = [RoundTrip(o, b) for o in fresh if o.origin in ("JFK", "EWR") and o.destination in ("SYD", "MEL")
             for b in fresh if b.origin == o.destination and b.destination in ("JFK", "EWR")
             and b.source == o.source and b.cabin == o.cabin and 10 <= RoundTrip(o, b).nights <= 35]
    assert sorted(p.key() for p in au_pairs) == sorted(p.key() for p in brute), "bucketed pairing must equal brute force"
    print(f"pairing ok: {len(au_pairs)} same-cabin combos for Australia, {len(mixed)} mixed rejected by default")

    # -- ranking and seat status --------------------------------------------
    ranked = rank_all(cfg, fresh)
    assert set(ranked) == {"australia", "mexico"}
    assert [rt.total_miles for rt, _ in ranked["australia"]["Y"]] == [54000, 66200]
    assert ranked["australia"]["W"][0][0].total_miles == 140000
    assert ranked["australia"]["J"][0][0].total_miles == 285000
    assert [rt.outbound.source for rt, _ in ranked["australia"]["F"]] == ["qantas"], "delta F filtered by sources"
    assert ranked["australia"]["F"][0][0].min_seats == 2, "F min_seats override"
    mx_rows = ranked["mexico"]["Y"]
    assert [rt.total_miles for rt, _ in mx_rows] == [18000, 25000], mx_rows
    assert seat_status(mx_rows[0][0], 4) == ("unpublished", "seats unpublished, verify before booking")
    assert seat_status(mx_rows[1][0], 4) == ("confirmed", "4 seats confirmed")
    assert seat_status(RoundTrip(legs[0], legs[4]), 4)[0] == "short"
    strict = copy.deepcopy(cfg)
    strict["alerting"]["require_seat_count"] = True
    assert [rt.total_miles for rt, _ in rank_all(strict, fresh)["mexico"]["Y"]] == [25000], \
        "require_seat_count drops unpublished rows"
    print("ranking ok: per trip, Y needs 4 seats, F accepts 2, unpublished counts are flagged not trusted")

    # -- alert gating -------------------------------------------------------
    y_best, j_best, w_best = (ranked["australia"][c][0][0] for c in ("Y", "J", "W"))
    assert alert_reason(y_best, None, au["cabins"]["Y"]) == "under floor"
    assert alert_reason(ranked["australia"]["Y"][1][0], 54000, au["cabins"]["Y"]) is None
    over = dict(au["cabins"]["Y"], ceiling_miles=65000)
    assert alert_reason(RoundTrip(legs[0], legs[2]), None, over) is None, "above ceiling"
    assert alert_reason(w_best, None, au["cabins"]["W"]) is None, "null thresholds never alert"
    assert alert_reason(j_best, None, au["cabins"]["J"]) == "first qualifying combination"
    same_y = RoundTrip(Leg(**dict(asdict(legs[5]), cabin="Y")), Leg(**dict(asdict(legs[6]), cabin="Y")))
    assert RoundTrip(legs[5], legs[6]).key() != same_y.key(), "dedupe key must include cabin"
    title, body = format_alert(mx_rows[0][0], "seats unpublished, verify before booking",
                               "under floor", 4, mx, mx["cabins"]["Y"])
    assert title == "18k MEXICO ECONOMY DELTA JFK-MEX, seats unconfirmed", title
    assert "NOT confirmed" in body and FARE_BRAND_REMINDER in body
    title, _ = format_alert(mx_rows[1][0], "4 seats confirmed", "under floor", 4, mx, mx["cabins"]["Y"])
    assert title == "25k MEXICO ECONOMY DELTA JFK-SJD", title
    print("alert gating ok: floor fires, ceiling suppresses, null thresholds silent, unconfirmed seats named in the title")

    # -- evaluate, per trip and cabin running best --------------------------
    out = _quiet(evaluate, cfg, store, True)
    assert "54k AUSTRALIA ECONOMY DELTA JFK-SYD" in out, out
    assert "285k AUSTRALIA BUSINESS DELTA JFK-SYD" in out, "cheap economy suppressed the business alert"
    assert "18k MEXICO ECONOMY DELTA JFK-MEX, seats unconfirmed\n" in out, out
    assert "25k MEXICO" not in out, "25,000 is above the floor and not a new best"
    assert "PREMIUM ECONOMY" not in out and "FIRST" not in out, "observe-only cabins alerted"
    assert out.count("WOULD ALERT") == 3, out
    assert "vs economy benchmark 66,200: saves 12,200" in out
    assert store.get_state("best_total_miles:australia:Y") == 54000
    assert store.get_state("best_total_miles:australia:J") == 285000
    assert store.get_state("best_total_miles:australia:W") == 140000, "observe-only cabins still track a best"
    assert store.get_state("best_total_miles:mexico:Y") == 18000
    focus = store.get_state("focus_windows")
    assert focus == [{"trip": "australia", "date": "2027-03-14"}, {"trip": "mexico", "date": "2027-01-10"},
                     {"trip": "australia", "date": "2027-06-01"}], focus
    snaps = store.conn.execute("SELECT trip, cabin, best_miles, viable, unconfirmed FROM snapshots ORDER BY trip, cabin").fetchall()
    assert [tuple(r) for r in snaps] == [
        ("australia", "F", 400000, 1, 0), ("australia", "J", 285000, 1, 0),
        ("australia", "W", 140000, 1, 0), ("australia", "Y", 54000, 2, 0),
        ("mexico", "F", None, 0, 0), ("mexico", "J", None, 0, 0),
        ("mexico", "W", None, 0, 0), ("mexico", "Y", 18000, 2, 1)], [tuple(r) for r in snaps]
    assert "WOULD ALERT" not in _quiet(evaluate, cfg, store, True), "alerts repeated on the second pass"
    print(f"evaluate ok: three trips and cabins alert independently, snapshots recorded, second pass silent")

    # -- a burst of qualifying pairings is one message ------------------------
    burst = Store(":memory:")
    burst_raw = [av("b0", "delta", "JFK", "SYD", "2027-03-01", Y=(27000, 6, 6600))]
    for i in range(12):
        day = (date(2027, 3, 13) + timedelta(days=i)).isoformat()
        burst_raw.append(av(f"b{i + 1}", "delta", "SYD", "JFK", day, Y=(27000, 4, 6600)))
    burst.record_legs([leg for obj in burst_raw for leg in parse_availability(obj, CABIN_ORDER)])
    out = _quiet(evaluate, cfg, burst, True)
    assert out.count("WOULD ALERT") == 1, out
    assert "54k AUSTRALIA ECONOMY DELTA JFK-SYD +11 more" in out, out
    assert out.count("2027-03-01 > ") == DIGEST_ROWS and "and 5 more, run --best --trip australia --cabin Y" in out, out
    assert out.strip().endswith(FARE_BRAND_REMINDER), "reminder must stay last"
    assert len(out.split("+11 more\n", 1)[1]) <= 1024, "Pushover cap"
    assert burst.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 12
    assert "WOULD ALERT" not in _quiet(evaluate, cfg, burst, True)
    print("digest ok: 12 under-floor pairings are one message, all 12 deduped after")

    # -- leaderboard and overview rendering ---------------------------------
    out = _quiet(show_best, cfg, store)
    assert "WHERE TO GO RIGHT NOW" in out and "=== AUSTRALIA  SYD/MEL, 10 to 35 nights" in out
    assert "=== MEXICO  MEX/SJD, 5 to 14 nights" in out
    for label in ("ECONOMY  benchmark 66,200", "PREMIUM ECONOMY  no benchmark, observe-only",
                  "BUSINESS  no benchmark", "FIRST  no benchmark, observe-only"):
        assert label in out, f"missing section {label!r}"
    assert "+12,200" in out and "216,000" in out and "1,140,000" in out
    assert "1 with no published seat count (shown as ?)" in out, out
    assert out.index("=== AUSTRALIA") < out.index("=== MEXICO"), "trip sections keep config order"
    ov = out.split("WHERE TO GO", 1)[1].split("===", 1)[0]
    assert ov.index("Mexico") < ov.index("Australia") and "18,000 ?" in ov and "54,000" in ov, ov
    assert "Cheapest by cabin  economy Mexico at 18,000, premium economy Australia at 140,000" in ov, ov
    only_f = _quiet(show_best, cfg, store, 15, "australia", "F")
    assert "FIRST" in only_f and "ECONOMY" not in only_f and "MEXICO" not in only_f
    assert _quiet(show_best, cfg, Store(":memory:")).count("no availability recorded") == 8
    os.environ["GITHUB_ACTIONS"] = "true"
    try:
        lines = _quiet(show_best, cfg, store).strip().splitlines()
    finally:
        del os.environ["GITHUB_ACTIONS"]
    assert lines[0] == "```" and lines[-1] == "```"
    print("leaderboard ok: overview first, trips in order, --trip and --cabin filters, fenced under Actions")

    # -- dashboard export ---------------------------------------------------
    data = dashboard_data(cfg, store)
    assert [t["key"] for t in data["trips"]] == ["australia", "mexico"]
    au_y = data["trips"][0]["cabins"][0]
    assert au_y["code"] == "Y" and au_y["best"] == 54000 and au_y["rows"][0]["seat_status"] == "confirmed"
    assert au_y["rows"][0]["party"] == 216000 and au_y["rows"][0]["vs_bench"] == 12200
    mx_y = data["trips"][1]["cabins"][0]
    assert mx_y["rows"][0]["seat_status"] == "unpublished" and mx_y["unconfirmed"] == 1
    assert len(au_y["history"]) == 2 and au_y["history"][0]["best"] == 54000
    old_rows = [{"at": (now_dt - timedelta(days=5, hours=h)).isoformat(), "best_miles": 70000 - h * 100, "viable": 3}
                for h in range(6)] + [{"at": now, "best_miles": 54000, "viable": 2}]
    compact = compact_history(old_rows, now_dt)
    assert len(compact) == 2 and compact[0]["best"] == 69500 and compact[1]["best"] == 54000, compact
    assert data["overview"][0]["key"] == "mexico" and data["budget"]["cap"] == 900
    cal = au_y["calendar"]
    assert cal["out"] == [["2027-02-10", 66200, "confirmed", "2027-03-04", "delta"],
                          ["2027-03-14", 54000, "confirmed", "2027-04-05", "delta"]], cal["out"]
    assert cal["back"][0] == ["2027-03-04", 66200, "confirmed", "2027-02-10", "delta"]
    assert ["2027-03-16", 20000, 2, "delta"] in cal["legs_back"], "one-way legs include the 2 seat return"
    assert all(r[0].startswith("2027-0") for r in cal["legs_out"]) and len(cal["legs_out"]) == 2
    mx_cal = mx_y["calendar"]
    assert mx_cal["out"][0] == ["2027-01-10", 18000, "unpublished", "2027-01-17", "delta"]
    assert data["horizon"]["from"] < data["horizon"]["to"]
    json.dumps(data)
    print("export ok: dashboard data carries trips, seat status, history, calendar and the overview")
    out = _quiet(show_calendar, cfg, store, "australia", "Y")
    assert "AUSTRALIA ECONOMY  cheapest viable round trip by departure day" in out
    assert "March 2027" in out and "Mo   Tu" in out
    out = _quiet(show_calendar, cfg, store, "mexico", "Y", "back")
    assert "by return day" in out and "18?" in out, out
    print("calendar ok: twelve months per trip and cabin, both directions")

    # -- pagination protocol ------------------------------------------------
    client = SeatsAero(cfg, store, api_key="test-key")
    hdr = {"x-ratelimit-remaining": "990"}
    client.session = _FakeSession([
        _FakeResponse({"data": [raw[0], raw[1]], "count": 2, "hasMore": True, "cursor": 1700000000}, headers=hdr),
        _FakeResponse({"data": [raw[2]], "count": 1, "hasMore": True, "cursor": 1700000099}, headers=hdr),
        _FakeResponse({"data": [], "count": 0, "hasMore": False, "cursor": 1700000099}, headers=hdr),
    ])
    res = client.cached_search(["JFK"], ["SYD"], ["Y", "J"], ["delta"], "2027-02-01", "2027-03-03")
    assert len(res.rows) == 3 and res.calls == 3 and not res.capped
    calls = client.session.calls
    assert "cursor" not in calls[0][1] and calls[0][1]["cabins"] == "economy,business", calls[0]
    assert calls[1][1]["cursor"] == 1700000000 and calls[1][1]["skip"] == 2, calls[1]
    assert calls[2][1]["cursor"] == 1700000000 and calls[2][1]["skip"] == 3, "cursor must stay the first one"
    assert store.get_state("quota_header") == "x-ratelimit-remaining"
    assert store.quota_remaining_today() == 990 and store.calls_today() == 3
    more = ("/partnerapi/search?take=25&skip=25&origin_airport=JFK&destination_airport=SYD"
            "&cursor=1789152528&start_date=2027-02-01&end_date=2027-03-03&cabins=economy&sources=delta")
    client.session = _FakeSession([
        _FakeResponse({"data": [raw[0]], "count": 1, "hasMore": True, "cursor": 1789152528, "moreURL": more}),
        _FakeResponse({"data": [raw[1]], "count": 1, "hasMore": False, "cursor": 1789152528, "moreURL": ""}),
    ])
    res = client.cached_search(["JFK"], ["SYD"], ["Y"], ["delta"], "2027-02-01", "2027-03-03")
    assert res.calls == 2 and client.session.calls[1][0] == "https://seats.aero" + more
    assert client.session.calls[1][1] == {}, "moreURL already carries the query"
    client.session = _FakeSession([_FakeResponse({"data": [raw[0]], "hasMore": True, "cursor": 1}) for _ in range(5)])
    res = client.cached_search(["JFK"], ["SYD"], ["Y"], ["delta"], "2027-02-01", "2027-03-03")
    assert res.capped and res.calls == cfg["api"]["max_pages_per_query"]
    client.session = _FakeSession([_FakeResponse({"message": "bad cabin"}, status=400)])
    res = client.cached_search(["JFK"], ["SYD"], ["Y"], ["delta"], "2027-02-01", "2027-03-03")
    assert res.rows == [] and res.status == 400
    print("pagination ok: first cursor kept, skip accumulates, moreURL followed, page cap flagged")

    # -- budget, both modes, windows are trips x date ranges ----------------
    plan = budget_plan(cfg, store)
    assert plan["windows"] == 20 and plan["per_window"] == 2.0 and plan["mode"] == "loop"
    store.set_state("sweep_stats", {"at": now, "windows": 20, "calls": 52,
                                    "calls_per_window": 2.6, "capped_windows": 1})
    sched = copy.deepcopy(cfg)
    sched["scan"].update({"mode": "scheduled", "polls_per_day": 48, "sweeps_per_day": 4})
    plan = budget_plan(sched, store)
    close = math.isclose
    assert close(plan["per_sweep"], 52) and close(plan["per_poll"], 7.8), plan
    assert close(plan["planned"], 4 * 52 + 48 * 7.8) and plan["planned"] < 900, plan
    out = _quiet(show_budget, sched, store)
    assert "Mode        scheduled" in out and "= 20 windows" in out and "measured 2.60 calls" in out, out
    print(f"budget ok: scheduled plan is {plan['planned']:.0f} calls a day for 2 trips at measured pagination")

    # -- a tick only when something changed -----------------------------------
    tick = Store(":memory:")
    old = (now_dt - timedelta(hours=20)).isoformat()
    base = Leg("s", "delta", "JFK", "SYD", "2027-03-01", "Y", 30000, 66.0, 4, "DL", False, old, old)
    assert tick.record_legs([base]) == (1, 0)
    again = Leg(**dict(asdict(base), observed_at=now, last_seen=now))
    assert tick.record_legs([again]) == (0, 1), "same price and seats touches the latest tick"
    assert tick.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
    assert [l.observed_at for l in tick.latest_legs(12)] == [old], "confirmed just now, so still fresh"
    cheaper = Leg(**dict(asdict(base), miles=27000, observed_at=now, last_seen=now))
    assert tick.record_legs([cheaper]) == (1, 0), "a price change is a new tick"
    assert tick.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
    assert [l.miles for l in tick.latest_legs(12)] == [27000]
    fewer = Leg(**dict(asdict(cheaper), seats=1))
    assert tick.record_legs([fewer, fewer]) == (1, 0), "a seat change is a new tick, duplicates in a batch collapse"
    assert tick.record_legs([fewer], record_unchanged=True) == (1, 0), "record_unchanged restores a tick per sighting"
    assert tick.record_legs([]) == (0, 0)
    print("ticks ok: new or changed legs are rows, unchanged legs only refresh last_confirmed_at")
    # a database written a row per sighting collapses to one row per change
    legacy = Store(":memory:")
    t = [(now_dt - timedelta(hours=h)).isoformat() for h in (30, 20, 10, 5, 1)]
    for at, miles in zip(t, (30000, 30000, 30000, 27000, 27000)):
        legacy.record_legs([Leg(**dict(asdict(base), miles=miles, observed_at=at, last_seen=at))], record_unchanged=True)
    legacy.record_legs([Leg(**dict(asdict(base), origin="EWR", observed_at=t[0], last_seen=t[0]))], record_unchanged=True)
    assert legacy.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 6
    assert legacy.collapse_repeated_ticks() == 3
    left = legacy.conn.execute("SELECT origin, miles, observed_at, last_confirmed_at FROM observations ORDER BY id").fetchall()
    assert [tuple(r) for r in left] == [("JFK", 30000, t[0], t[2]), ("JFK", 27000, t[3], t[4]), ("EWR", 30000, t[0], t[0])], [tuple(r) for r in left]
    assert legacy.collapse_repeated_ticks() == 0
    print("collapse ok: a row per sighting database shrinks to a row per change with the right stamps")
    keep = Store(":memory:")
    ago = lambda d: (now_dt - timedelta(days=d)).isoformat()
    keep.record_legs([Leg("s", "delta", "JFK", "SYD", "2027-06-01", "Y", 30000, 66.0, 4, "DL", False, ago(40), ago(40))])
    keep.record_legs([Leg("s", "delta", "JFK", "SYD", "2027-06-01", "Y", 29000, 66.0, 4, "DL", False, ago(30), ago(30))])
    keep.record_legs([Leg("s", "delta", "JFK", "SYD", "2027-06-01", "Y", 28000, 66.0, 4, "DL", False, ago(20), ago(20))])
    keep.record_legs([Leg("s", "delta", "JFK", "SYD", "2026-01-01", "Y", 28000, 66.0, 4, "DL", False, now, now)])
    assert keep.prune(14, today=date(2026, 9, 13)) == 3
    left = keep.conn.execute("SELECT depart_date, miles FROM observations").fetchall()
    assert [tuple(r) for r in left] == [("2027-06-01", 28000)], left
    print("prune ok: flown dates and old change ticks go, each leg's latest tick stays whatever its age")

    # -- prune runs at the end of --sweep and --once --------------------------
    for label, runner in (("sweep", run_sweep), ("once", run_focus)):
        pst = Store(":memory:")
        stale = (now_dt - timedelta(days=500)).isoformat()
        pst.record_legs([
            Leg("s", "delta", "JFK", "SYD", "2027-03-01", "Y", 30000, 66.0, 4, "DL", False, stale, stale),
            Leg("s", "delta", "JFK", "SYD", "2027-03-02", "Y", 30000, 66.0, 4, "DL", False, now, now),
        ], record_unchanged=True)
        pst.record_legs([Leg("s", "delta", "JFK", "SYD", "2027-03-01", "Y", 28000, 66.0, 4, "DL", False, stale, stale)],
                        record_unchanged=True)
        pst.set_state("focus_windows", [{"trip": "australia", "date": "2027-03-14"},
                                        {"trip": "gone", "date": "2027-03-14"}])
        pcl = SeatsAero(cfg, pst, api_key="test-key")
        pcl.session = _FakeSession([_FakeResponse({"data": [], "hasMore": False})], repeat_last=True)
        with redirect_stdout(io.StringIO()):
            runner(cfg, pst, pcl, True)
        left = pst.conn.execute("SELECT depart_date, miles, observed_at FROM observations ORDER BY id").fetchall()
        assert [tuple(r) for r in left] == [("2027-03-02", 30000, now), ("2027-03-01", 28000, stale)], \
            f"{label} kept {[tuple(r) for r in left]}, the old 30000 tick goes, the latest 28000 tick stays"
        assert pst.calls_today() == (40 if label == "sweep" else 2), (label, pst.calls_today())
    print("prune ok: --sweep covers every trip, --once skips windows for unknown trips, both prune")

    # -- calibration --------------------------------------------------------
    cals = calibrate(cfg, store)
    assert all(c.refusal for c in cals.values()), "thin history must be refused"
    assert "200 viable combinations" in cals[("australia", "Y")].refusal
    hist = Store(":memory:")
    old = (now_dt - timedelta(days=20)).isoformat()
    for stamp in (old, now):
        synthetic = []
        for i in range(20):
            out_day = (date(2027, 3, 1) + timedelta(days=i)).isoformat()
            synthetic.append(Leg("s", "delta", "JFK", "SYD", out_day, "Y", 27000 + 700 * i,
                                 66.0, 4, "DL", False, stamp, stamp))
        for i in range(26):
            back_day = (date(2027, 3, 15) + timedelta(days=i)).isoformat()
            synthetic.append(Leg("s", "delta", "SYD", "JFK", back_day, "Y", 27000 + 500 * i,
                                 66.0, 4, "DL", False, stamp, stamp))
        hist.record_legs(synthetic)
    cal = calibrate(cfg, hist)[("australia", "Y")]
    assert cal.refusal is None, cal.refusal
    assert cal.p10 > 40300, "percentile computed over single legs, not round trip totals"
    assert cal.floor == round_to(cal.p10, 5000) and cal.ceiling == round_to(cal.median, 5000)
    assert cal.floor < cal.ceiling and 55000 <= cal.floor <= 70000, cal
    assert percentile([1, 2, 3, 4], 50) == 2.5 and percentile([10], 90) == 10.0
    assert round_to(58600, 5000) == 60000 and round_to(57400, 5000) == 55000
    out = _quiet(show_calibrate, cfg, hist)
    assert f"floor_miles: {cal.floor}" in out and "refused, need" in out and "  australia:\n    cabins:\n      Y:" in out, out
    print(f"calibrate ok: refuses thin history, proposes Australia Y floor {cal.floor:,} "
          f"ceiling {cal.ceiling:,} from {cal.combos} combos over {cal.span_days:.0f} days")

    # -- typical best from snapshots -----------------------------------------
    for i in range(16):
        hist.record_snapshot((now_dt - timedelta(days=15 - i)).isoformat(), "australia", "Y",
                             60000 + (i % 4) * 1000, 100, 0)
    assert hist.typical_best("australia", "Y") == 61500.0
    assert hist.typical_best("mexico", "Y") is None
    print("typical ok: median of three weeks of snapshots, none under fourteen days")

    print("\nAll self-tests passed.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="seats.aero open-date award monitor, several trips, four cabin views. "
                    "Add --dry-run to print alerts instead of sending them.")
    ap.add_argument("--config", default="config.yaml", help="path to config, default config.yaml")
    modes = ap.add_argument_group("modes")
    modes.add_argument("--once", action="store_true", help="one focus poll")
    modes.add_argument("--sweep", action="store_true", help="one full calendar sweep, every trip")
    modes.add_argument("--loop", action="store_true", help="scheduler, sweep then focus polls")
    modes.add_argument("--probe", action="store_true", help="one live call, dump the raw response")
    modes.add_argument("--best", action="store_true", help="overview, then leaderboards per trip and cabin")
    modes.add_argument("--overview", action="store_true", help="where to go right now, one table")
    modes.add_argument("--calendar", action="store_true", help="twelve months of days for one trip and cabin")
    modes.add_argument("--calibrate", action="store_true", help="propose thresholds from history")
    modes.add_argument("--export", metavar="FILE", help="write the dashboard data file")
    modes.add_argument("--stats", action="store_true", help="observation history summary")
    modes.add_argument("--budget", action="store_true", help="API call plan and measured usage")
    modes.add_argument("--self-test", action="store_true", help="offline test, no network")
    modes.add_argument("--test-alert", action="store_true", help="send one Pushover message")
    opts = ap.add_argument_group("options")
    opts.add_argument("--trip", help="--best for one trip key only")
    opts.add_argument("--cabin", choices=CABIN_ORDER, help="--best for one cabin only")
    opts.add_argument("--limit", type=int, default=15, help="--best rows per section, default 15")
    opts.add_argument("--returns", action="store_true", help="--calendar by return day instead of departure day")
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
        show_best(cfg, store, limit=args.limit, trip_key=args.trip, cabin=args.cabin)
        return
    if args.overview:
        show_overview(cfg, store)
        return
    if args.calendar:
        show_calendar(cfg, store, args.trip, args.cabin, "back" if args.returns else "out")
        return
    if args.calibrate:
        show_calibrate(cfg, store)
        return
    if args.export:
        export_dashboard(cfg, store, args.export)
        return
    if args.stats:
        show_stats(cfg, store)
        return
    if args.budget:
        show_budget(cfg, store)
        return
    if args.test_alert:
        print("sent" if send_pushover(cfg["pushover"], "Award monitor online",
                                      "Watching the whole calendar for every trip.", 0) else "failed")
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
            except Exception:
                LOG.exception("Cycle failed, continuing")
            time.sleep(interval)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
