# BUILD.md

Work brief for Claude Code. Read `CLAUDE.md` first for project context, then
work through this file in order. Do not skip Task 0.

Stop and ask before deviating from anything under "Constraints".

---

## Objective

Add four price views to the monitor, one per cabin, each with its own
thresholds and its own leaderboard:

1. Cheapest economy (Y)
2. Cheapest premium economy (W)
3. Cheapest business (J)
4. Cheapest first (F)

Today the code hardcodes `["Y", "W"]` as a flat list and applies one set of
mileage thresholds to everything. That breaks the moment J and F enter, because
a business redemption on this route costs several times an economy one and would
either never fire or would drag the economy ceiling up past uselessness.

Dates stay fully open. The output is four ranked tables, and Eric picks dates
off whichever table he's shopping.

---

## Constraints

Carry these forward. They are in `CLAUDE.md` too.

- **Four seats on one booking.** Loosening the seat check to make a leaderboard
  look better is a regression. The one permitted change is a per-cabin
  `min_seats` override, see Task 2.
- **Fare brand is invisible.** seats.aero returns a mileage price, not a Delta
  fare family. The April 2026 tickets were Main Basic, non-refundable after 24
  hours. Every alert keeps its reminder to verify on delta.com.
- **1,000 API calls a day, hard.** Adding cabins does not add calls, since
  `cabin` is a parameter on the same query. It does add rows, which adds
  pagination, which does add calls. See Task 6.
- **Cached search only.** No live search code path.
- **Dependencies stay `requests` and `pyyaml`.** Ask before adding any other.
- **Every observation is a tick.** Do not switch the observations table to
  upsert. The price history is what makes calibration possible.
- **`--self-test` must pass before and after every change.** It needs no network
  and no API key. Extend the synthetic fixtures rather than burning live calls.

---

## Task 0. Verify the live API before touching anything else

Nothing in this repo has ever run against the real seats.aero API. Four
assumptions are guesses. Confirm each and fix the code to match reality.

```bash
python monitor.py --self-test                    # baseline, must pass
python monitor.py --sweep --dry-run --verbose    # first live calls
```

Capture one raw Availability object and reconcile it field by field against
`parse_availability`.

| Assumption | Where | How to confirm |
|---|---|---|
| Field names `YMileageCost`, `YRemainingSeats`, `YTotalTaxes`, `ComputedLastSeen`, `Route.OriginAirport` | `parse_availability` | Dump one raw object, diff the keys |
| `*TotalTaxes` is in the smallest currency unit, so divide by 100 | `parse_availability` | Compare one row against delta.com |
| Pagination keys `data`, `hasMore`, `cursor`, `skip` | `SeatsAero.cached_search` | A wrong cursor truncates sweeps silently, with no error |
| Quota header name, four candidates in `QUOTA_HEADERS` | `SeatsAero.cached_search` | Log the real one, then narrow the tuple to it |

Also confirm that `J` and `F` come back populated at all for these routes and
sources. If a program returns nothing in a cabin, note it rather than assuming
the parser is broken.

**Do not start Task 1 until Task 0 is done and `--self-test` still passes.**

---

## Task 1. Restructure config from a cabin list to a cabin map

Replace `trip.cabins` and the flat `alerting` mileage keys with a per-cabin map.
Keep `alerting` for the things that genuinely are global.

```yaml
trip:
  party_size: 4
  outbound_origins: ["JFK", "EWR", "LGA", "BOS", "IAD", "PHL"]
  outbound_destinations: ["SYD", "MEL", "BNE"]
  min_trip_nights: 10
  max_trip_nights: 35
  require_same_source: true
  allow_open_jaw: false
  allow_mixed_cabin: false        # new, see Task 2

cabins:
  Y:
    label: "Economy"
    enabled: true
    sources: [delta, virginatlantic, virginaustralia, qantas, alaska, american, united, aeroplan]
    benchmark_miles: 66200        # verified, April 2026 receipt
    floor_miles: 60000
    ceiling_miles: 72000
    new_best_margin_miles: 3000
    max_total_taxes_usd: 400
    pushover_priority: 1
  W:
    label: "Premium Economy"
    enabled: true
    sources: [delta, virginatlantic, virginaustralia, qantas, alaska, american, united, aeroplan]
    benchmark_miles: null         # no prior redemption
    floor_miles: null             # calibrate, see Task 4
    ceiling_miles: null
    new_best_margin_miles: 8000
    max_total_taxes_usd: 500
    pushover_priority: 0
  J:
    label: "Business"
    enabled: true
    sources: [delta, virginatlantic, virginaustralia, qantas, alaska, american, united, aeroplan]
    benchmark_miles: null
    floor_miles: null
    ceiling_miles: null
    new_best_margin_miles: 20000
    max_total_taxes_usd: 800
    min_seats: 4
    pushover_priority: 0
  F:
    label: "First"
    enabled: true
    # Delta sells no first class on this route. First here means partner metal,
    # mostly Qantas, so the useful sources are narrower.
    sources: [qantas, alaska, american]
    benchmark_miles: null
    floor_miles: null
    ceiling_miles: null
    new_best_margin_miles: 40000
    max_total_taxes_usd: 1200
    min_seats: 2                  # four first class seats together is a fantasy
    pushover_priority: 0
```

Rules:

- `sources` moves to per-cabin because F on this route is partner metal and
  querying `delta` for F wastes response size. Build the API query as the union
  of enabled cabins' sources per call, then filter per cabin when ranking.
- `min_seats` defaults to `trip.party_size` when absent.
- `benchmark_miles: null` means the leaderboard prints a blank in the "vs bench"
  column rather than a nonsense number. Only Y has a verified benchmark.
