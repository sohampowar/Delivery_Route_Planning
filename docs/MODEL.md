# Entity Relationships And Lifecycle

All entities have a text primary key. Relationships below are enforced by foreign keys; references are required unless explicitly marked optional. A parent may have zero children unless the business invariant below requires exactly one. All joins are checked before export; SQLite foreign-key checking is enabled on insert and SQL restore.

| Parent -> child | Cardinality | Foreign key / semantics |
|---|---|---|
| CustomerSite -> Order | 1:N | Order.site_id; one receiving site per order |
| Order -> Delivery | 1:N | Delivery.order_id; baseline creates one delivery per order; schema permits split deliveries |
| Vehicle -> VehicleCapacity | 1:N | VehicleCapacity.vehicle_id; effective-dated versions, exactly one applicable per route date |
| Driver -> Shift | 1:N | Shift.driver_id; unique driver/date |
| Vehicle -> Shift | 1:N | Shift.vehicle_id; unique vehicle/date |
| Driver <-> Vehicle | N:M over time | Shift is the dated assignment bridge; never comma-separated IDs |
| TrafficScenario -> TravelTimeMatrix | 1:N | scenario_id; one scenario per day |
| CustomerSite -> TravelTimeMatrix | 1:N twice | origin_id and destination_id; unique scenario/origin/destination |
| CustomerSite <-> CustomerSite | N:M | TravelTimeMatrix is a weighted/directed bridge; includes zero-cost diagonal |
| Delivery -> TimeWindow | 1:1 | TimeWindow.delivery_id unique; validator requires one per demand |
| CustomerSite <-> vehicle class | N:M | SiteVehicleRestriction bridge; vehicle_type enum, dated exclusion rather than permission |
| Shift -> Route | 1:0..1 | Route.shift_id unique; unused shifts are legal |
| CustomerSite (DEPOT) -> Route | 1:N | Route.depot_id; the generated operation has one hub |
| Route -> RouteStop | 1:N | RouteStop.route_id; sequence is consecutive from 1, unique per route |
| Delivery -> RouteStop | 1:0..1 | RouteStop.delivery_id unique in a valid plan; no stop if explicitly unserved |
| Route <-> Delivery | N:M logically | RouteStop is the ordered assignment bridge; a delivery can be in at most one current route in a dataset version |
| Delivery -> DeliveryOutcome | 1:1 | DeliveryOutcome.delivery_id unique; validator requires exactly one result per demand |
| RouteStop -> DeliveryOutcome | 1:0..1 | optional DeliveryOutcome.stop_id unique; null only for unserved baseline outcomes |
| Delivery -> DeliveryEvent | 1:N | delivery_id; unique delivery/sequence; append-only |
| Route -> RouteEvent | 1:N | route_id; unique route/sequence; append-only |

## IDs

Orders and deliveries are ORD-2026-000001 / DEL-2026-000001. Fleet and drivers are VEH-000001 / DRV-000001. Routes are RTE-2026-000001; stops are STOP-RTE-2026-000001-001. Counters are zero-padded and stable for the same generator settings. IDs are immutable within a dataset version. Different seeds are separate dataset namespaces, not mutations of a previous run's entities; joins must not mix run directories.

## Effective Dates

Sites, capacities and site/class restrictions have inclusive effective_from and optional inclusive effective_to dates; null means indefinitely effective. Baseline records start 2026-01-01. Driver/vehicle assignments use shift_date and local shift minutes. All orders precede their delivery date. Travel scenarios are date-specific snapshots; no matrix leg is interpolated from another day. Lifecycle events preserve the original entity records and are ordered by entity sequence and nondecreasing recorded_at. SQL composite uniqueness rules are listed in the generated dictionary.

## Lifecycle States

- CustomerSite: ACTIVE or INACTIVE. Generated sites are ACTIVE.
- Vehicle: ACTIVE or MAINTENANCE. Generated vehicles are ACTIVE.
- Driver: ACTIVE or INACTIVE. Generated drivers are ACTIVE.
- Order: CONFIRMED in this scope.
- Delivery: original CREATED record; effective status is projected from DeliveryEvent.
- Route: original PLANNED record; effective status is projected from RouteEvent.
- Shift, capacity, matrix, window and restriction: immutable master/dated assignment records, not state machines.
- DeliveryEvent: CREATED -> ASSIGNED -> DELIVERED or FAILED; alternatively CREATED -> UNSERVED. No subsequent retry is fabricated.
- RouteEvent: PLANNED -> DISPATCHED -> COMPLETED, including return travel time.
- DeliveryOutcome: DELIVERED, FAILED (required reason), or NOT_ATTEMPTED (NO_FEASIBLE_VEHICLE, null stop/actual times). Outcomes are immutable simulation targets.

Actual failed or late outcomes do not retroactively invalidate a feasible planned route. They are operational exceptions shown separately. Ground-truth anomaly fixtures are separate intentionally invalid candidate plans; they never overwrite valid operational rows.

## Validation Layers

1. Shape/types, required fields, immutable ID patterns, PK and composite uniqueness, enums and foreign keys.
2. ISO dates and effective periods; positive demand/capacity; bounded coordinates; valid windows and shifts.
3. One window/outcome per delivery; assigned-versus-unserved coverage; no duplicated delivery assignments.
4. Matrix distance/travel arithmetic; route leg completeness and distance reconciliation; chronological arrival/service/return; planned weight/volume capacity and site/class access restrictions; active resources and date matching.
5. Allowed event sequences and monotone timestamps; deterministic replay of simulation, delivery outcomes and route/delivery events on a structurally valid baseline.
6. Negative-scenario detection against a separate ground-truth file. Secondary consequences of each injected anomaly remain visible in validation reports.