# Award Monitor

Personal tool. Monitors seats.aero for award redemptions for a family of four
from the New York area to Australia, Mexico, Europe and Asia. Ranks the whole
bookable calendar by miles per trip and cabin, alerts on Pushover, feeds a
live dashboard. See @README.md for setup and operating instructions.

## The goal

Beat 66,200 miles per person round trip in economy. That is what Eric paid in
April 2026, not the 60,000 he remembers. Source is the Delta award receipt,
confirmation GYLTUY, ticket 0062378205056, issued 6 Nov 2025.

- 66,200 miles plus $132.53 per person, 264,800 miles for four
- Out Wed 25 Mar 2026, DL771 JFK-LAX, DL41 LAX-SYD
- Back Thu 16 Apr 2026, DL40 SYD-LAX, DL958 LAX-JFK
- 22 nights, booked 139 days before departure
- Fare basis ESVR331/FFX15, **Delta Main Basic (N)**

Every other trip and cabin has no benchmark and no thresholds yet. They are
scanned, stored, ranked and shown on the dashboard, and `--calibrate` proposes
thresholds once there is enough history. Nobody invents those numbers by hand.
Prices are per trip, because Mexico and Sydney are not on the same scale.

Dates are fully open. Eric picks dates off the price leaderboard, not the other
way round. Do not add fixed-date assumptions.

## Shape of the code

One file, `monitor.py`, in this order. Model, config, storage, chunking,
client, parsing, pairing and ranking, notification, scan cycles, reporting,
dashboard export, calibration, self test, main.

- `validate_config` applies defaults and fails loudly. Trips resolve their
  cabins by merging the global `cabins` defaults with the trip's overrides.
  A price on the global block, a destination in two trips, a destination
  that is also an origin, and the old `trip` section are all refused by name.
- `rank_all` is the one place pairings become per-trip, per-cabin ranked
  lists. `evaluate`, `show_best`, `calibrate` and `dashboard_data` all go
  through it so nothing can drift apart.
- `seat_status` is the one reading of a seat count. confirmed, unpublished
  or short. Unpublished rows are kept and flagged everywhere, never trusted.
  Short rows are dropped.
- `evaluate` walks each trip's each cabin's list ascending with a running
  best stored as `best_total_miles:<trip>:<cabin>`, collects every
  qualifying pairing and sends one digest through `format_digest`. The first
  live sweep sent twenty separate messages for twenty return dates at one
  price. Don't go back to one message per pairing. It also writes one
  `snapshots` row per list per pass, which is the dashboard's history.
- `choose_focus_windows` takes every trip's economy top first, then every
  trip's premium top, and so on, capped at `scan.max_focus_windows`.
- `overview_rows` is the "where to go right now" table, shared by
  `--overview`, `--best` and the dashboard. `Store.typical_best` is the
  median of three weeks of snapshots, null under fourteen days.
- `dashboard_data` and `--export` write the JSON the dashboard reads.
  `compact_history` keeps the file small.
- `budget_plan` prices a window as one trip and one date range, two
  directions, times the measured pagination.
- `scan.mode` is `loop` or `scheduled`. Scheduled mode is GitHub Actions on
  the crons in `.github/workflows`, state on the `monitor-state` branch.
  `run_sweep` and `run_focus` both call `prune_history` on the way out.
- Never commit `.env` or the database. Credentials are repository secrets.
  The workflow files are Eric's. The export step and the one line that adds
  `dashboard.json` to the state commit are the only additions. Don't rework
  the restore, save, concurrency or alert-if-down logic without asking.

## What is verified and what is not

Verified offline by `--self-test`, which runs against an in-memory database and
its own embedded two-trip config so it never touches real data. Config
resolution and nine rejections, date chunking, parsing in the live shape,
per-trip pairing and mixed cabin rejection, seat status, per-trip per-cabin
gating and running best, the alert digest, snapshots, the overview and
leaderboard rendering with filters, the dashboard export and history
compaction, the pagination protocol against a fake session, budget arithmetic
for trips and both modes, prune in both scan paths, calibration and the
typical-best median.

