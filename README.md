# Award Monitor

Scans the whole bookable calendar for a JFK to SYD round trip that four of you
can book together, ranks every legal date combination by miles, and alerts on
Pushover when a new best appears or anything drops under your floor. There is
one leaderboard per cabin. Economy, premium economy, business and first each
carry their own thresholds and their own running best, so a cheap economy print
never hides a business one.

## The benchmark

From the April 2026 award receipt, confirmation GYLTUY, ticketed 6 Nov 2025:

| | |
|---|---|
| Per person | 66,200 miles plus $132.53 |
| Party of 4 | 264,800 miles |
| Outbound | Wed 25 Mar, DL771 JFK-LAX, DL41 LAX-SYD |
| Return | Thu 16 Apr, DL40 SYD-LAX, DL958 LAX-JFK |
| Length | 22 nights |
| Booked | 139 days before departure |
| Fare | Delta Main Basic (N), fare basis ESVR331/FFX15 |

Two things follow. You booked roughly four and a half months out, so the
monitor's horizon runs 45 to 331 days forward and covers that zone comfortably.
And you flew Basic Economy, which the receipt confirms is non-changeable and
almost always non-refundable once the 24 hour risk-free window closes. Book
fast, but check the fare brand on delta.com before you confirm. The monitor puts
that reminder in every alert.

Only economy has a verified benchmark. The other three cabins show a blank in
the `vs bench` column until you have paid for one.

## Setup

```bash
pip install -r requirements.txt

export SEATS_AERO_API_KEY="..."
export PUSHOVER_USER_KEY="..."
export PUSHOVER_APP_TOKEN="..."

python monitor.py --self-test     # offline, no network, no API calls
python monitor.py --budget        # confirms the call plan fits
python monitor.py --test-alert    # proves the Pushover wiring
python monitor.py --probe         # one live call, dumps the raw response
python monitor.py --sweep --dry-run --verbose   # first real scan
```

## First live run

Nobody has run this against the real seats.aero API yet. The parser follows the
published Availability schema and two open source client libraries, but four
things still need a human with a key to confirm. `--probe` makes exactly one
call and prints everything needed to do that.

1. **Field names.** The probe prints the first raw object and a reconciliation
   table with `ok` or `MISSING` next to every field the parser reads. Mileage
   comes from `<cabin>MileageCostRaw` when present and the string
   `<cabin>MileageCost` otherwise. `ComputedLastSeen` may be absent, in which
   case `UpdatedAt` is used.
2. **Tax units.** `<cabin>TotalTaxes` is treated as the minor unit of
   `TaxesCurrency`, so 6625 becomes $66.25. Compare the probe's parsed taxes
   for one row against delta.com. If they are off by 100x, remove the division
   in `parse_availability`.
3. **Pagination.** seats.aero pages with the `cursor` from the first response
   and a `skip` equal to the rows already received. When a response carries a
   `moreURL` the monitor follows that instead. The probe prints `count`,
   `hasMore`, `cursor` and `moreURL` so you can see which shape you get.
4. **Quota header.** Three candidate header names are tried. The probe prints
   every response header and marks the one that matched. `--budget` reports
   the remaining quota from it afterwards.

The cabin filter sends word values (`economy,premium,business,first`) rather
than the letter codes used in field names. If the probe returns HTTP 400,
that is the first thing to check.

Also note which cabins come back populated. The probe counts rows with
availability per cabin. A program returning nothing in first class is normal,
not a parser bug.

## How it scans

Two modes share one budget.

**Sweep** walks the full horizon in 31 day windows, both directions, every six
hours. Ten windows, two calls each, four times a day is 80 calls before
pagination.

**Focus** re-polls only the months around each cabin's best combination every
15 minutes. Three windows, two calls each is 552 calls before pagination. Focus
windows go to the top date of each cabin first, in the order Y, W, J, F, then
fill by global rank. With four cabins and three windows the fourth cabin only
gets one when another cabin has nothing.

Adding cabins does not add calls, since cabin is a parameter on the same query.
It does add rows, which adds pages, which adds calls. Every sweep records how
many calls each window actually took and `--budget` multiplies the plan by that
measured figure rather than assuming one page per direction.

```bash
python monitor.py --loop
```

The scheduler decides each tick whether a sweep is due and runs a focus poll
otherwise.

## Choosing dates by price

```bash
python monitor.py --best              # all four cabins, in order Y W J F
python monitor.py --best --cabin J    # one cabin
python monitor.py --best --limit 40   # rows per section, default 15
```

Every viable combination ranked by total miles per person, one section per
enabled cabin. This is the deliverable. You pick dates off whichever table you
are shopping rather than picking dates and hoping.

```
ECONOMY  benchmark 66,200 per person
seats for 4, taxes under $400  |  alerts at or under 60,000, on a new best by 3,000, never above 72,000
out         back         nts route     prog              miles  vs bench  seats      party
------------------------------------------------------------------------------------------
2027-03-14  2027-04-05    22 JFK-SYD   delta            54,000   +12,200      4    216,000
2027-02-10  2027-03-04    22 JFK-SYD   delta            66,200        +0      4    264,800
  2 of 2 viable combinations, 1 beat the benchmark

BUSINESS  no benchmark, observe-only
seats for 4, taxes under $800  |  no alerts until thresholds are set, run --calibrate
out         back         nts route     prog              miles  vs bench  seats      party
------------------------------------------------------------------------------------------
2027-05-02  2027-05-24    22 JFK-SYD   delta           285,000         -      4  1,140,000
  1 of 1 viable combinations
```

