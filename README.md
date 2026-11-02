# Waypoint Delivery Planning

A working, local-first delivery operations application with a browser dashboard, Google OR-Tools routing, SQLite persistence, deterministic synthetic datasets, independent validation, and portable exports. Python 3.10+ is required; the verified environment is Windows / Python 3.10.5 / OR-Tools 9.14.6206. No API keys, map accounts, Node build, or external data services are needed.

## Start

From PowerShell in this folder:

```powershell
.\start.ps1
```

The script creates an isolated `.venv`, installs dependencies, selects a free local port starting at 8000, and prints the URL. Open that URL. If your PowerShell execution policy blocks scripts, use the manual commands below; changing machine-wide policy is unnecessary.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m planner serve --port 8000 --scale full
```

Open **http://127.0.0.1:8000**. First startup generates, validates and exports the full dataset and can take up to a minute, depending on storage speed. Later startups restore the selected SQLite snapshot. If port 8000 is occupied, use `--port 8001`. Stop with Ctrl+C. `--scale starter` affects first startup only unless dates are supplied; select another scale in Generate plan or choose a separate `--data` directory. Both `generate` and `serve` accept paired `--start-date YYYY-MM-DD --end-date YYYY-MM-DD` arguments. Explicit startup dates select that period without deleting earlier snapshots.

## Workflows

- **Overview:** daily and overall KPIs, a daily results table, planned travel/service/waiting time, and an input-order versus optimized distance comparison, alongside the complete horizon's demand and failures.
- **Dispatch board:** choose any calendar date/year or use the previous/next-day arrows, including beyond the current run. Stored dates load their results; dates outside the selected run open Generate plan with that day prefilled, where the end date can be extended before generation. Inspect weight/volume loads, driver shift, ordered stops, planned/actual arrival and depot return. Select a map marker or stop-history button for the full delivery event trail. Route/driver/vehicle search and failure filters are available. The map is a coordinate-based schematic, not a road navigation service.
- **Exceptions:** reason-coded failures, explicitly unserved deliveries and delivered-late cases, searchable and filterable. Delivery details show each vehicle's weight, volume, site-access, shift/window and candidate-fleet blockers. Individually feasible but unassigned deliveries are distinguished from constraint-blocked demand; the heuristic does not prove global infeasibility.
- **Fleet & drivers:** capacities, assigned driver, shifts, and route links; filter the roster to vehicles assigned that day.
- **Data & quality:** searchable/paginated entity tables; baseline validation; isolated anomaly evaluation with TP/FP/FN/TN, detection rate, precision and expected corrective actions; provenance, checksums and run history.
- **Generate plan:** choose starter/full operation scale, any valid start date, inclusive end date, and an integer seed. The two dates are independent of operation scale and the dialog shows the selected day count. Each content-addressed run is an immutable version; previous datasets remain on disk. Identical seed, dates and configuration select the identical version and append a run-selection event with the period dates.
- **Export bundle:** all entity CSVs, complete JSON, SQLite, SQL seed, schema, field dictionary, separate ground truth/anomaly inputs, manifest, validation report, checksums and dataset README. Dispatch CSV is available per day.

## Generate And Verify

Use the same generator for both scales:

```powershell
.\.venv\Scripts\python.exe -m planner generate --scale starter --seed 20261022 --output artifacts/starter-20261022 --verify
.\.venv\Scripts\python.exe -m planner generate --scale full --seed 20261022 --output artifacts/full-20261022 --verify
.\.venv\Scripts\python.exe -m planner generate --scale full --start-date 2026-11-01 --end-date 2026-11-30 --output artifacts/november-full --verify
.\.venv\Scripts\python.exe -m planner validate artifacts/full-20261022/planning.sqlite
```

Output directories must be new: existing versions are never overwritten. `--verify` regenerates twice and compares canonical SHA-256 values and row counts for **every table**, including routes and events. The result is recorded in `source_manifest.json`. Archive generation also uses fixed ZIP entry timestamps. Reproducibility is guaranteed for the same pinned runtime/dependencies and configuration, not arbitrary future solver releases.

| Scale | Customer sites | Deliveries / orders | Vehicles / drivers | Days |
|---|---:|---:|---:|---:|
| Starter | 60 | 160 | 12 | 7 |
| Full | 500 | 2,000 | 100 | 30 |

Each scale adds one distribution depot to `CustomerSite`, so full has **501 site records**: 500 customers + 1 depot. These are workload defaults, not horizon limits. Without date arguments a new run starts on the local current date and uses the scale's default duration; the seed never determines the date. Supply both `--start-date` and `--end-date` to select an inclusive period and reproduce it later. The API accepts the same dates as `start_date` and `end_date` in `POST /api/plan`.

For selected periods, master fleet/site counts stay fixed while demand is `ceil(default_deliveries * selected_days / default_days)`, distributed over every calendar day, including weekends. Active vehicle availability is represented by a dated shift for each vehicle/driver every day. Routing checks active drivers and vehicles, dated shifts, effective capacity and site restrictions, time windows and service duration. Windows, traffic matrices, routes, simulated outcomes and events cover the entire period, including month/year boundaries and leap day. There is no 7-day or 30-day cap; feasible range size depends on available memory, storage and generation time. Valid ISO dates span 0001-01-01 through 9999-12-31. Order creation is normally two days before delivery, clipped to the calendar minimum when necessary. New IDs use each delivery/route's actual year; older snapshot IDs remain readable. All names, coordinates and driver identities are synthetic.

Malformed dates, missing date partners and reversed ranges are rejected before replacing the selected run. Empty planning days remain selectable with zero KPIs and no routes; a day with demand but no available resources records unserved outcomes. Entirely empty snapshots can be exported and restored. Anomaly scoring is explicitly unavailable when no route has at least two stops. Scenario coverage validation detects missing intermediate planning dates.

## Business Rules

1. Generate master sites, vehicles, capacities and drivers first; then orders, deliveries, shifts, windows, traffic scenarios and matrices. Derived routes, stops, outcomes and event streams follow. Every referenced parent must exist.
2. Weight follows a seeded triangular distribution (15-240 kg, mode 55); volume is 80-650 L and service is 8-24 minutes. Priority distribution is 70% standard, 23% high, 7% critical. Integer weight and volume are positive.
3. Vans carry 750 kg / 4,000 L; trucks carry 1,800 kg / 9,000 L. Every fifth customer site prohibits trucks. Drivers work one daily shift; start is 08:00-08:30 and finish 16:30-17:00. Capacity and access records have inclusive effective dates, with null end meaning open-ended.
4. All time values are integer local minutes after midnight on the entity date. Service **starts** within the delivery window; the 8-24 minute service duration may finish after the window, but planned depot return must fit the shift. Travel departure includes the previous stop's full service duration. Waiting is permitted.
5. Approximate road distance is rounded metres from an equirectangular projection of generated coordinates multiplied by 1.22. Planned minutes are `ceil(distance_m / 450 * traffic_factor)`. Daily traffic follows a fixed seven-day CLEAR/RAIN/CLEAR/ROADWORKS/CLEAR/RAIN/CLEAR cycle (1.0/1.25/1.5). Every day materializes all ordered pairs of that day's active delivery locations plus depot, including diagonals. It does **not** export 30 redundant 501-by-501 matrices for unused locations.
6. OR-Tools uses a deterministic parallel-cheapest-insertion **first solution**, not a wall-clock search budget. The solver minimizes travel-plus-service minutes and priority-weighted unserved penalties (100,000 / 1,000,000 / 10,000,000). These are soft priority preferences, not a lexicographic guarantee. Daily candidate fleet is `min(available, max(3, ceil(deliveries / 7)))`, rotated across the fleet. This explicit operating policy limits active vehicles; unserved means infeasible under that candidate fleet and heuristic, not proven infeasible across every possible fleet/route combination.
7. Capacity, volume, time windows, site restrictions and driver shift are hard planned constraints. All deliveries are either in exactly one stop or explicitly NOT_ATTEMPTED with NO_FEASIBLE_VEHICLE. An inactive fleet or universally prohibited class produces unserved outcomes, never an overloaded route.
8. Simulation propagates actual departure and travel along each route. Actual leg time is planned leg time multiplied by 1.03 in clear weather or 1.12 otherwise, rounded up. Actual arrival is the greater of planned arrival and previous actual departure plus actual travel; departure adds service duration. Return-to-depot completion is simulated similarly.
9. Failure precedence: a site numeric suffix divisible by 37 gives SITE_CLOSED; otherwise service departure after shift end gives SHIFT_EXCEEDED; otherwise arrival more than 15 minutes after the window end gives WINDOW_MISSED; otherwise DELIVERED. A delivered arrival can be late by 1-15 minutes. Unassigned demand is NOT_ATTEMPTED/NO_FEASIBLE_VEHICLE with null actual timestamps and stop. No outcome is independently randomly assigned. Simulated depot-return overtime is visible beside planned return; planned shifts remain hard-constrained.
10. Events retain CREATED -> ASSIGNED -> DELIVERED/FAILED, or CREATED -> UNSERVED. Routes retain PLANNED -> DISPATCHED -> COMPLETED. Master lifecycle columns are original states, not mutable current-state caches. Outcomes/events represent the simulation; their timestamps are synthetic local operational times, distinct from UTC run-selection audit timestamps.

## Metrics

- Assignment coverage = assigned deliveries / demand. Assigned + explicitly unserved = total demand.
- Daily and overall counts track completed (`DELIVERED`), failed, unserved (`NOT_ATTEMPTED`) and delayed arrivals (`late_minutes > 0`). Delays overlap completed/failed outcomes; they are not a fourth exclusive status. Overall percentages use horizon-wide totals, not averages of daily percentages. `/api/state` exposes `overall_metrics` and per-day `analytics[].metrics` alongside selected-day `metrics`.
- Delivery success, failed delivery and unserved rates = delivered, failed and unserved respectively / total demand x 100. Delivered + failed = assigned.
- On-time arrival (top summary) = attempted arrivals no later than their window end / assigned deliveries x 100. Closed sites can be on time but unsuccessful. On-time successful deliveries (Overview KPI) = successful deliveries arriving within their window / delivered x 100, excluding failed attempts from both numerator and denominator.
- Distance = all planned legs including outbound and depot return, summed in integer metres before km conversion. Average route distance = total distance / used routes. Planned travel time sums matrix leg minutes, including return; service time sums assigned service durations; waiting = planned route duration - travel - service. Route hours include all three.
- Average dispatch to completion = sum(actual service departure - assigned route start) / successfully delivered count, in minutes. This includes travel, waiting and earlier stops from dispatch; it is not just service duration or order lead time.
- Per-route utilization = `max(loaded_weight / weight_capacity, loaded_volume / volume_capacity)`; the top summary retains the unweighted mean across used routes. Overview weight and volume utilization separately divide total loaded units by total available units on **used routes** x 100; API `vehicle_utilization_pct` is the larger of those aggregate percentages. Idle fleet capacity is not included, and weight/volume units are never added together.
- Zero-denominator operational KPIs return 0; counts remain visible so an empty day is distinguishable from measured performance.
- Anomaly scoring uses one expected-class positive patch and one clean-baseline negative control per required class. TP = expected class detected in its patch; FN = expected class missed; FP = that class reported on the clean baseline; TN = that class absent from the clean baseline. Detection rate = TP / (TP + FN) x 100; precision = TP / (TP + FP) x 100 (null when undefined). Secondary patch violations are retained but unscored because their full expected labels are not independently adjudicated. These twelve targeted decisions are **not** a representative estimate of real-world classifier performance.

## Route Efficiency Baseline

Overview compares input-order visits against the OR-Tools plan for the **same assigned delivery set on the same vehicles**. Both distances include depot departure and return. Distance saved = baseline metres - optimized metres; improvement = distance saved / baseline metres x 100 (0 for an empty baseline). Negative savings are shown honestly, not clamped to zero.

The baseline reorders each used route by original delivery input order; it is a reproducible distance reference, **not a second time-window/shift-feasible operating plan**. The solver optimizes travel-plus-service and unserved penalties, not distance alone, so distance improvement is not guaranteed. Unserved deliveries are excluded from both distances. No measured fuel or cost reduction is claimed without a fuel/cost model.

## Model And Ground Truth

See [docs/MODEL.md](docs/MODEL.md) for all relationships and lifecycle/effective-date semantics. Every export contains the executable `schema.sql` and complete field-level `DATA_DICTIONARY.md` with types, required flags, enums, units, uniqueness, FK targets and validation rules. [planner/schema.py](planner/schema.py) is their single source of truth.

`ground_truth.csv` and `ground_truth.json` contain the independently defined expected anomaly labels. `anomaly_inputs.json` contains six label-free patches, applied **one at a time** to a valid baseline by `planner.validation.apply_case`. It covers capacity exceedance, omitted stop, duplicated stop, impossible arrival order, shift exceedance and out-of-region coordinates. For the omitted-stop case, the now-missing stop reference on its outcome is nulled while preserving its assigned outcome, so the semantic omission is detected rather than masked by an unrelated dangling reference.

| Anomaly ID | Required scenario | Expected classification | Expected action |
|---|---|---|---|
| CASE-001 | Capacity exceeded | ROUTE_EXCEEDS_CAPACITY | REALLOCATE_LOAD |
| CASE-002 | Missing stop | STOP_OMITTED | RESTORE_STOP |
| CASE-003 | Duplicate stop | STOP_DUPLICATED | REMOVE_DUPLICATE |
| CASE-004 | Invalid arrival sequence | ARRIVAL_BEFORE_PREVIOUS_DEPARTURE | REPLAN_TIMING |
| CASE-005 | Driver shift exceeded | DRIVER_SHIFT_EXCEEDED | REASSIGN_OR_SHORTEN_ROUTE |
| CASE-006 | Invalid coordinates | COORDINATE_OUTSIDE_REGION | CORRECT_COORDINATES |

The separate truth files include `case_id` (anomaly ID), `issue_type`, `expected_classification`, `expected_action`, `ground_truth` and `entity_id`. The evaluator joins patches to truth by case ID and compares classifications after independent validation. Export version 1.1 adds the actions and scored report; version 1.2 records start/end dates and day count, with an exact-period regeneration command; version 1.3 removes fixed-year IDs and supports empty planning snapshots. Existing snapshots remain unchanged; select **Generate plan** to create a new-format export, even for the same seed. The live quality view recomputes evaluation for older snapshots without modifying them.

The operational database never contains injected corrupt records or ground-truth labels. `DeliveryOutcome`, `DeliveryEvent` and `RouteEvent` are target/history tables, not pre-delivery features. Exclude them and the anomaly/truth files when training a pre-delivery model. The manifest explicitly lists feature and target tables. Planner and validator never accept ground truth as an input.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check web/app.js
```

