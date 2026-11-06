# Executed experiment report

SUMO 1.28.0; seed 1001; step 0.5 s; cap 3600 s. Smoke inspected before twelve cyclic runs.

| Experiment | Expected | Completed | Incomplete | Teleported vehicles | Collision records |
|---|---:|---:|---:|---:|---:|
| Smoke | 300 | 300 | 0 | 0 | 0 |
| Twelve runs | 3600 | 3600 | 0 | 0 | 0 |

All twelve runs inserted and completed 300 vehicles each; no truncation. All warning logs were empty. Smoke ended at 1636.5 s. The first failed smoke attempt is retained under data/experiments/smoke: it stopped because startupDelay runtime retrieval returned an empty string. The successful smoke is data/experiments/smoke_verified.

Eight pytest tests passed after the final export change (3.06 s). Full read-back validation checks assignment balance, every original vehicle under every persona, trip joins, runtime parameters, unique telemetry keys, numerical jerk and partition paths. See readback_validation.json.

## Actual exported row counts

- driver_parameters: 3,600
- trip_summary: 3,600
- driving_events: 27,889
- simulation_runs: 12
- telemetry: 1,671,909

Smoke row counts: 139,228 telemetry, 300 parameters, 300 trips, 2,040 events, one run.

## Actual parameter values

driver_parameters.parquet records every sampled individual value. raw/run_XXXX/observed_parameters.parquet records departure-time TraCI getters. Available getters matched all 3,600 drivers within 1e-6 absolute tolerance. The ranges below are actual sampled values across twelve runs; startupDelay was accepted by schema and SUMO but could not be independently read at runtime.

| Parameter | Actual minimum | Actual maximum |
|---|---:|---:|
| speedFactor | 0.820465443141 | 1.29989226273 |
| accel | 1.30185329168 | 3.99764049959 |
| decel | 3.40100959755 | 5.99738047651 |
| tau | 0.60185608036 | 2.29899923276 |
| minGap | 0.801969797323 | 4.49461093083 |
| sigma | 0.0506593964442 | 0.996205527574 |
| actionStepLength | 0.5 | 1.5 |
| startupDelay | 0 | 1.99766656456 |
| lcStrategic | 0.900161585693 | 2.194467752 |
| lcSpeedGain | 0.300768937925 | 2.99896508883 |
| lcCooperative | 0.202897455156 | 0.999981906025 |
| lcKeepRight | 0.800018986126 | 1.19967560033 |
| lcAssertive | 0.650971227189 | 1.99590380473 |
| lcOvertakeRight | 0 | 0.149597634729 |
| apparentDecel | 3.40100959755 | 5.99738047651 |
| emergencyDecel | 9 | 9 |

## Missingness and limitations

- Road legal limits remain unverified. network_speed_audit.json lists 153 tertiary lanes near 80 km/h, with edge IDs and original values. No road speed was changed. Exceedance is relative to SUMO lane limits.
- startupDelay actual runtime getter is null for all drivers. Sampled configuration values are retained.
- Anonymous inline routes have null route_id; route_edges preserves their exact sequence.
- Missing leaders and upcoming signals, first-observation jerk, nonclosing TTC, zero-speed headway and zero-exposure rates are null. Per-column counts are in data_quality.json.
- Failed departures cannot be established from pending vehicles at a cap; failed_departures_count is null. There were zero not-departed vehicles in these executed experiments.
- No SSM device data were collected; no SSM conflicts or red-light violations are inferred.
- Telemetry omits insertion and arrival intervals and teleport gaps from exposure; TripInfo provides authoritative whole-trip measurements.
- Telemetry Parquet files use Hive run_id partitions. run_id is reconstructed from the directory and omitted from individual physical files to avoid duplicate partition-column type conflicts. Both pyarrow.dataset and pq.read_table on the telemetry directory were exercised.
- Original network, routes, configuration and baseline TripInfo SHA256 hashes still match the pre-run manifest.
- No classification, insurance scoring, premiums or ML models were implemented.

## Reproduce and inspect

See README.md and docs/methodology.md. Output directories must be new. Read metadata, dictionaries, quality reports and raw XML in data/experiments/balanced_12.