Every enabled section prints, even when empty. An empty section says
`no availability recorded`, or how many legs were seen and why none paired,
and that is useful signal. `vs bench` shows `-` when the cabin has no
benchmark. `seats` shows `?` when the program publishes no count. `party` is
the per person figure times `party_size`, because 285,000 each reads very
differently at four passengers.

## Cabin config

Each cabin is one block under `cabins` in `config.yaml`. Only `sources` is
required. Everything else has a default.

```yaml
cabins:
  Y:
    label: "Economy"
    enabled: true
    sources: [delta, virginatlantic, qantas, ...]
    benchmark_miles: 66200        # null prints a blank vs bench column
    floor_miles: 60000            # at or under this always alerts
    ceiling_miles: 72000          # above this never alerts
    new_best_margin_miles: 3000   # also alert on a new cabin best by this much
    max_total_taxes_usd: 400
    min_seats: 4                  # defaults to trip.party_size
    pushover_priority: 1          # -2 to 2, 1 bypasses quiet hours
```

- `sources` is per cabin because first class on this route is partner metal
  and querying Delta for it wastes response size. One API call carries the
  union of every enabled cabin's sources, then each cabin ranks only its own.
- `floor_miles` and `ceiling_miles` are set together or left null together.
  Null means observe-only. The cabin is still scanned, stored and ranked, it
  just never alerts. W, J and F ship this way until `--calibrate` has history.
- `min_seats` is the one permitted loosening of the four seat rule. First
  class carries 2 because four first class seats together is a fantasy.
- `trip.allow_mixed_cabin: false` keeps every pairing in one cabin both ways.
  An outbound in business and a return in economy is not a business
  redemption. When true, a mixed pairing ranks under its lower cabin and
  `--best` adds a `cabins` column.

Config validation fails loudly on an unknown cabin code, a floor above a
ceiling, one threshold set without the other, `min_seats` above `party_size`,
and on any key left over from the old single-threshold layout, naming where it
moved.

## Calibrating thresholds

```bash
python monitor.py --calibrate
```

Nobody knows what a good business price on this route looks like yet, and the
monitor will not invent one. For each enabled cabin it takes every viable round
trip total from the whole retained history, latest price per leg, and reports
the count of combinations, the date span, min, 10th percentile, median and 90th
percentile. It proposes `floor_miles` at the 10th percentile and
`ceiling_miles` at the median, both rounded to the nearest 5,000, and prints a
YAML block to paste.

It refuses to propose on fewer than 200 viable combinations or less than 14
days of history and says which. Nothing is written to `config.yaml`
automatically.

Sanity check. With real economy history the proposed Y floor should land near
60,000. If it lands near 30,000, percentiles are being computed over single
legs rather than round trip totals.

## Alert rules

Price gates live per cabin, plumbing lives under `alerting`.

- `floor_miles` fires unconditionally. Anything at or under wakes you.
- `new_best_margin_miles` fires when a fresh cabin best beats the standing best
  by that much.
- `ceiling_miles` suppresses everything worse, however good the trend.

Each cabin sorts ascending and walks with its own running best, stored as
`best_total_miles:<cabin>`, so a cold start alerts on each cabin's winner rather
than the whole leaderboard, and a cheap economy pairing cannot suppress a
business alert. The same pairing repeats only if it improves by
`improvement_threshold_miles` or the cooldown expires. The dedupe key is built
from both legs' fingerprints, which carry the cabin, so the same dates in a
different cabin are a different alert.

Alert titles carry the cabin, for example `285k BUSINESS DELTA JFK-SYD`.
Pushover priority comes from the cabin, so an economy floor hit can wake you at
3am while a business observation waits until morning. Every alert carries the
party total, the benchmark comparison when the cabin has one, and the fare brand
reminder.

## Search breadth

Six departure airports and three Australian arrivals go in the same
comma-separated query, so widening costs nothing extra in API calls. Trip length
runs 10 to 35 nights, which covers your 22 night pattern with room either side.
`require_same_source: true` keeps both legs in one program, since splitting
across two programs means holding two mileage currencies.

## Known limits

**Fare brand is invisible.** seats.aero returns a mileage price, not a Delta
fare family. The 66,200 you paid was Main Basic. A price the monitor surfaces
could be Basic or Main, and you won't know until you're on delta.com. Given the
refund rules, that check is worth the thirty seconds.

**Cached data only.** Pro accounts don't get Live Search, so your true latency
is seats.aero's crawl rather than your poll interval. Rows older than
`max_data_age_hours` get dropped before alerting. The leaderboard uses a window
24 times wider so it survives a quiet day.

**Seat counts.** Some programs never publish them and return zero. The default
alerts anyway and labels the row `?`. Flip `require_seat_count` to true once you
know which of your eight sources report honestly.

**Four seats stays the constraint.** A cheap print usually surfaces with one or
two. The leaderboard shows the binding seat count per row so you can see whether
a headline number is real for a party of four.

**Taxes are compared in the currency seats.aero reports.** The monitor stores
`TaxesCurrency` with every observation. If a program reports in something other
than USD, the `max_total_taxes_usd` cap is comparing unlike units for that
program. `--stats` will show you which programs are in play.

## systemd

`/etc/systemd/system/award-monitor.service`

```ini
[Unit]
Description=seats.aero award monitor
After=network-online.target

[Service]
Type=simple
User=eric
WorkingDirectory=/opt/award-monitor
EnvironmentFile=/opt/award-monitor/.env
ExecStart=/usr/bin/python3 /opt/award-monitor/monitor.py --loop
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```
