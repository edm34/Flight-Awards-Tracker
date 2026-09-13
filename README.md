# Award Monitor

Scans the whole bookable calendar for award round trips that four of you can
book together, from the New York area to Australia, Mexico, Europe and Asia.
Ranks every legal date combination by miles, per trip and per cabin, alerts on
Pushover when a price crosses a threshold, and feeds a live dashboard so you
can watch the market yourself. Runs on GitHub Actions, no server.

## The benchmark

From the April 2026 award receipt, ticketed 6 Nov 2025:

| | |
|---|---|
| Per person | 66,200 miles plus $132.53 |
| Party of 4 | 264,800 miles |
| Outbound | Wed 25 Mar, DL771 JFK-LAX, DL41 LAX-SYD |
| Return | Thu 16 Apr, DL40 SYD-LAX, DL958 LAX-JFK |
| Length | 22 nights |
| Booked | 139 days before departure |
| Fare | Delta Main Basic (N), fare basis ESVR331/FFX15 |

You booked roughly four and a half months out, so the horizon runs 45 to 360
days forward. You flew Basic Economy, which is non-changeable and almost always
non-refundable once the 24 hour window closes. Every alert carries a reminder
to check the fare brand before confirming.

Only Australia economy has a verified benchmark. Every other trip and cabin
shows a blank in the `vs bench` column until you have paid for one.

## Setup

```bash
pip install -r requirements.txt

export SEATS_AERO_API_KEY="..."
export PUSHOVER_USER_KEY="..."
export PUSHOVER_APP_TOKEN="..."

python monitor.py --self-test     # offline, no network, no API calls
python monitor.py --budget        # confirms the call plan fits
python monitor.py --test-alert    # proves the Pushover wiring
python monitor.py --probe         # one live call, reconciles the parser
python monitor.py --sweep --dry-run --verbose   # first real scan
```

## Trips

`config.yaml` has one block per trip. A trip is a set of destination airports,
a stay length, and its own price thresholds per cabin. Home airports are
shared and listed once under `origins`.

```yaml
origins: [JFK, EWR, LGA, BOS, IAD, PHL]

trips:
  australia:
    label: "Australia"
    destinations: [SYD, MEL, BNE]
    min_trip_nights: 10
    max_trip_nights: 35
    cabins:
      Y: {benchmark_miles: 66200, floor_miles: 60000, ceiling_miles: 72000}
  mexico:
    destinations: [MEX, SJD, LAP]
    min_trip_nights: 5
    max_trip_nights: 14
```

Cabin defaults, the programs to query, tax caps, seat rules and Pushover
priority, live once under `cabins` and apply to every trip. A trip's own
`cabins` block overrides any of them. Prices are only ever per trip, because
Mexico and Sydney are not on the same scale, and validation refuses a price on
the global block.

- `floor_miles` and `ceiling_miles` are set together or left null together.
  Null means observe-only. The cabin is scanned, stored, ranked and shown on
  the dashboard, it just never alerts. Everything but Australia economy ships
  this way until `--calibrate` has history.
- `min_seats` is the one permitted loosening of the four seat rule. First
  class carries 2 because four first class seats together is a fantasy.
- A destination belongs to exactly one trip and can't also be an origin.
- Each trip is its own API query, so four trips cost four times the calls of
  one. Run `--budget` after adding one.

## Seats

Four seats on one booking is the binding constraint, and some programs never
publish a seat count. The monitor keeps those rows but never lets a maybe read
as a yes.

- A row whose count is published and at least `min_seats` is confirmed.
- A row whose program publishes no count is kept, shown as `?` in every table,
  counted in every section footer, and its alert title ends in
  `seats unconfirmed` with a line in the body saying the four seats are not
  confirmed. The dashboard shows an amber pill.
- A row whose published count is below `min_seats` is dropped. It can't be
  booked for the party.

The first live sweep showed Alaska and Qantas publishing a count on every row
and American publishing none. `alerting.require_seat_count: true` drops
unpublished rows everywhere instead of flagging them.

## Where to go right now

```bash
python monitor.py --overview
```

One table. A row per trip, a column per cabin, the cheapest viable round trip
per person with a `?` when the seats are not confirmed, and once there are
three weeks of history, the gap against the recent typical best. A closing line
names the cheapest trip per cabin. This is the "should we go somewhere else
instead" view. The same table heads `--best` and the dashboard.

## The calendar

```bash
python monitor.py --calendar --trip australia --cabin Y            # by departure day
python monitor.py --calendar --trip australia --cabin J --returns  # by return day
```

Twelve months of days for one trip and cabin. Each cell is the cheapest
viable round trip that departs (or returns) that day, in thousands of miles
per person, with `?` when the seats are not confirmed, a dot where a one-way
leg exists but nothing pairs into a bookable round trip, and blank outside
the scanned horizon. The dashboard draws the same twelve months as a heat
grid with a cabin picker and a departures/returns toggle, and hovering a day
shows the price, the matching return date, the program and the seat status.
The horizon runs 45 to 360 days out so the grid reaches a year ahead. Most
programs load inventory 331 to 355 days ahead, so the last month is thin.

## Choosing dates by price

```bash
python monitor.py --best                                 # overview, then every trip and cabin
python monitor.py --best --trip asia                     # one trip
python monitor.py --best --trip australia --cabin J      # one trip and cabin
python monitor.py --best --limit 40                      # rows per section, default 15
```

Every viable combination ranked by total miles per person, one section per
trip and cabin, in the order Y W J F. You pick dates off whichever table you
are shopping.

