# Research enrichment of the twelve-run pilot

These files add exposure-aware measurements to the existing synthetic experiment. They do not add trips, real-world observations, risk labels, or calibrated risk estimates. The original `data/experiments/balanced_12` files remain unchanged. Join all individual tables on `(run_id, vehicle_id)`.

## Files

- `trip_research_features.parquet`: 3,600 rows, one per expected vehicle/run. Includes time and distance coverage, moving/stopped time, observable headway and closing-TTC time, time-weighted acceleration and jerk RMS, event rates per observed hour and per 100 observed kilometres, route grouping and short-trip indicators.
- `road_context_exposure.parquet`: exposure per driver/run/road class, including internal junction lanes as their own category. Road type comes from the original network, not inferred legal speed limits.
- `persona_exposure_summary.parquet`: 12 descriptive summaries. Fractions pool time numerators and denominators across trips rather than averaging individual fractions. Personas remain simulator settings and are not risk labels.
- `telemetry_missingness.parquet`: missing-value counts and fractions per run and physical telemetry column. Nulls often indicate that a measure is undefined, rather than a sensor or export failure. Partition-supplied run_id is not a physical telemetry column.
- `threshold_sensitivity.parquet`: seven alternate headway/TTC cutoff summaries. These count time at qualifying observed samples, not minimum-duration episodes. They must not be interpreted as counts of incidents or compared directly with the original episode counts.
- `quality_report.json`, `source_manifest.json`, `schemas.json`, `data_dictionary.json`: checks, source-file hashes, explicit Arrow types, and units/derivations.

## Denominators and temporal interpretation

Only positive, consecutive observation intervals no longer than 1.01 times the run step contribute time or distance. First observations and larger gaps contribute zero exposure. The endpoint's state and road class are assigned to the preceding interval; transitions within the 0.5-second interval cannot be reconstructed exactly. Distance is the nonnegative difference between consecutive SUMO distance values. Reconstructed per-trip time/distance were compared with the original trip summaries.

Moving means observed speed >0.1 m/s; stopped means speed <=0.1 m/s. This is a descriptive analysis cutoff, not a SUMO waiting-time definition. Short-trip flags use duration <60 s and route length <1 km solely to expose limited observation length. They do not exclude any trip and are not adequacy guarantees.

Headway is observable only with a leader, nonnegative physical gap and defined nonnegative headway. Closing TTC is observable only with a leader, nonnegative physical gap and defined nonnegative longitudinal TTC. Nonclosing followers can therefore have valid headway but no defined TTC. A leader outside the 200 m minimum lookahead/best-lane search may not be observed.

`short_headway_observable_fraction` divides sampled time below the manifest's 1 s threshold by valid headway time. `low_ttc_closing_fraction` divides sampled time below 1.5 s TTC by time with defined closing TTC. A zero denominator yields null, not a zero fraction. The headway observation fraction uses all consecutive observation time as its denominator. Speed exceedance fractions divide by time with a defined network speed ratio; legal limits remain unverified.

RMS acceleration/jerk is sqrt(sum(value squared * interval duration) / valid interval duration). Event rates use the original minimum-1-second episode counts, except official lane changes which are instantaneous records. Rates per 100 km and per observed hour use sampled exposure, not authoritative whole-trip exposure. Very short exposure can create unstable rates.

## Dependence and context

`route_group_id` is SHA256 of the whitespace-normalized original edge sequence. Repeated routes share the identifier across runs, so researchers can account for repeated-route dependence. This does not remove dependence between routes that share road segments or traffic conditions. Every actual route in this experiment has an inline edge sequence; reference-only IDs are explicitly prefixed in the reusable implementation.

No weather, visibility, pavement friction, measured road limits, claims, crashes or demographic information has been added. Missing real-world evidence cannot be recovered by postprocessing. Existing limitations, including unavailable runtime startupDelay, still apply.

## Actual audit findings

- 649 of 3,600 trips (18.0%) had zero observed headway time.
- 803 trips (22.3%) had zero observed closing-TTC time.
- 179 trips (5.0%) lasted under one minute; 384 (10.7%) covered less than 1 km.
- 300 unique original route sequences were confirmed.
- No nonfinite values, negative speeds, negative physical gaps or negative headways were detected by the implemented checks. This does not certify every possible data property.
- Valid headway exposure totalled 195,078 s; defined closing-TTC exposure totalled 106,155 s.
- Sampled TTC time below 1 s was 201.5 s, versus 1,793.5 s below 1.5 s. Threshold choice substantially changes this descriptive measure; it is not an empirically validated danger boundary.
- Every new Parquet table was read back and row counts verified. Reconstructed exposure matched all original trip summaries. Source artifact hashes matched before and after enrichment.

Five new tests cover interval gaps, observation denominators, RMS calculations, invalid-gap handling, grouping, explicit null types and the actual 3,600-row output/context totals. The executed combined test selection passed 9 tests; 4 existing temporary-directory tests were deselected. The full original suite had passed previously.

## Reproduce

```powershell
.venv/Scripts/python.exe -m src.processing.enrich_dataset --source data/experiments/balanced_12 --output data/research_enrichment_v2
.venv/Scripts/python.exe -m pytest -q tests/test_enrichment.py -p no:cacheprovider
```

Choose a new output directory. The enrichment command generates all five Parquet tables, quality report, source manifest and schemas. This field guide and the additional field dictionary accompany v1; the latter describes the same columns produced by the command.