The Python suite uses only standard-library unittest and the installed routing dependency. It covers deterministic generation, 7/30/31/35-day horizons, cross-month/year and leap-year periods, minimum/maximum dates, invalid ranges, missing days, empty-day and empty-period export/reload, inactive drivers, missing shifts, effective-dated resources, constraint feasibility, all six negative scenarios, explicit unserved accounting, service-duration feasibility, KPI denominators, baseline/travel arithmetic, anomaly TP/FP/FN controls, schema errors, FK checks, lifecycle replay, immutable history triggers and SQL/JSON/database round trips. HTTP acceptance tests create each required calendar range, query every date, reconcile KPIs, download exports and restore history. Full-scale reproducibility is tested with the generator command above. These are focused acceptance tests, not a claim that every possible input has been exhaustively tested.

## Persistence And Boundaries

`data/current.json` selects an immutable database under `data/versions/<scale>-<seed>-<digest>/`. `data/run_history.jsonl` is an append-only plan-selection log. SQLite enforces primary/foreign keys and enums; triggers reject UPDATE/DELETE on delivery/route events. The application lock serializes generation and queries. Normal planning never overwrites an earlier dataset. Validation adds spatial, arithmetic, sequence, temporal and lifecycle rules beyond SQL constraints.

The server binds to **127.0.0.1 only**. It is a single-user local demonstration, not an authenticated or hardened multi-tenant production service. Do not expose it directly to the internet. Real traffic, addresses, fleet telematics, order ingestion, arbitrary hand-edited constraints, driver breaks, multi-depot routing, partial deliveries, retries and production authentication are not implemented. Synthetic distances are not suitable for navigation or legal shift certification. No optimization-gap or global-optimality guarantee is made.

## Provenance

No external dataset or personal/confidential information is used. Generated records are dedicated to CC0-1.0. Each export records lawful provenance, fixed seed, generator version, transformations, row counts, table/file hashes, discarded fields (none), and validation results. No public reference data was required. OR-Tools is Apache-2.0; locally vendored Lucide 0.468.0 icons are ISC licensed; source and license are in [web/vendor/README.md](web/vendor/README.md). The schematic is drawn from synthetic coordinates and uses no third-party map tiles.