```
=== AUSTRALIA  SYD/MEL/BNE, 10 to 35 nights

ECONOMY  benchmark 66,200 per person
seats for 4, taxes under $400  |  alerts at or under 60,000, on a new best by 3,000, never above 72,000
out         back         nts route     prog              miles  vs bench  seats      party
------------------------------------------------------------------------------------------
2027-03-14  2027-04-05    22 JFK-SYD   delta            54,000   +12,200      4    216,000
2026-10-27  2026-11-17    21 JFK-SYD   american         60,000    +6,200      ?    240,000
  2 of 2 viable combinations, 2 beat the benchmark, 1 with no published seat count (shown as ?)
```

Every enabled section prints, even when empty. An empty section says
`no availability recorded`, or how many legs were seen and why none paired.
`party` is the per person figure times `party_size`, because 285,000 each
reads very differently at four passengers.

## The dashboard

Every scheduled run writes `dashboard.json` to the `monitor-state` branch
next to the database. The dashboard is a published claude.ai page that reads
that file through your GitHub connector and refreshes itself every minute, so
it is at most one poll behind the market. It shows the overview as cards, a
tab per trip with a twelve month calendar, the four cabin leaderboards, a
sparkline of each cabin's best price over the last 45 days, and a seat pill
on every row.

The page needs the GitHub connector in claude.ai Settings > Connectors. Without
it the page still renders the snapshot it was published with and says how to
make it live.

```bash
python monitor.py --export dashboard.json   # what the workflows run
```

## Calibrating thresholds

```bash
python monitor.py --calibrate
```

For each trip and cabin, from every viable round trip total in the whole
retained history, latest price per leg, it reports the count of combinations,
the date span, min, 10th percentile, median and 90th percentile. It proposes
`floor_miles` at the 10th percentile and `ceiling_miles` at the median, both
rounded to the nearest 5,000, and prints a YAML block to paste under `trips`.

It refuses to propose on fewer than 200 viable combinations or less than 14
days of history and says which. Nothing is written to `config.yaml`
automatically.

## Alert rules

Price gates live per trip and cabin, plumbing lives under `alerting`.

- `floor_miles` fires unconditionally. Anything at or under wakes you.
- `new_best_margin_miles` fires when a fresh best for that trip and cabin
  beats the standing best by that much.
- `ceiling_miles` suppresses everything worse, however good the trend.

Each trip and cabin sorts ascending and walks with its own running best,
stored as `best_total_miles:<trip>:<cabin>`, so a cold start alerts on each
list's winner and a cheap Mexico economy pairing can't suppress a Sydney
business one.

Each list sends at most one message per pass. The cheapest qualifying pairing
is written out in full and every other qualifying pairing gets one line, so
twenty return dates at one price arrive as one message titled
`60k AUSTRALIA ECONOMY AMERICAN JFK-SYD, seats unconfirmed +19 more`. Each
pairing inside the digest is still deduped on its own and repeats only if it
improves by `improvement_threshold_miles` or the cooldown expires.

Pushover priority comes from the cabin, so an economy floor hit can wake you
at 3am while a business observation waits until morning.

## How it scans

Two modes share one budget.

**Sweep** walks the full horizon in 31 day windows, every trip, both
directions. Eleven date ranges times four trips is 44 windows, three times a
day. Europe returns about four thousand legs a window and needs seven calls
where the other trips need two, so a sweep is about 143 calls.

**Focus** re-polls `focus_pad_days` either side of the best dates. Every
trip's economy top first, then every trip's premium top, and so on, capped
at `max_focus_windows`, hourly on the schedule.

Every sweep records how many calls each window actually took and `--budget`
multiplies the plan by that measured figure. Four trips plan at 741 of 900
usable calls a day at the measured 3.25 calls per window.

## Hosting on GitHub Actions

`config.yaml` ships with `scan.mode: scheduled`. Three workflows under
`.github/workflows` run the monitor without a server.

- `probe.yml` is manual. One live call, prints the reconciliation table.
- `focus-poll.yml` runs `--once` hourly at :07.
- `full-sweep.yml` runs `--sweep` at :17 every eight hours.

The database and `dashboard.json` live on an orphan branch called
`monitor-state`. A leg gets a new row only when it is new or something about
it changed, and an unchanged leg refreshes its `last_confirmed_at`, so the
history stays complete without eighty thousand duplicate rows a sweep. Each run restores them, works, then force-pushes them back. A
run refuses to start on a blank database if that branch exists but can't be
read. Both scheduled workflows share one concurrency group, and a separate job
sends a Pushover message if a run fails or times out. Every run prunes
flown departure dates and change ticks past `storage.retain_observation_days`
on its way out, keeping each leg's latest tick, and
`--best` fences its output in a code block under Actions so the job summary
keeps its columns.

## First live run

The parser was reconciled against a live response on 11 Sep 2026 by `--probe`.
Field names, tax units (minor unit of `TaxesCurrency`), pagination (`cursor`
plus a ready-made `moreURL`), the `x-ratelimit-remaining` quota header and
the `cabins` filter with word values are all confirmed. Run `--probe` again
after any parser change. It costs one call.

## Known limits

**Fare brand is invisible.** seats.aero returns a mileage price, not a fare
family. A price the monitor surfaces could be Basic or Main, and you won't know
until you're on the airline's site.

**Cached data only.** Pro accounts don't get Live Search, so your true latency
is seats.aero's crawl rather than the poll interval.

**Source coverage.** The first sweep returned rows from Qantas, Alaska and
American only. Delta, Virgin Atlantic, United and Aeroplan returned nothing for
Australia. Virgin Australia's program is Velocity, so both `virginaustralia`
and `velocity` are listed until a sweep says which id returns rows. Unknown
source ids are ignored by the API, not rejected.

**Taxes are compared in the currency seats.aero reports.** `TaxesCurrency` is
stored with every observation. `--stats` shows which programs are in play.

## systemd

For `scan.mode: loop` on a machine of your own.

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