Verified against a live response on 11 Sep 2026 by `--probe`. Field names
`ID`, `Route.OriginAirport`, `Source`, `Date`, `UpdatedAt`, `TaxesCurrency`,
per cabin `Available`, `MileageCost` as a string, `RemainingSeats`,
`TotalTaxes` as an integer in the minor unit (40860 USD was $408.60 on a
Qantas economy seat, plausible), `Airlines` and `Direct`. Every field has a
`Raw` twin holding the pre-filter value, the parser reads the filtered set.
The pagination shape is `data`, `count`, `hasMore`, `cursor` and `moreURL`.
The quota header is `x-ratelimit-remaining` against `x-ratelimit-limit`
1000. The cabin filter is the `cabins` parameter with word values. There is
no `ComputedLastSeen`.

From the first full sweep on 11 Sep 2026, 3,381 legs over the whole horizon.
Only Qantas, Alaska and American returned rows. Delta, Virgin Atlantic,
Virgin Australia, United and Aeroplan returned nothing at all, which is
worth checking by hand on seats.aero before trusting, since the benchmark
is a Delta redemption. First class returned nothing anywhere. Alaska and
Qantas publish seat counts on every row, American on none.

## Constraints that matter

**Four seats on one booking is the binding constraint.** A cheap print usually
surfaces with one or two. Any change that makes the leaderboard look better by
loosening the seat check is a regression, not an improvement. The one permitted
override is per-cabin `min_seats`, and validation refuses a value above
`party_size`.

**Basic Economy is non-refundable after 24 hours.** The April ticket was Main
Basic. seats.aero returns a mileage price with no fare brand, so the monitor
cannot tell Basic from Main. Every alert carries a reminder to check on
delta.com before confirming. Do not remove it. It's the `FARE_BRAND_REMINDER`
constant and the self test asserts it appears.

**API budget is 1,000 calls a day, hard.** Run `--budget` after any change to
`horizon`, `scan`, `cabins` or `trips`. Adding a cabin adds no calls but adds
rows, which adds pages. Adding a trip adds a full set of calls. Four trips
plan at 704 of 900 before pagination. Every sweep measures calls per window and `--budget`
multiplies by that. If it climbs past about 2.5, lower `chunk_days` or raise
`focus_interval_minutes`. `budget_safety_margin` exists so a manual `--once`
never trips the cap.

**Cached search only.** Pro accounts do not get Live Search, so real latency is
seats.aero's crawl, not the poll interval. Do not add a live search call path.

## Working agreements

- Run `python monitor.py --self-test` before and after any change. It needs no
  network and no API key.
- Never burn live API calls to test logic. Extend the synthetic fixtures in
  `self_test()` instead. `--probe` is the one sanctioned live diagnostic and
  it costs one call.
- No new dependencies beyond `requests` and `pyyaml` without asking.
- SQLite keeps every observation as a tick rather than upserting. The price
  history is what makes `--calibrate` possible. Do not switch to upsert.
- Thresholds are never written to `config.yaml` by code. `--calibrate` prints
  a block to paste.

## Likely next work

- Run `--calibrate` after two weeks on the schedule and paste the proposed
  numbers per trip.
- Check which source ids return rows for Mexico, Europe and Asia and trim
  the lists. Consider `flyingblue` for Paris, it is in the defaults.
- Date exclusion filter for school holidays and blackout ranges.
- Confirm which of the eight sources publish honest seat counts, then flip
  `require_seat_count` to true.
- Consider a second signal source, since seats.aero Delta coverage may lag
  delta.com on promotional pricing.

Out of scope without asking. Live Search, auto-booking, channels beyond
Pushover and the dashboard, scraping airline sites for fare brand.

## Prose style for docs and commit messages

No em dashes, colons, semicolons or ellipses. Use contractions. No rhetorical
questions. Plain and direct.
