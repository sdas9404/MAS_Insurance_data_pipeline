# Codex implementation brief — SE4457 synthetic vehicle telemetry

You are operating in `C:\Users\suman\MAS-Insurance_data_pipeline` (Windows PowerShell). Inspect repository before edits. Existing:
- `network/london_filtered.net.xml`
- `network/london.poly.xml`
- `routes/london.rou.xml` (300 existing vehicles)
- `london.sumocfg`
- `data/raw/smoke_tripinfo.xml` (300 tripinfo records)
- SUMO 1.28.0 works on PowerShell; Python `.venv` with `traci`, `sumolib`, `pandas`, `pyarrow`, `pyyaml`, `pytest`.

GOAL: deliver research-ready **synthetic driving behaviour Parquet data only**. NO machine learning, scoring, risk labels, or insurance premium model. Original network, route file, config, and smoke output must remain untouched.

## Phase 0: Audit the road speed limits
Read `network_audit.md` and inspect the exact network. 153 tertiary lanes are marked 80 km/h. Output a report with edge IDs and current values; never silently rewrite the network. Support `network_speed_status` in metadata: `unverified`, `validated`, `corrected`. If unverified, mark any speed exceedance metric as relative to the SUMO network value, not real-world legal speeding.

## Phase 1: Persona assignment
Read `driver_profiles.yaml` and implement config validation. For each vehicle and run, sample all supported parameters reproducibly using a seeded RNG. The `profiles` override `default_ranges`. Maintain 12 persona groups, 25 cars each of 300, and a 12-run cyclic balanced assignment so each original route/vehicle appears under every persona once. Keep the original route IDs, edge sequences, departure schedules, and relevant child XML elements. Use generated per-vehicle `<vType>` definitions (one vType per driver, to preserve individual parameters) and/or documented vehicle overrides; set `speedFactor` on each `<vehicle>` to the individual sampled value. Set `apparentDecel=decel` and `emergencyDecel=9.0`. Enforce `tau>=actionStepLength`, positive gaps, valid ranges. Only use parameters supported by Krauss+LC2013 in SUMO 1.28.0. Validate generated XML and smoke-run a scenario. Do not call anything a realistic age or insurer risk class.

## Phase 2: Reproducible runs
CLI: `python -m src.simulation.run_experiment --runs 12 --seed 1001 --step-length 0.5` (may choose another coherent CLI, document it). Execute headless SUMO via TraCI. Preserve XML outputs under `data/raw/run_XXXX`. Run until no vehicles are expected/active (`simulation.getMinExpectedNumber()==0`) or configurable 3600s cap; record truncation, failed departures, teleports, collision counts; never assume 300/300 successes for persona runs. Seed route/persona sampling separately from SUMO random seed and keep manifest. Capture `--tripinfo-output`, `--lanechange-output`, `--collision-output`, `--statistic-output`, and optional SSM output (document exact SUMO 1.28 option compatibility).

## Phase 3: Telemetry data
Collect active vehicle states at every step, preferably TraCI subscriptions/batched reads, with `run_id, vehicle_id, persona, time_s, speed_mps, accel_mps2, jerk_mps3, lane_id, edge_id, lane_index, lane_position_m, x_m, y_m, distance_m, lane_max_speed_mps, speed_to_lane_limit_ratio, leader_id, leader_gap_sumo_m, leader_gap_physical_m, leader_speed_mps, time_headway_s, longitudinal_ttc_s, current_waiting_time_s, upcoming_tls_id, upcoming_tls_distance_m, upcoming_tls_state`. Explicitly distinguish TraCI `getLeader` gap semantics from physical bumper gap; verify against SUMO 1.28 API documentation and handle no-leader, internal lane, nulls, API error sentinel, and dynamic traffic lights. TTC is defined only for valid closing leaders. Do NOT infer red-light infractions solely from upcoming signal state. Write telemetry in bounded-memory chunks to Parquet, partitioned by `run_id`, with an explicit PyArrow schema and ZSTD compression. Use nullable Arrow columns, not '0' stand-ins.

## Phase 4: Trip/incident/assignment tables
- `driver_parameters.parquet`: one row per `(run_id, vehicle_id)` with **actual individually sampled parameters**, persona, road-route ID, departure plan, and RNG sampling seed.
- `trip_summary.parquet`: one row per expected `(run_id, vehicle_id)` with arrival/completion/teleport status, authoritative TripInfo departure/arrival/duration/distance/waiting/timeLoss, mean/median/p95/max speed, distance-weighted speed ratios, exposed kilometres/time, counts/rates of acceleration/braking/speeding/headway/near-miss events. Duration of episodes and thresholds configurable. No pseudo-labels.
- `driving_events.parquet`: episodes, event timestamps/durations, contextual road/lane, severity values; official SUMO lanechange-output to avoid confusing normal edge transitions with lane changes.
- `simulation_runs.parquet`: run/scenario ID, SUMO version, network/route SHA256, seeds, step size, demand, expected/arrived/failed/teleported count, OS, runtime, speed-limit audit status.
- `ssm_conflicts.parquet` optionally from SUMO SSM device with TTC/DRAC/PET semantics clearly distinct from longitudinal TTC.

## Phase 5: Tests and docs
Automated pytest for configs, individual sampling, determinism, XML preservation, appending route type references, numerical headway/TTC/jerk, contiguous episode counting, trip joins, Arrow types, no duplicate composite keys, one assignment per route per run, persona group balance, valid unique scenario file paths. Provide dataset schema/data dictionary with unit, provenance, derivation, missingness, and thresholds, methodology, run manifest and a `data_quality.json` report. Verify real Parquet row counts by reading with PyArrow. Test one smoke run first, then run 12 balanced runs. Report actual stats, warnings and limitations; DO NOT invent results. Preserve raw XML and original baseline.

External references: SUMO vehicle/vType definitions, Krauss car-following docs, TraCI vehicle retrieval, TripInfo, lanechange-output, collision-output, SSM device.
