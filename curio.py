"""CURIO: local, session-based computer diagnosis."""
from __future__ import annotations

import json
import os
import platform
import socket
import sqlite3
import statistics
import threading
import time
import uuid
import csv
import io
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psutil

ROOT = Path(__file__).parent
DB_PATH = ROOT / "curio.db"
MODEL_PATH = ROOT / "curio_model.joblib"
PORT = int(os.environ.get("CURIO_PORT", "8765"))
SAMPLE_SECONDS = 1
SESSION_SECONDS = 30
WINDOW_SECONDS = 5
SAMPLES_PER_WINDOW = WINDOW_SECONDS // SAMPLE_SECONDS
DB_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
DIAG = {"id": None, "running": False, "progress": 0, "phase": "idle", "startedAt": None, "result": None, "error": None}
CLASSIFIER = None
ANOMALY_MODEL = None
MODEL_INFO = {"available": False}

FEATURES = [
    "cpu_mean", "cpu_max", "cpu_std", "cpu_trend", "cpu_freq_mean_mhz", "cpu_freq_trend_mhz",
    "memory_mean", "memory_max", "available_min_gb", "available_pct_min", "memory_trend", "swap_used_gb", "swap_pct_mean",
    "disk_read_mbps", "disk_write_mbps", "disk_busy_pct", "disk_latency_ms",
    "network_rx_mbps", "network_tx_mbps", "temperature_max_c", "process_count", "process_growth_mb_per_sec",
]
FEATURE_NAMES = {
    "cpu_mean": "Average CPU use", "cpu_max": "Peak CPU use", "cpu_std": "CPU variation", "cpu_trend": "CPU change",
    "cpu_freq_mean_mhz": "Average CPU frequency", "cpu_freq_trend_mhz": "CPU frequency change",
    "memory_mean": "Average memory use", "memory_max": "Peak memory use", "available_min_gb": "Minimum available memory",
    "available_pct_min": "Available RAM share", "memory_trend": "Memory growth", "swap_used_gb": "Page file in use", "swap_pct_mean": "Page file use",
    "disk_read_mbps": "Disk read rate",
    "disk_write_mbps": "Disk write rate", "disk_busy_pct": "Disk busy time", "disk_latency_ms": "Disk response time",
    "network_rx_mbps": "Network receive rate", "network_tx_mbps": "Network send rate", "temperature_max_c": "Peak reported temperature",
    "process_count": "Running process count", "process_growth_mb_per_sec": "Sampled process memory growth",
}
ISSUE_LABELS = {
    "cpu_saturation": "CPU saturation", "memory_pressure": "Memory pressure",
    "disk_io_bottleneck": "Disk I/O bottleneck", "thermal_stress": "Thermal stress",
    "network_activity": "High network activity", "normal": "No sustained pressure observed",
}


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextmanager
def connect_db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with DB_LOCK, connect_db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL, label TEXT NOT NULL, source TEXT NOT NULL,
            summary TEXT NOT NULL, result_json TEXT NOT NULL, windows_json TEXT NOT NULL)""")


def load_model():
    global CLASSIFIER, ANOMALY_MODEL, MODEL_INFO
    if not MODEL_PATH.exists():
        MODEL_INFO = {"available": False, "message": "No trained model is installed. This run will use transparent signal rules."}
        return
    try:
        import joblib
        artifact = joblib.load(MODEL_PATH)
        if artifact.get("feature_names") != FEATURES:
            MODEL_INFO = {"available": False, "message": "The model feature version does not match this collector."}
            return
        CLASSIFIER = artifact.get("classifier")
        ANOMALY_MODEL = artifact.get("anomaly_model")
        MODEL_INFO = {"available": CLASSIFIER is not None, "message": "Trained local model loaded.",
                      "trainedAt": artifact.get("trained_at"), "metrics": artifact.get("metrics", {})}
    except Exception as exc:
        MODEL_INFO = {"available": False, "message": f"Could not load the local model: {exc}"}


def disk_and_network_counters():
    disk = psutil.disk_io_counters()
    net = psutil.net_io_counters()
    return disk, net


def system_sample(previous, previous_processes, elapsed):
    """Capture privacy-minimized technical readings; process names never enter stored features."""
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu = psutil.cpu_percent(interval=None)
    freq = psutil.cpu_freq()
    disk, net = disk_and_network_counters()
    def rate(current, old, field):
        if current is None or old is None:
            return None
        return max(0, getattr(current, field, 0) - getattr(old, field, 0)) / max(elapsed, .01)
    read_rate = rate(disk, previous[0], "read_bytes")
    write_rate = rate(disk, previous[0], "write_bytes")
    read_ops = rate(disk, previous[0], "read_count")
    write_ops = rate(disk, previous[0], "write_count")
    read_ms = rate(disk, previous[0], "read_time")
    write_ms = rate(disk, previous[0], "write_time")
    total_ops = (read_ops or 0) + (write_ops or 0)
    latency = ((read_ms or 0) + (write_ms or 0)) / total_ops if total_ops > 0 else None
    busy = min(100, ((read_ms or 0) + (write_ms or 0)) / max(elapsed * 1000, 1) * 100) if read_ms is not None or write_ms is not None else None
    rx = rate(net, previous[1], "bytes_recv")
    tx = rate(net, previous[1], "bytes_sent")
    temperatures = []
    try:
        for entries in (psutil.sensors_temperatures() or {}).values():
            temperatures.extend(item.current for item in entries if item.current is not None)
    except (AttributeError, OSError):
        pass
    logical = psutil.cpu_count() or 1
    proc_rows, process_map = [], {}
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
        try:
            info = proc.info
            name = info.get("name") or "Unknown"
            if name.lower() in ("system idle process", "system"):
                continue
            cpu_pct = (info.get("cpu_percent") or 0) / logical
            rss = info.get("memory_info").rss if info.get("memory_info") else 0
            proc_rows.append({"name": name, "cpu": round(cpu_pct, 1), "memory_mb": round(rss / 1048576)})
            # PID is only used in-memory to calculate changes; it is not persisted.
            process_map[info["pid"]] = rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
            continue
    top_cpu = sorted(proc_rows, key=lambda p: p["cpu"], reverse=True)[:4]
    top_memory = sorted(proc_rows, key=lambda p: p["memory_mb"], reverse=True)[:4]
    growth = sum(max(0, process_map[k] - previous_processes.get(k, process_map[k])) for k in process_map) / 1048576
    return ({"cpu": cpu, "memory": mem.percent, "available_gb": mem.available / 1073741824,
             "available_pct": mem.available / max(mem.total, 1) * 100,
             "swap_used_gb": swap.used / 1073741824, "swap_pct": swap.percent, "swap_in_bytes": getattr(swap, "sin", None),
             "swap_out_bytes": getattr(swap, "sout", None), "read_mbps": (read_rate or 0) / 1048576,
             "write_mbps": (write_rate or 0) / 1048576, "disk_busy_pct": busy, "disk_latency_ms": latency,
             "rx_mbps": (rx or 0) * 8 / 1_000_000, "tx_mbps": (tx or 0) * 8 / 1_000_000,
             "temperature": max(temperatures) if temperatures else None, "frequency_mhz": freq.current if freq else None,
             "process_count": len(proc_rows), "process_memory_growth_mb": growth,
             "top_cpu": top_cpu, "top_memory": top_memory},
            (disk, net), process_map)


def mean(values):
    clean = [float(v) for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def feature_window(samples):
    cpus = [x["cpu"] for x in samples]
    mems = [x["memory"] for x in samples]
    temps = [x["temperature"] for x in samples if x["temperature"] is not None]
    def last_delta(key):
        values = [x[key] for x in samples if x[key] is not None]
        return values[-1] - values[0] if len(values) > 1 else 0.0
    return {
        "start": samples[0]["time"], "end": samples[-1]["time"],
        "cpu_mean": mean(cpus), "cpu_max": max(cpus), "cpu_std": statistics.pstdev(cpus) if len(cpus) > 1 else 0,
        "cpu_trend": cpus[-1] - cpus[0], "cpu_freq_mean_mhz": mean([x["frequency_mhz"] for x in samples]),
        "cpu_freq_trend_mhz": last_delta("frequency_mhz"), "memory_mean": mean(mems), "memory_max": max(mems),
        "available_min_gb": min(x["available_gb"] for x in samples), "available_pct_min": min(x["available_pct"] for x in samples),
        "memory_trend": mems[-1] - mems[0], "swap_used_gb": max(x["swap_used_gb"] for x in samples),
        "swap_pct_mean": mean([x["swap_pct"] for x in samples]),
        "disk_read_mbps": mean([x["read_mbps"] for x in samples]), "disk_write_mbps": mean([x["write_mbps"] for x in samples]),
        "disk_busy_pct": mean([x["disk_busy_pct"] for x in samples]), "disk_latency_ms": mean([x["disk_latency_ms"] for x in samples]),
        "network_rx_mbps": mean([x["rx_mbps"] for x in samples]), "network_tx_mbps": mean([x["tx_mbps"] for x in samples]),
        "temperature_max_c": max(temps) if temps else None, "process_count": round(mean([x["process_count"] for x in samples]) or 0),
        "process_growth_mb_per_sec": mean([x["process_memory_growth_mb"] for x in samples]),
        "top_cpu": samples[-1]["top_cpu"], "top_memory": samples[-1]["top_memory"],
        "process_memory_growth_mb": mean([x["process_memory_growth_mb"] for x in samples]),
    }


def aggregate_run(windows):
    out = {}
    for name in FEATURES:
        vals = [w.get(name) for w in windows if w.get(name) is not None]
        out[name] = mean(vals)
    out["cpu_max"] = max([w["cpu_max"] for w in windows if w.get("cpu_max") is not None], default=None)
    out["memory_max"] = max([w["memory_max"] for w in windows if w.get("memory_max") is not None], default=None)
    out["available_min_gb"] = min([w["available_min_gb"] for w in windows if w.get("available_min_gb") is not None], default=None)
    out["temperature_max_c"] = max([w["temperature_max_c"] for w in windows if w.get("temperature_max_c") is not None], default=None)
    first_memory, last_memory = (windows[0].get("memory_mean"), windows[-1].get("memory_mean")) if windows else (None, None)
    out["memory_trend"] = (last_memory - first_memory) if len(windows) > 1 and first_memory is not None and last_memory is not None else 0
    return out


def make_rule_scores(f):
    def ramp(x, start, end):
        return max(0.0, min(1.0, (float(x) - start) / (end - start))) if x is not None else 0.0
    scores = {
        "cpu_saturation": max(ramp(f.get("cpu_mean"), 70, 95), ramp(f.get("cpu_max"), 85, 100) * .8),
        "memory_pressure": max(ramp(f.get("memory_mean"), 80, 97), ramp(f.get("memory_max"), 90, 100),
                                ramp(15 - (f.get("available_pct_min") if f.get("available_pct_min") is not None else 15), 5, 13) * .9,
                                ramp(f.get("swap_pct_mean"), 50, 95) * .8),
        "disk_io_bottleneck": min(ramp(f.get("disk_busy_pct"), 65, 100), ramp(f.get("disk_latency_ms"), 12, 45)),
        "thermal_stress": min(ramp(f.get("temperature_max_c"), 75, 100), max(ramp(f.get("cpu_mean"), 60, 95), .1)),
    }
    return scores


def explain_issue(label, f, windows):
    ev, actions = [], []
    if label == "cpu_saturation":
        ev.append(f"CPU averaged {f['cpu_mean']:.0f}% and peaked at {f['cpu_max']:.0f}% during the check.")
        top = max((p for w in windows for p in (w.get("top_cpu") or [])), key=lambda p: p["cpu"], default=None)
        if top:
            ev.append(f"{top['name']} was the busiest sampled process ({top['cpu']:.1f}% of total machine capacity at the last sample).")
        actions.append("Check whether the busiest process is expected to be active; compare again after it settles.")
    elif label == "memory_pressure":
        ev.append(f"Memory use averaged {f['memory_mean']:.0f}% and reached {f['memory_max']:.0f}%.")
        if f.get("available_min_gb") is not None:
            ev.append(f"Available RAM fell as low as {f['available_min_gb']:.1f} GB ({f['available_pct_min']:.0f}% of installed memory).")
        if f.get("swap_pct_mean") is not None and f["swap_pct_mean"] > 0:
            ev.append(f"Page-file use averaged {f['swap_pct_mean']:.0f}%; this is context, not proof of active paging.")
        if f.get("memory_trend") is not None:
            ev.append(f"Memory changed {f['memory_trend']:+.1f} percentage points across the session.")
        top = max((p for w in windows for p in (w.get("top_memory") or [])), key=lambda p: p["memory_mb"], default=None)
        if top:
            ev.append(f"{top['name']} was the largest sampled process at {top['memory_mb']:,} MB.")
        actions.append("Review the largest memory users. A single short check cannot tell whether growth will continue.")
    elif label == "disk_io_bottleneck":
        ev.append(f"Disk was busy for about {f['disk_busy_pct']:.0f}% of measured time.")
        if f.get("disk_latency_ms") is not None:
            ev.append(f"Average service time was approximately {f['disk_latency_ms']:.1f} ms per I/O operation.")
        ev.append(f"Read/write throughput averaged {f['disk_read_mbps']:.1f}/{f['disk_write_mbps']:.1f} MB/s.")
        actions.append("Repeat the check while the slowdown is happening and inspect disk-active applications.")
    elif label == "thermal_stress":
        ev.append(f"A reported sensor reached {f['temperature_max_c']:.0f}°C while CPU use averaged {(f.get('cpu_mean') or 0):.0f}%.")
        actions.append("Check airflow and fans. CURIO can report temperature, but cannot confirm CPU throttling on this device.")
    elif label == "network_activity":
        ev.append(f"Network throughput averaged {f['network_rx_mbps']:.1f} Mbps receive and {f['network_tx_mbps']:.1f} Mbps send.")
        actions.append("This is activity only. CURIO does not measure latency or packet loss, so it cannot diagnose a network fault.")
    else:
        ev.append("No sustained CPU, memory, disk-I/O, or reported thermal pressure crossed CURIO's screening thresholds during this check.")
        actions.append("If the slowdown comes and goes, repeat Quick Diagnosis while it is happening. Longer Deep Diagnosis mode is future scope.")
    return ev, actions


def analyze(windows):
    features = aggregate_run(windows)
    rule_scores = make_rule_scores(features)
    sorted_rules = sorted(rule_scores.items(), key=lambda kv: kv[1], reverse=True)
    rule_label, rule_strength = sorted_rules[0] if sorted_rules else ("normal", 0)
    rule_label = rule_label if rule_strength >= .28 else "normal"
    model_result = None
    if CLASSIFIER is not None:
        try:
            rows = [[float(w[name]) if w.get(name) is not None else float("nan") for name in FEATURES] for w in windows]
            predictions = CLASSIFIER.predict(rows)
            labels = [str(x) for x in predictions]
            model_label = max(set(labels), key=labels.count)
            model_result = {"label": model_label, "available": True, "training": MODEL_INFO.get("metrics", {})}
            label = model_label
            source = "trained_model"
        except Exception:
            label, source = rule_label, "transparent_rules"
    else:
        label, source = rule_label, "transparent_rules"
    if label not in ISSUE_LABELS:
        label = rule_label
    evidence, actions = explain_issue(label, features, windows)
    anomaly = None
    if ANOMALY_MODEL is not None:
        try:
            rows = [[float(w[name]) if w.get(name) is not None else float("nan") for name in FEATURES] for w in windows]
            anomaly = {"outlier_windows": int(sum(ANOMALY_MODEL.predict(rows) == -1)), "total_windows": len(rows),
                       "message": "Compared with controlled normal-state training sessions."}
        except Exception:
            pass
    label = label if label in ISSUE_LABELS else "normal"
    status = "pressure_detected" if label != "normal" else "no_pressure_observed"
    other_signals = [{"title": ISSUE_LABELS[k], "strength": round(float(v), 2)} for k, v in sorted_rules
                     if k != label and v >= .28]
    if other_signals:
        evidence.append("Other elevated screening signals: " + ", ".join(x["title"] for x in other_signals) + ". These may overlap with the leading signal.")
    return {"label": label, "labelTitle": ISSUE_LABELS[label], "status": status, "source": source,
            "ruleStrength": round(float(rule_strength), 2), "ruleScores": {ISSUE_LABELS[k]: round(float(v), 2) for k, v in rule_scores.items()},
            "model": model_result, "otherSignals": other_signals, "anomaly": anomaly, "features": features, "evidence": evidence,
            "actions": actions, "windows": windows, "limitations": [
                "A 30-second screening can miss intermittent problems; rerun while the issue is happening.",
                "A likely operating condition is not proof of a damaged component.",
                "No trained classification model is installed." if CLASSIFIER is None else "This estimate is only as representative as its controlled training data.",
            ]}


def run_diagnosis(session_id):
    global DIAG
    started = utcnow()
    samples, windows, chunk = [], [], []
    prev_counters = (None, None)
    prev_processes = {}
    last = time.monotonic()
    try:
        # Prime interval counters to avoid an invalid first delta.
        psutil.cpu_percent(interval=None)
        prev_counters = disk_and_network_counters()
        deadline = time.monotonic() + SESSION_SECONDS
        total = SESSION_SECONDS // SAMPLE_SECONDS
        while time.monotonic() < deadline:
            tick = time.monotonic()
            elapsed = max(tick - last, .01)
            raw, counters, processes = system_sample(prev_counters, prev_processes, elapsed)
            stamp = utcnow()
            raw["time"] = stamp
            samples.append(raw)
            chunk.append(raw)
            prev_counters, prev_processes, last = counters, processes, tick
            if len(chunk) >= SAMPLES_PER_WINDOW:
                windows.append(feature_window(chunk)); chunk = []
            with STATE_LOCK:
                DIAG.update({"progress": min(96, int(len(samples) / total * 96)), "phase": "collecting",
                             "samples": len(samples), "windowCount": len(windows), "latest": raw})
            time.sleep(max(0, SAMPLE_SECONDS - (time.monotonic() - tick)))
        if chunk:
            windows.append(feature_window(chunk))
        with STATE_LOCK:
            DIAG.update({"progress": 98, "phase": "analyzing"})
        result = analyze(windows)
        ended = utcnow()
        summary = result["evidence"][0] if result["evidence"] else "Analysis complete."
        with DB_LOCK, connect_db() as con:
            con.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (session_id, started, ended, SESSION_SECONDS, result["label"], result["source"], summary,
                         json.dumps(result), json.dumps(windows)))
        with STATE_LOCK:
            DIAG.update({"running": False, "progress": 100, "phase": "complete", "result": result, "endedAt": ended})
    except Exception as exc:
        with STATE_LOCK:
            DIAG.update({"running": False, "phase": "error", "error": str(exc)})


def start_diagnosis():
    with STATE_LOCK:
        if DIAG["running"]:
            return None
        session_id = uuid.uuid4().hex
        DIAG.update({"id": session_id, "running": True, "progress": 0, "phase": "preparing",
                     "startedAt": utcnow(), "result": None, "error": None, "samples": 0, "windowCount": 0, "latest": None})
    threading.Thread(target=run_diagnosis, args=(session_id,), daemon=True).start()
    return session_id


def get_history():
    with DB_LOCK, connect_db() as con:
        rows = con.execute("SELECT id,started_at,ended_at,duration_seconds,label,source,summary FROM sessions ORDER BY started_at DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]


def get_session_count():
    with DB_LOCK, connect_db() as con:
        return con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


def export_windows_csv(handler):
    output = io.StringIO(newline="")
    columns = ["session_id", "window_index", "window_start", "window_end", "machine_id", "label", "suggested_label", *FEATURES]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    with DB_LOCK, connect_db() as con:
        rows = con.execute("SELECT id,label,windows_json FROM sessions ORDER BY started_at").fetchall()
    for row in rows:
        for index, window in enumerate(json.loads(row["windows_json"])):
            values = {key: (window.get(key) if window.get(key) is not None else "") for key in FEATURES}
            writer.writerow({"session_id": row["id"], "window_index": index, "window_start": window.get("start"),
                             "window_end": window.get("end"), "machine_id": "", "label": "",
                             "suggested_label": row["label"], **values})
    body = output.getvalue().encode("utf-8-sig")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/csv; charset=utf-8")
    handler.send_header("Content-Disposition", 'attachment; filename="curio_research_windows.csv"')
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def parse_uploaded_csv(file_text):
    if not isinstance(file_text, str) or not file_text.strip():
        raise ValueError("The file is empty.")
    if len(file_text.encode("utf-8")) > 10 * 1024 * 1024:
        raise ValueError("CSV files must be 10 MB or smaller.")
    try:
        reader = csv.DictReader(io.StringIO(file_text.lstrip("\ufeff")))
        raw_headers = reader.fieldnames or []
        headers = [str(h or "").strip() for h in raw_headers]
        if not headers or any(not h for h in headers):
            raise ValueError("The CSV needs a header row with column names.")
        if len(headers) > 80 or len(set(headers)) != len(headers):
            raise ValueError("Use at most 80 uniquely named columns.")
        rows = []
        for raw in reader:
            if not raw or all(not str(v or "").strip() for v in raw.values()):
                continue
            if len(rows) >= 25000:
                raise ValueError("This local analyzer supports up to 25,000 rows per CSV.")
            rows.append({name: str(raw.get(original) or "").strip() for name, original in zip(headers, raw_headers)})
    except csv.Error as exc:
        raise ValueError(f"Could not read this CSV: {exc}") from exc
    if not rows:
        raise ValueError("The CSV has headers but no data rows.")
    columns = []
    numeric_cache = {}
    for name in headers:
        values, blank = [], 0
        for row in rows:
            raw = row[name]
            try:
                number = float(raw)
                if not math.isfinite(number): raise ValueError()
                values.append(number)
            except (ValueError, TypeError):
                if not raw: blank += 1
        numeric = len(values) >= max(3, int(len(rows) * .65))
        missing = len(rows) - len(values) if numeric else blank
        if numeric: numeric_cache[name] = [as_number(row[name]) for row in rows]
        else: numeric_cache[name] = None
        categories = []
        if not numeric:
            counts = {}
            for row in rows:
                if row[name]: counts[row[name]] = counts.get(row[name], 0) + 1
            categories = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:12]
        columns.append({"name": name, "kind": "numeric" if numeric else "category/text", "missing": missing,
                        "missingPct": round(missing / len(rows) * 100, 1), "unique": len(set(v for v in (numeric_cache[name] if numeric else [r[name] for r in rows]) if v is not None and v != "")),
                        "categories": [{"value": k, "count": v} for k, v in categories]})
    return {"headers": headers, "rows": rows, "columns": columns, "numeric": numeric_cache}


def as_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def normalize_csv_name(value):
    return "".join(char for char in str(value).lower() if char.isalnum())


def preview_dataset(file_text, filename="dataset.csv"):
    parsed = parse_uploaded_csv(file_text)
    target_guess = next((name for name in parsed["headers"] if normalize_csv_name(name) in ("label", "class", "diagnosis", "target", "condition", "outcome", "groundtruth")), "")
    group_guess = next((name for name in parsed["headers"] if normalize_csv_name(name) in ("sessionid", "session", "runid", "run", "trialid", "experimentid")), "")
    return {"filename": Path(filename).name[:140], "rows": len(parsed["rows"]), "columns": parsed["columns"],
            "headers": parsed["headers"], "targetGuess": target_guess, "groupGuess": group_guess,
            "preview": [{k: v[:80] for k, v in row.items()} for row in parsed["rows"][:6] ],
            "note": "The CSV stays in this browser tab and the local CURIO server's memory while you work; it is not saved to history or sent to a remote service."}


def analyze_uploaded_dataset(file_text, filename, target_name, group_name):
    parsed = parse_uploaded_csv(file_text)
    headers, rows, numeric = parsed["headers"], parsed["rows"], parsed["numeric"]
    if target_name and target_name not in headers: raise ValueError("Selected target column is not in this file.")
    if group_name and group_name not in headers: raise ValueError("Selected session/group column is not in this file.")
    class_counts = {}
    if target_name:
        for row in rows:
            value = row.get(target_name, "") or "(missing)"
            class_counts[value] = class_counts.get(value, 0) + 1
    feature_columns = [c["name"] for c in parsed["columns"] if c["kind"] == "numeric" and c["name"] != target_name and c["name"] != group_name]
    excluded = {target_name, group_name}
    excluded_names = {normalize_csv_name(x) for x in ("suggested_label", "label", "target", "class", "diagnosis", "condition", "session_id", "session", "run_id", "run", "trial_id", "machine_id", "window_index", "window_start", "window_end", "timestamp", "time", "date", "pid", "id")}
    feature_columns = [c for c in feature_columns if normalize_csv_name(c) not in excluded_names and not normalize_csv_name(c).endswith("id")]
    stats_rows = []
    for name in feature_columns:
        vals = [v for v in numeric[name] if v is not None]
        if not vals: continue
        stats_rows.append({"name": name, "missingPct": round((len(rows)-len(vals))/len(rows)*100,1),
                           "mean": round(statistics.mean(vals),4), "median": round(statistics.median(vals),4),
                           "min": round(min(vals),4), "max": round(max(vals),4), "unique": len(set(vals))})
    warnings = []
    if not target_name: warnings.append("No target/label selected: CURIO will profile the data but will not train or score a classifier.")
    if target_name and class_counts.get("(missing)"): warnings.append("Rows with a missing target are excluded from model evaluation.")
    if "suggested_label" in headers: warnings.append("suggested_label is excluded from model inputs to prevent target leakage. Use independently assigned labels as the target.")
    evaluation = None
    group_counts = {}
    if target_name and group_name and len(feature_columns) and len(class_counts) >= 2 and "(missing)" not in class_counts:
        labels = [r[target_name] for r in rows]
        groups = [r[group_name] for r in rows]
        valid_labels = sorted(set(labels))
        group_counts = {label: len(set(groups[i] for i, value in enumerate(labels) if value == label)) for label in valid_labels}
        if len(valid_labels) > 15:
            warnings.append("The selected target has more than 15 distinct values; it looks continuous or too granular for this classifier.")
        elif min(group_counts.values(), default=0) < 3:
            warnings.append("Leakage-safe evaluation needs at least 3 independent sessions per class. This dataset has too few for one or more classes.")
        else:
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.impute import SimpleImputer
            from sklearn.inspection import permutation_importance
            from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score
            from sklearn.model_selection import GroupShuffleSplit
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import StandardScaler
            X = [[numeric[col][i] if numeric[col][i] is not None else float("nan") for col in feature_columns] for i in range(len(rows))]
            split = next(((tr, te) for tr, te in GroupShuffleSplit(n_splits=500, test_size=.34, random_state=23).split(X, labels, groups)
                          if set(labels[i] for i in tr) == set(valid_labels) and set(labels[i] for i in te) == set(valid_labels)), None)
            if split is None:
                warnings.append("No session-held-out split contained every class in both train and test. Collect more independent sessions per class.")
            else:
                train, test = split
                model = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(),
                                      RandomForestClassifier(n_estimators=180, min_samples_leaf=2, class_weight="balanced_subsample", random_state=23))
                model.fit([X[i] for i in train], [labels[i] for i in train])
                actual, predicted = [labels[i] for i in test], model.predict([X[i] for i in test])
                class_order = sorted(set(labels))
                report = classification_report(actual, predicted, labels=class_order, output_dict=True, zero_division=0)
                evaluation = {"accuracy": round(accuracy_score(actual, predicted),3), "macroF1": round(f1_score(actual, predicted, average="macro"),3),
                              "report": {k: {m: round(float(v),3) for m,v in val.items()} if isinstance(val,dict) else round(float(val),3) for k,val in report.items()},
                              "classes": class_order, "confusion": confusion_matrix(actual,predicted,labels=class_order).tolist(),
                              "trainSessions": len(set(groups[i] for i in train)), "testSessions": len(set(groups[i] for i in test)),
                              "split": f"GroupShuffleSplit by {group_name}; no session appears in both sides."}
                try:
                    importance = permutation_importance(model,[X[i] for i in test],actual,n_repeats=3,random_state=23,scoring="accuracy")
                    evaluation["importance"] = [{"name": feature_columns[i], "score": round(float(importance.importances_mean[i]),4)}
                                                 for i in sorted(range(len(feature_columns)),key=lambda j:importance.importances_mean[j],reverse=True)[:8]]
                except Exception:
                    evaluation["importance"] = []
    elif target_name and not group_name:
        warnings.append("No session/run grouping column was selected. CURIO will not make a model evaluation using a random row split, because neighboring telemetry rows can leak across train and test.")
    missing_columns = sorted([c["name"] for c in parsed["columns"] if c["missingPct"]>=25],key=lambda n:next(c["missingPct"] for c in parsed["columns"] if c["name"]==n),reverse=True)
    if missing_columns: warnings.append("Some columns are at least 25% missing: " + ", ".join(missing_columns[:5]) + ".")
    if len(feature_columns)<2: warnings.append("Fewer than two eligible numeric features were found; check the column mapping or file format.")
    # Send each telemetry run through the same signal scoring and explanation pipeline
    # used by Quick Diagnosis. Dataset labels are intentionally not used as predictions.
    aliases = {
        "cpu_mean": ("cpu_mean", "cpu_percent", "cpu_pct", "cpu_usage_percent", "cpu_usage"),
        "cpu_max": ("cpu_max", "cpu_peak", "cpu_mean", "cpu_percent", "cpu_pct", "cpu_usage_percent", "cpu_usage"),
        "cpu_std": ("cpu_std", "cpu_variation"), "cpu_trend": ("cpu_trend", "cpu_change"),
        "cpu_freq_mean_mhz": ("cpu_freq_mean_mhz", "cpu_frequency_mhz", "cpu_freq_mhz"),
        "cpu_freq_trend_mhz": ("cpu_freq_trend_mhz",),
        "memory_mean": ("memory_mean", "memory_percent", "memory_pct", "ram_percent", "ram_pct", "memory_usage_percent", "memory_usage"),
        "memory_max": ("memory_max", "memory_peak", "memory_mean", "memory_percent", "memory_pct", "ram_percent", "ram_pct"),
        "available_min_gb": ("available_min_gb", "available_ram_gb", "available_memory_gb", "free_memory_gb"),
        "available_pct_min": ("available_pct_min", "available_memory_pct", "free_memory_pct"),
        "memory_trend": ("memory_trend", "memory_change"), "swap_used_gb": ("swap_used_gb", "pagefile_used_gb"),
        "swap_pct_mean": ("swap_pct_mean", "swap_percent", "pagefile_percent"),
        "disk_read_mbps": ("disk_read_mbps", "disk_read_mb_s"), "disk_write_mbps": ("disk_write_mbps", "disk_write_mb_s"),
        "disk_busy_pct": ("disk_busy_pct", "disk_utilization_pct", "disk_active_pct"),
        "disk_latency_ms": ("disk_latency_ms", "disk_response_ms", "disk_read_latency_ms"),
        "network_rx_mbps": ("network_rx_mbps", "network_receive_mbps", "network_download_mbps"),
        "network_tx_mbps": ("network_tx_mbps", "network_send_mbps", "network_upload_mbps"),
        "temperature_max_c": ("temperature_max_c", "cpu_temperature_c", "temperature_c", "temperature"),
        "process_count": ("process_count", "running_processes"),
        "process_growth_mb_per_sec": ("process_growth_mb_per_sec", "memory_growth_mb_s"),
    }
    by_lower = {normalize_csv_name(name): name for name in headers}
    def mapped_value(source_row, feature):
        for alias in aliases[feature]:
            actual = by_lower.get(normalize_csv_name(alias))
            if actual is not None:
                return as_number(source_row.get(actual))
        return None
    grouped_rows = {}
    for row in rows:
        key = (row.get(group_name) or "(missing group)") if group_name else "Entire CSV"
        grouped_rows.setdefault(key, []).append(row)
    diagnosis_runs = []
    max_runs, max_windows = 40, 600
    for key, run_rows in list(grouped_rows.items())[:max_runs]:
        mapped = []
        for index, source_row in enumerate(run_rows[:max_windows]):
            window = {name: mapped_value(source_row, name) for name in FEATURES}
            if window["cpu_max"] is None: window["cpu_max"] = window["cpu_mean"]
            if window["memory_max"] is None: window["memory_max"] = window["memory_mean"]
            window["start"] = source_row.get(by_lower.get("windowstart", ""), source_row.get(by_lower.get("timestamp", ""), ""))
            window["end"] = source_row.get(by_lower.get("windowend", ""), window["start"])
            window["top_cpu"], window["top_memory"] = [], []
            mapped.append(window)
        signal_groups = (("CPU", ("cpu_mean", "cpu_max")),
                         ("Memory", ("memory_mean", "memory_max", "available_pct_min")),
                         ("Disk", ("disk_busy_pct", "disk_latency_ms")),
                         ("Temperature", ("temperature_max_c",)))
        available_signals = [name for name, keys in signal_groups if any(w.get(k) is not None for w in mapped for k in keys)]
        if not available_signals:
            diagnosis_runs.append({"group": str(key), "windowCount": len(mapped), "result": None,
                                   "reason": "No recognized CPU, memory, disk, or temperature telemetry columns were found."})
            continue
        # Sort by a numeric window index when present, otherwise retain CSV order.
        index_column = by_lower.get("windowindex")
        if index_column:
            mapped = [item for _, item in sorted(zip([as_number(r.get(index_column)) or i for i, r in enumerate(run_rows[:max_windows])], mapped), key=lambda pair: pair[0])]
        try:
            result = analyze(mapped)
            diagnosis_runs.append({"group": str(key), "windowCount": len(mapped), "signalGroups": available_signals,
                                   "result": result})
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            diagnosis_runs.append({"group": str(key), "windowCount": len(mapped), "result": None,
                                   "reason": f"Could not create a diagnosis from this run: {exc}"})
    if len(grouped_rows) > max_runs:
        warnings.append(f"The file contains {len(grouped_rows)} runs; showing diagnoses for the first {max_runs} only.")
    if any(len(run_rows) > max_windows for run_rows in grouped_rows.values()):
        warnings.append(f"Runs are limited to their first {max_windows} rows for diagnosis display.")
    if not any(run.get("result") for run in diagnosis_runs):
        warnings.append("Diagnosis requires recognizable telemetry columns such as cpu_mean/memory_mean/disk_busy_pct, or common CPU and RAM percentage columns.")
    return {"filename": Path(filename).name[:140], "rows": len(rows), "columnCount": len(headers), "featureCount": len(feature_columns),
            "target": target_name or None, "group": group_name or None, "classCounts": class_counts,
            "classGroupCounts": group_counts if target_name and group_name and len(class_counts)>=2 else {},
            "featureStats": stats_rows, "missingColumns": missing_columns, "evaluation": evaluation, "diagnoses": diagnosis_runs,
            "warnings": warnings, "stored": False,
            "privacy": "Analyzed only in this browser tab and local server memory; the CSV is not saved to disk. Click Clear or close this tab to release the browser copy."}


def discover_patterns():
    history = get_history()
    abnormal = [x for x in history if x["label"] != "normal"]
    normal = [x for x in history if x["label"] == "normal"]
    if len(abnormal) < 3 or len(normal) < 3:
        return {"ready": False, "patterns": [], "abnormalSessions": len(abnormal), "normalSessions": len(normal),
                "required": "Collect at least 3 separate pressure sessions and 3 no-pressure sessions. Patterns are compared across session starts and ends."}
    with DB_LOCK, connect_db() as con:
        rows = con.execute("SELECT label,windows_json FROM sessions ORDER BY started_at DESC LIMIT 200").fetchall()
    patterns = []
    # Discover candidate directional changes from the first and last thirds of each independent run.
    candidates = ["cpu_mean", "memory_mean", "available_pct_min", "swap_pct_mean", "disk_busy_pct", "disk_latency_ms", "temperature_max_c", "process_count", "process_growth_mb_per_sec", "cpu_freq_mean_mhz"]
    for feature in candidates:
        high_abnormal, high_normal = 0, 0
        n_abnormal, n_normal = 0, 0
        for row in rows:
            ws = json.loads(row["windows_json"])
            vals = [w.get(feature) for w in ws if w.get(feature) is not None]
            if len(vals) < 4:
                continue
            edge = max(1, len(vals) // 3)
            change = mean(vals[-edge:]) - mean(vals[:edge])
            # Require both a relative change and a practical floor, avoiding tiny measurement noise.
            scale = max(abs(mean(vals[:edge]) or 0), 1)
            increasing = change > max(scale * .18, {"cpu_mean": 8, "memory_mean": 5, "available_min_gb": 1,
                "available_pct_min": 3, "swap_pct_mean": 5, "disk_busy_pct": 12, "disk_latency_ms": 5, "temperature_max_c": 3,
                "process_count": 3}.get(feature, 1))
            decreasing = change < -max(scale * .18, {"available_pct_min": 3}.get(feature, 0))
            candidate = increasing if feature != "available_pct_min" else decreasing
            if row["label"] == "normal":
                n_normal += 1; high_normal += int(candidate)
            else:
                n_abnormal += 1; high_abnormal += int(candidate)
        if n_abnormal >= 3 and n_normal >= 3:
            rate_a, rate_n = high_abnormal / n_abnormal, high_normal / n_normal
            lift = rate_a / max(rate_n, .05)
            if high_abnormal >= 2 and rate_a >= .6 and lift >= 1.5:
                direction = "fell" if feature == "available_pct_min" else "rose"
                patterns.append({"feature": FEATURE_NAMES.get(feature, feature), "direction": direction,
                                 "support": high_abnormal, "abnormalRuns": n_abnormal, "normalRuns": n_normal,
                                 "abnormalRate": round(rate_a, 2), "normalRate": round(rate_n, 2), "lift": round(lift, 1),
                                 "text": f"{FEATURE_NAMES.get(feature, feature)} {direction} in {high_abnormal} of {n_abnormal} pressure sessions, compared with {high_normal} of {n_normal} no-pressure sessions."})
    patterns.sort(key=lambda p: (p["lift"], p["support"]), reverse=True)
    return {"ready": True, "patterns": patterns[:5], "abnormalSessions": len(abnormal), "normalSessions": len(normal),
            "method": "Exploratory early-to-late feature trend support and lift across independent diagnostic sessions.",
            "caveat": "These are recurring associations, not proof of cause. CURIO needs more sessions to test stability.",
            "required": "At least 3 sessions per comparison group; more runs make the comparison less fragile."}


def json_response(handler, payload, status=200):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'")
        self.end_headers_original()

    end_headers_original = SimpleHTTPRequestHandler.end_headers

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            with STATE_LOCK:
                diag = {k: v for k, v in DIAG.items() if k not in ("latest", "samples")}
            return json_response(self, {"system": {"name": socket.gethostname(), "os": platform.system(), "release": platform.release()},
                                        "model": MODEL_INFO, "diagnosis": diag, "history": get_history()[:5], "sessionCount": get_session_count()})
        if parsed.path == "/api/diagnosis":
            with STATE_LOCK:
                diag = dict(DIAG)
            if diag.get("latest"):
                latest = diag["latest"]
                # Keep the response process names for the live progress display; do not persist them.
                diag["liveSummary"] = {"cpu": latest["cpu"], "memory": latest["memory"], "availableGb": latest["available_gb"],
                                       "readMbps": latest["read_mbps"], "writeMbps": latest["write_mbps"]}
            return json_response(self, diag)
        if parsed.path == "/api/history":
            return json_response(self, {"sessions": get_history()})
        if parsed.path == "/api/session":
            sid = parse_qs(parsed.query).get("id", [""])[0]
            with DB_LOCK, connect_db() as con:
                row = con.execute("SELECT result_json FROM sessions WHERE id=?", (sid,)).fetchone()
            if not row: return json_response(self, {"error": "Session not found."}, 404)
            return json_response(self, json.loads(row["result_json"]))
        if parsed.path == "/api/discovery":
            return json_response(self, discover_patterns())
        if parsed.path == "/api/model":
            return json_response(self, MODEL_INFO)
        if parsed.path == "/api/export.csv":
            return export_windows_csv(self)
        return super().do_GET()

    def do_POST(self):
        origin = self.headers.get("Origin")
        local_origin = f"http://127.0.0.1:{PORT}"
        if origin and origin != local_origin:
            return json_response(self, {"error": "Requests must come from the local CURIO page."}, 403)
        if urlparse(self.path).path == "/api/diagnosis":
            sid = start_diagnosis()
            if sid is None: return json_response(self, {"error": "A diagnosis is already running."}, 409)
            return json_response(self, {"id": sid, "durationSeconds": SESSION_SECONDS, "sampleSeconds": SAMPLE_SECONDS,
                                        "windowSeconds": WINDOW_SECONDS}, 202)
        endpoint = urlparse(self.path).path
        if endpoint in ("/api/dataset/preview", "/api/dataset/analyze"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 12 * 1024 * 1024:
                    raise ValueError("CSV upload is empty or exceeds the 10 MB limit.")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                source = payload.get("text", "")
                filename = payload.get("filename", "dataset.csv")
                if endpoint.endswith("preview"):
                    return json_response(self, preview_dataset(source, filename))
                return json_response(self, analyze_uploaded_dataset(source, filename, payload.get("target", ""), payload.get("group", "")))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                return json_response(self, {"error": str(exc)}, 400)
            except ImportError:
                return json_response(self, {"error": "ML evaluation requires scikit-learn. Install requirements.txt and restart CURIO."}, 400)
        return json_response(self, {"error": "Not found."}, 404)


if __name__ == "__main__":
    init_db()
    load_model()
    psutil.cpu_percent(interval=None)
    print(f"CURIO is running at http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
