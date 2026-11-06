# Synthetic driving telemetry

Use the supplied virtual environment and installed SUMO 1.28.0:

```powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m src.simulation.run_experiment --runs 1 --seed 1001 --step-length 0.5 --output data/experiments/my_smoke
# Inspect data_quality.json and raw XML before the full experiment.
.venv/Scripts/python.exe -m src.simulation.run_experiment --runs 12 --seed 1001 --step-length 0.5 --output data/experiments/my_balanced
```

Output directories must be new, preventing accidental replacement. Original files are read only. See [methodology](docs/methodology.md). Configured personas are exploratory simulator settings, with no empirical demographic or insurance interpretation.

The executed results are in [experiment_report.md](docs/experiment_report.md). Validate an existing experiment with:

```powershell
.venv/Scripts/python.exe -m src.validation.validate_outputs data/experiments/balanced_12
```

Read the partitioned telemetry using `pyarrow.parquet.read_table('data/experiments/balanced_12/telemetry')` or `pyarrow.dataset.dataset(..., format='parquet', partitioning='hive')`. The partition supplies run_id; individual physical files omit it.

Exposure-aware research tables, road context, missingness and threshold sensitivity are available in `data/research_enrichment_v1`. See [dataset enrichment](docs/dataset_enrichment.md) for audit findings, units and interpretation. The original experiment remains unchanged.
