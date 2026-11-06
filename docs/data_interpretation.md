# Understanding the simulation data and zero recorded collisions

## What this dataset contains

The main dataset consists of 12 balanced SUMO 1.28.0 runs with 12 exploratory personas and 25 vehicles per persona per run. Each of the 300 original vehicle/route assignments appears under every persona once. There are 3,600 completed trips, 1,671,909 post-step telemetry observations, 8,285.72 km of authoritative TripInfo distance and 232.21 vehicle-hours. No trips were incomplete and no teleports were observed. A separate successful smoke test ran first; its observations are not included in these totals.

Every handoff telemetry row explicitly includes run_id, vehicle_id, and persona. Vehicle IDs repeat across runs: use (run_id, vehicle_id) for trip joins, and add time_s for individual observations. Observations and vehicles sharing traffic conditions are correlated, not independent real-world drivers.

## What zero collisions means

**Zero collisions were recorded under the configured collision checks. This is not proof that every possible intersection collision was checked, that the personas are safe, or that their real-world crash probability is zero.**

A review after the initial handoff found that the run commands did not enable `--collision.check-junctions`. The installed SUMO 1.28.0 executable reports that option's default as `false`. Consequently the optional junction vehicle-shape overlap checks were not enabled. The existing collision XML and simulation counters cannot retrospectively establish what an additional collision-checking mode would have reported. No additional experiment has been run to resolve that limitation.

The recorded collision count remains zero; it must not be relabelled as an exhaustive count of physical crashes. SUMO collision recording can use car-following minimum-gap criteria as well as overlap checks, depending on settings, so simulator collision records are not automatically equivalent to real-world crash outcomes. The default junction-check limitation was omitted from the initial interpretation and is explicitly documented in this revision.

## Why this setup favoured collision avoidance

These are verified properties of the run configuration, not a causal reconstruction of each vehicle interaction:

- All vehicles used Krauss car following and LC2013 lane changing. Their built-in driving and safety logic remained active; the collection code did not disable it through TraCI overrides.
- Every sampled tau was at least its actionStepLength, so desired headway was not set below the driving-decision interval. The simulation step was 0.5 seconds.
- apparentDecel equalled each driver's sampled decel, avoiding a deliberately misleading braking-capability setting.
- emergencyDecel was fixed at 9.0 m/s². This was configured emergency capability, not a claim that drivers used that acceleration or that the value was empirically calibrated.
- No explicit distraction/perception-error device, forced hazardous manoeuvre, or red-light-running scenario was introduced.

An aggressive or tailgating persona changes parameters such as preferred speed, headway and gaps. Its name does not disable collision avoidance and does not guarantee a crash. Sigma is a Krauss imperfection setting; it is not a validated probability of inattention or a crash rate. Zero recorded collisions is therefore compatible with observable behavioural differences and threshold-qualified events. These properties explain why collision avoidance is plausible, but do not prove a unique cause for the zero count.

## What the events actually show

| Recorded event type | Count | Interpretation |
|---|---:|---|
| network_speed_exceedance | 14,916 | Episodes above 1.05 times the SUMO lane limit; real-world legal limits unverified |
| braking | 7,321 | Acceleration at or below -4.5 m/s² for the configured minimum episode duration |
| acceleration | 1,436 | Acceleration at or above 3.0 m/s² for the configured minimum episode duration |
| longitudinal_near_miss | 1,038 | Historical table name for closing longitudinal TTC below 1.5 s; not a verified near-crash label |
| short_headway | 452 | Defined physical-gap/speed headway below 1.0 s |
| lane_change | 2,726 | Official SUMO lane-change records, not inferred edge transitions |

The first five event types use a minimum accumulated qualifying episode duration of 1.0 second. Official lane changes are instantaneous records. Event thresholds are exploratory and recorded in the experiment manifest. Braking can be an appropriate response to traffic, and a lane change can be routine. Counts are not validated risk labels or crash outcomes.

## Why some telemetry cells are blank

A null is an undefined or unavailable observation, not a zero. No usable observed leader means that leader gap, speed, headway and TTC cannot be evaluated. Positive ego speed is required for headway; positive closing speed is required for longitudinal TTC. Leader search follows current best lanes with a 200 m minimum lookahead, so it is not an exhaustive search for all nearby vehicles.

Across the experiment, 649 trips had zero observed headway time and 803 had zero defined closing-TTC time. Those cases must not be assigned a safe-following interpretation simply because their recorded event count is zero. Use the enrichment table's observable-time denominators and null-aware fractions.

For run_0001, 90,851 of 139,228 rows have no usable leader; 106,360 have undefined headway and 121,573 have undefined closing TTC. In contrast, current_waiting_time_s and lane_index have no nulls in any of the twelve runs. In run_0001 they equal zero on 105,620 and 120,898 rows respectively. Lane index zero is a valid SUMO lane; waiting time zero is an observed value. A spreadsheet that hides zero values can make these fields appear blank.

## What research conclusions are supported

This is reproducible synthetic telemetry for examining designed behavioural differences and candidate exposure-normalized indicators. It does not validate crash prediction, claims prediction, insurance pricing, or a real-world risk score. Personas were not fitted to measured human driving, and no measured crash/claim outcomes, weather, friction or demographic data were collected. Legal road speed limits remain unverified; 153 tertiary lanes near 80 km/h are listed in the audit. The startupDelay parameter was accepted in XML but its runtime getter was unavailable.

More repetitions can describe simulator variability, but cannot supply missing external validation. A future crash-focused study would need documented collision-detection settings, suitable driver-error/scenario assumptions, sensitivity analyses and independent validation. Enabling a collision-detection option only changes what is checked; it does not by itself make driving less safe or create a representative crash dataset.

## Evidence and references

- `evidence/collision_detection_audit.json`: all twelve saved commands, parameter-constraint checks, and the installed version/default check used for this explanation.
- `evidence/raw_balanced_runs/run_XXXX/command.json`, `collisions.xml`, `statistics.xml`, and `observed_parameters.parquet`: original execution evidence.
- `statistics/`, `research_features/`, and `documentation/data_dictionary.*`: actual counts, exposure, definitions and missingness.
- Official SUMO [Safety documentation](https://sumo.dlr.de/docs/Simulation/Safety.html) explains collision avoidance, gap-based collision recording, and the optional junction checks. The installed 1.28.0 executable and saved commands are the authority for the settings actually used here.

This revision adds interpretation and detection limitations. It does not modify telemetry, sampled parameters, event counts, trip outcomes, or the original SUMO inputs, and no simulations were rerun.
