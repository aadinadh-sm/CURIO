# CURIO

CURIO is a local-first Windows computer diagnosis prototype. Its primary workflow is one 30-second Quick Diagnosis: collect telemetry, summarize it into six five-second windows, screen for observable operating pressure, and show measured evidence and conservative next steps. It keeps independent checks in a local SQLite history and compares repeat sessions for exploratory precursor patterns.

## Run

Python 3.10 or newer is recommended.

```powershell
python -m pip install -r requirements.txt
python curio.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). CURIO binds to loopback and uses no external services or fonts. Set `CURIO_PORT` to change the port.

## What a Quick Diagnosis measures

Every second for 30 seconds, CURIO reads CPU load, memory use and availability, page-file use, system-wide disk I/O counters, network byte counters, available temperature sensors, and process CPU/memory summaries. It groups readings into five-second windows and derives peaks, averages, changes, rates and disk response-time estimates. Feature windows are stored in `curio.db`; process names are kept only in the saved result for local explanation. PIDs, file names, file contents, browser history and document contents are not stored or read.

Sensor availability varies. Windows may not expose temperature sensors. Disk service time is a system-wide estimate and is unavailable if the OS does not return disk timing counters. CURIO counts network throughput; it does not probe network latency or packet loss. It cannot prove thermal throttling from temperature alone.

## Current diagnosis status

Out of the box, CURIO uses transparent multi-signal screening rules. These rules are useful triage, but they are **not** a trained classifier and do not produce calibrated diagnosis probabilities. The interface states this directly. No classifier model is shipped because there is no validated, independently labeled controlled dataset in this project yet.

To enable the trained models:

1. Run repeated, controlled sessions for each observable operating state, including normal use. Keep sessions independent and record ground-truth labels from the experiment setup—not from CURIO's own suggested diagnosis.
2. In **History**, export the collected five-second windows. Fill the `label` column with one of `normal`, `cpu_saturation`, `memory_pressure`, `disk_io_bottleneck`, or `thermal_stress`. Keep all rows from one session under the same `session_id`. For multi-computer collection, enter consistent anonymized IDs such as `M1` in `machine_id`.
3. Train and evaluate with held-out sessions:

   ```powershell
   python train_models.py curio_research_windows.csv
   ```

   The trainer requires at least three independent groups per class, uses a group-held-out train/test split to keep adjacent windows together, fits a Random Forest classifier, and fits an Isolation Forest only on training normal windows. It writes `curio_model.joblib` and a held-out evaluation report next to it.

4. Restart CURIO. It loads the local model artifact and identifies model-derived results separately from rule-based screening.

For a stricter cross-machine evaluation, collect at least three machines per class with anonymized `machine_id` values, then run:

```powershell
python train_models.py curio_research_windows.csv --group-by-machine
```

Do not treat data from threshold-created pseudo-labels as independent ground truth. A high model score on labels derived from CURIO's rules would only show that the model learned those rules.

## Discovery

CURIO compares early and late windows across at least three pressure sessions and three no-pressure sessions. It reports a directional feature trend only when the trend recurs and is more common in pressure sessions. These results are exploratory associations, not causal explanations; they need more sessions and held-out confirmation before being presented as validated discoveries.

## Dataset Lab

Open **Dataset Lab** to upload a CSV up to 10 MB (25,000 data rows and 80 columns). CURIO previews columns and missingness, then runs each selected session through the same signal scoring, evidence explanation, timeline, and suggested checks as Quick Diagnosis. Select a session/run column for one result per run, or leave it blank to treat the file as one run. It recognizes CURIO feature-window exports and common CPU/RAM/disk telemetry names. Data is analyzed by the local server and is not added to SQLite history or saved to disk. The browser tab keeps an in-memory copy so you can adjust mappings and rerun analysis; click Clear or close the tab to release it.

For optional classification evaluation, select an independently assigned categorical label and a session/run grouping column. CURIO requires at least three independent groups per class and holds out whole groups; it will not report a misleading random-row split. The displayed metrics describe one exploratory split only and do not install a model into live diagnosis. Never use `suggested_label` as ground truth; it is excluded from model inputs.

The `web/samples/` folder includes a diagnosis-ready combined suite with 20 independent runs across four conditions (normal, CPU saturation, memory pressure, and disk I/O bottleneck), plus a focused CSV for each condition. Each run contains six five-second feature windows and the same feature names CURIO exports. The combined suite supports run-by-run diagnoses and a grouped evaluation demonstration; its high or low score says nothing about real-world accuracy. Additional balanced, overlapping, and unlabeled examples demonstrate alternate data profiles. All samples are synthetic—not real device telemetry, scientific evidence, or suitable production training data. Regenerate the diagnosis suite with `python generate_diagnosis_samples.py`.

## Controlled student dataset

`lab_workloads.py` provides opt-in CPU, memory, and temporary-file disk-I/O stimuli for controlled collection. It never starts with CURIO and never changes operating-system settings. Use a separate test computer when possible; workloads can make the computer slower while they run.

Example protocol for one run: start a workload in PowerShell, immediately click **Diagnose my computer** in CURIO, then let both finish. Do not use the same session twice as two independent runs.

```powershell
python lab_workloads.py cpu --seconds 45
python lab_workloads.py memory --seconds 45 --max-mb 512
python lab_workloads.py disk --seconds 45 --size-mb 128
```

Collect at least 3 independent sessions for each included condition and 3 normal sessions; 5–10 sessions per condition is a better target. `memory` leaves at least 1 GiB free and caps allocation at 512 MB by default, so it may not create measurable pressure on every machine. Increase its cap only on a controlled test machine with sufficient free RAM. Disk I/O uses a temporary file and removes it when the workload exits. Thermal stress is deliberately not generated by this helper.

For each independently controlled run, assign the actual experiment condition in every exported window's `label` cell. Use the `suggested_label` column only as a comparison; never use it as ground truth. Labels are `normal`, `cpu_saturation`, `memory_pressure`, or `disk_io_bottleneck`. Each `session_id` remains intact in the held-out split. Add an anonymized `machine_id` and use `--group-by-machine` only if you have enough machines for every class.

## Scope and limitations

- Quick Diagnosis is the MVP. Deep Diagnosis and continuous background monitoring are not implemented yet.
- Supported and exercised primarily on Windows; telemetry relies on `psutil` and some counters differ by operating system and hardware.
- Supported screening states: CPU saturation, memory pressure, combined disk busy time and response-time pressure, and thermal stress when a sensor is available.
- Network throughput is context, not a network-fault diagnosis.
- No automatic repairs, process termination, system configuration changes, or privileged drivers.
- Results are operating-condition hypotheses, not proof of damaged hardware or proof of causation.