- `floor_miles: null` and `ceiling_miles: null` mean alerting is off for that
  cabin. It still gets scanned, stored and ranked. This is deliberate. Ship with
  W, J and F in observe-only mode until Task 4 gives real numbers.
- Write a `load_config` function that applies defaults and validates. Fail loudly
  on an unknown cabin code or a floor above a ceiling.

Migrate `config.yaml` in place. There is no existing user data to preserve.

---

## Task 2. Make pairing and evaluation cabin-aware

- `build_round_trips` currently pairs any leg with any leg. Add same-cabin
  matching, gated on `trip.allow_mixed_cabin`. Default false. An outbound in J
  and a return in Y is not a business redemption and should not appear in the
  business view.
- `viable()` takes per-cabin `min_seats` and `max_total_taxes_usd` rather than
  the globals.
- `alert_reason()` takes the per-cabin thresholds. When `floor_miles` and
  `ceiling_miles` are both null, return None every time.
- `evaluate()` groups by cabin, sorts each group ascending, and runs the running
  best walk **per cabin**. The current single running best across all cabins
  would let one cheap economy pairing suppress every business alert. Store best
  state per cabin, keyed `best_total_miles:{cabin}`.
- Focus window selection currently takes the top dates overall, which after this
  change would be all economy. Take the top date from each enabled cabin, then
  fill remaining slots by global rank, capped at `scan.max_focus_windows`.

---

## Task 3. Four views on `--best`

```bash
python monitor.py --best              # all four sections, in order Y W J F
python monitor.py --best --cabin J    # one section
python monitor.py --best --limit 40   # rows per section, default 15
```

Each section prints a header, then the same columns as today. Keep the existing
column layout, it works.

```
ECONOMY  benchmark 66,200 per person
out         back         nts route     prog       miles  vs bench  seats
2027-03-14  2027-04-05    22 JFK-SYD   delta     54,000   +12,200      4
...

BUSINESS  no benchmark, observe-only
out         back         nts route     prog       miles  vs bench  seats
2027-05-02  2027-05-24    22 JFK-SYD   delta    285,000         -      4
```

- Sections for enabled cabins always print, even when empty. An empty section
  says `no availability recorded` and that is useful signal, not a blank to hide.
- `vs bench` shows `-` when the benchmark is null.
- `seats` shows `?` when the program publishes no count.
- Add a `total for party` column or footer line per row group, since 285,000 each
  reads very differently at four passengers.

---

## Task 4. Add `--calibrate`

W, J and F ship with null thresholds because nobody knows what a good price on
this route looks like yet, and I am not going to invent numbers. Let the data
set them.

```bash
python monitor.py --calibrate
```

For each enabled cabin, from stored observations of round trip totals:

- report count of distinct combinations and the date span of the history
- report min, 10th percentile, median, 90th percentile
- propose `floor_miles` at roughly the 10th percentile, rounded to the nearest
  5,000, and `ceiling_miles` at roughly the median
- refuse to propose anything with fewer than 200 distinct observed combinations
  or less than 14 days of history, and say why

Print the proposed YAML block for copy and paste. Do not write to `config.yaml`
automatically.

Sanity check the implementation against the one number that is known: with the
real economy history loaded, the proposed Y floor should land somewhere near
60,000, not near 30,000. If it lands wildly off, the percentile is being computed
over single legs rather than round trip totals.

---

## Task 5. Cabin-aware alerts

- Alert title gets the cabin label, for example
  `285k BUSINESS DELTA JFK-SYD`.
- Pushover priority comes from the cabin config, so an economy floor hit can wake
  Eric at 3am while a business observation waits until morning.
- Keep the fare brand reminder line in every alert regardless of cabin.
- Add the party total to the alert body, which already happens, and the per-cabin
  benchmark comparison when one exists.
- Dedupe stays keyed on the round trip fingerprint, which already includes cabin.
  Confirm that, do not assume it.

---

## Task 6. Re-check the budget

Adding J and F does not change the call count, since cabin is a query parameter.
It does increase rows per response, which increases pagination, which does
increase calls.

- Run `python monitor.py --budget` and confirm the plan still fits.
- Instrument the first live sweep to log actual pages fetched per window.
- If real pagination exceeds one page per window often, raise `chunk_days` from
  31 so each query covers fewer dates, or raise `focus_interval_minutes`.
- Update `show_budget` to include a measured pages-per-window figure from the
  last sweep rather than only the theoretical plan.

The planner currently assumes one page per window, which is optimistic. Fix that.

---

## Acceptance criteria

- [ ] `--self-test` passes, with new fixtures covering all four cabins, mixed
      cabin rejection, per-cabin thresholds, and the per-cabin running best
- [ ] A synthetic cheap economy pairing does not suppress a business alert
- [ ] `--best` prints four sections, empty ones included
- [ ] `--best --cabin F` prints only first
- [ ] Null thresholds mean scanned and ranked but never alerted
- [ ] `--calibrate` refuses to propose on thin history and explains why
- [ ] `--budget` reflects measured pagination, not the one-page assumption
- [ ] A live `--sweep --dry-run` runs clean and the parser matches real fields
- [ ] `README.md` and `CLAUDE.md` updated to describe cabin config and the four
      views

---

## Out of scope

Do not build these without asking.

- Live Search. Pro accounts do not have it.
- Auto-booking or any write action against Delta or seats.aero.
- Writing calibrated thresholds into `config.yaml` automatically.
- A web UI. The terminal leaderboard plus Pushover is the whole product.
- Additional notification channels beyond Pushover.
- Scraping delta.com to recover fare brand. Discuss the approach first, it has
  terms-of-service implications the API path does not.
