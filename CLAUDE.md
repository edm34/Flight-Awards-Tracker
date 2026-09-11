# Award Monitor

Personal tool. Monitors seats.aero for a JFK to SYD award redemption for a
family of four, ranks the whole bookable calendar by miles in four cabin views,
alerts on Pushover. See @README.md for setup and operating instructions.

## The goal

Beat 66,200 miles per person round trip in economy. That is what Eric paid in
April 2026, not the 60,000 he remembers. Source is the Delta award receipt,
confirmation GYLTUY, ticket 0062378205056, issued 6 Nov 2025.

- 66,200 miles plus $132.53 per person, 264,800 miles for four
- Out Wed 25 Mar 2026, DL771 JFK-LAX, DL41 LAX-SYD
- Back Thu 16 Apr 2026, DL40 SYD-LAX, DL958 LAX-JFK
- 22 nights, booked 139 days before departure
- Fare basis ESVR331/FFX15, **Delta Main Basic (N)**

Premium economy, business and first have no benchmark and no thresholds yet.
They are scanned, stored and ranked, and `--calibrate` proposes thresholds once
there is enough history. Nobody invents those numbers by hand.

Dates are fully open. Eric picks dates off the price leaderboard, not the other
way round. Do not add fixed-date assumptions.

## Shape of the code

One file, `monitor.py`, in this order. Model, config, storage, chunking,
client, parsing, pairing and ranking, notification, scan cycles, reporting,
calibration, self test, main.

- `validate_config` applies defaults and fails loudly. It rejects the old
  single-threshold layout by name so a stale config can't run.
- `rank_by_cabin` is the one place pairings become per-cabin ranked lists. It
  filters each cabin to its own sources and runs `viable` with that cabin's
  `min_seats` and tax cap. `evaluate`, `show_best` and `calibrate` all go
  through it so the three views can't drift apart.
- `evaluate` walks each cabin's list ascending with a running best stored as
  `best_total_miles:<cabin>`. Keep it single pass. An earlier version compared
  every candidate against a stale figure and alerted on the entire leaderboard
  on cold start. Observe-only cabins still track a best.
- `choose_focus_dates` takes the top date from each cabin first, then fills by
  global rank, capped at `scan.max_focus_windows`.
- `cached_search` returns a `SearchResult` with the calls it used. `run_sweep`
  aggregates that into the `sweep_stats` state key and `budget_plan` reads it
  back, so `--budget` reflects measured pagination.
- `RoundTrip.cabin` is the cabin a trip ranks under. Mixed pairings, only
  possible when `trip.allow_mixed_cabin` is true, rank under the lower cabin.
- `scan.mode` is `loop` or `scheduled`. Scheduled mode is GitHub Actions
  running `--sweep` and `--once` on the crons in `.github/workflows`, with
  the database kept on the `monitor-state` branch. `budget_plan` prices the
  day from `polls_per_day` and `sweeps_per_day` in that mode. `run_sweep` and
  `run_focus` both call `prune_history` on the way out, because scheduled
  mode never enters the loop.
- Never commit `.env` or the database. Credentials are repository secrets.
  The workflow files are Eric's, reviewed outside the repo. Don't rework the
  restore, save, concurrency or alert-if-down logic without asking.

## What is verified and what is not

Verified offline by `--self-test`, which runs against an in-memory database and
its own embedded config so it never touches real data. Date chunking, parsing
of all four cabins, same-cabin pairing and mixed cabin rejection, per-cabin
seat and tax gates, per-cabin alert gating with null thresholds, per-cabin
running best, dedupe key carrying the cabin, leaderboard rendering including
empty sections and `--cabin`, the pagination protocol against a fake session,
budget arithmetic with measured pagination, and calibration refusal and
proposal on synthetic history.

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

Still open after one probe page. Which programs return anything in J and F
for these routes. The first page of Oct to Nov 2026 had Y 25, W 3, J 0, F 0.
Empty is a finding, not a bug. The first full sweep will say.

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
`horizon`, `scan` or `cabins`. Adding a cabin adds no calls but adds rows,
which adds pages. Every sweep measures calls per window and `--budget`
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

- Run `--calibrate` after two weeks of `--loop` and paste the W and J numbers.
- Date exclusion filter for school holidays and blackout ranges.
- Confirm which of the eight sources publish honest seat counts, then flip
  `require_seat_count` to true.
- Consider a second signal source, since seats.aero Delta coverage may lag
  delta.com on promotional pricing.

Out of scope without asking. Live Search, auto-booking, a web UI, channels
beyond Pushover, scraping delta.com for fare brand.

## Prose style for docs and commit messages

No em dashes, colons, semicolons or ellipses. Use contractions. No rhetorical
questions. Plain and direct.
