from __future__ import annotations
import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).parent / "web" / "samples"
OUT.mkdir(parents=True, exist_ok=True)
RNG = random.Random(20260927)
FEATURES = [
    "cpu_mean", "cpu_max", "cpu_std", "cpu_trend", "cpu_freq_mean_mhz", "cpu_freq_trend_mhz",
    "memory_mean", "memory_max", "available_min_gb", "available_pct_min", "memory_trend", "swap_used_gb", "swap_pct_mean",
    "disk_read_mbps", "disk_write_mbps", "disk_busy_pct", "disk_latency_ms", "network_rx_mbps", "network_tx_mbps",
    "temperature_max_c", "process_count", "process_growth_mb_per_sec",
]
BASE = ["session_id", "window_index", "window_start", "window_end", "machine_id", "label", "data_origin", *FEATURES]
SCENARIOS = ("normal", "cpu_saturation", "memory_pressure", "disk_io_bottleneck")
TITLE = {"normal":"No sustained pressure", "cpu_saturation":"CPU saturation", "memory_pressure":"Memory pressure", "disk_io_bottleneck":"Disk I/O bottleneck"}

def jitter(scale):
    return RNG.gauss(0, scale)

def clamp(x, low, high):
    return max(low, min(high, x))

def make_row(label, session, machine, index, started):
    run_shift = jitter(1.5)
    if label == "normal":
        cpu = [27, 30, 26, 33, 29, 31][index] + run_shift + jitter(2)
        mem = [43, 44, 45, 44, 46, 45][index] + jitter(1.5)
        busy = [8, 12, 9, 17, 11, 10][index] + jitter(2)
        latency = [2.5, 3.2, 2.8, 4.1, 3.0, 2.7][index] + jitter(.5)
        swap_pct = 4 + jitter(1)
        growth = max(0, jitter(.12))
    elif label == "cpu_saturation":
        cpu = [48, 61, 73, 83, 91, 94][index] + run_shift + jitter(2)
        mem = [50, 51, 52, 51, 53, 52][index] + jitter(1.5)
        busy = [10, 13, 12, 15, 14, 12][index] + jitter(2)
        latency = 3.5 + jitter(.6)
        swap_pct = 5 + jitter(1)
        growth = max(0, jitter(.15))
    elif label == "memory_pressure":
        cpu = [28, 31, 30, 34, 32, 33][index] + run_shift + jitter(2)
        mem = [70, 75, 82, 88, 92, 95][index] + jitter(1.2)
        busy = [9, 11, 13, 12, 14, 11][index] + jitter(2)
        latency = 3.2 + jitter(.6)
        swap_pct = [4, 8, 18, 34, 55, 72][index] + jitter(2)
        growth = [1.0, 1.8, 2.7, 3.5, 4.4, 5.2][index] + jitter(.3)
    else:
        cpu = [31, 32, 34, 33, 35, 34][index] + run_shift + jitter(2)
        mem = [52, 53, 53, 54, 54, 55][index] + jitter(1.3)
        busy = [67, 73, 78, 84, 90, 94][index] + jitter(2)
        latency = [20, 24, 29, 34, 41, 47][index] + jitter(1.5)
        swap_pct = 5 + jitter(1)
        growth = max(0, jitter(.15))
    cpu = clamp(cpu, 0, 100); mem = clamp(mem, 0, 99); busy = clamp(busy, 0, 100)
    available_pct = clamp(100-mem+jitter(.4), .5, 100)
    previous_cpu = cpu if index == 0 else make_row._previous[(session,"cpu")]
    previous_mem = mem if index == 0 else make_row._previous[(session,"memory")]
    previous_freq = None if index == 0 else make_row._previous[(session,"freq")]
    freq = max(900, 3400 - max(0,cpu-60)*8 + jitter(25))
    make_row._previous[(session,"cpu")] = cpu
    make_row._previous[(session,"memory")] = mem
    make_row._previous[(session,"freq")] = freq
    start = started + timedelta(seconds=5*index)
    end = start + timedelta(seconds=5)
    return {
      "session_id": session, "window_index": index, "window_start": start.isoformat().replace("+00:00","Z"),
      "window_end": end.isoformat().replace("+00:00","Z"), "machine_id": machine, "label": label, "data_origin": "synthetic",
      "cpu_mean": round(cpu,2), "cpu_max": round(clamp(cpu+abs(jitter(3)),0,100),2), "cpu_std": round(abs(jitter(3))+1,2),
      "cpu_trend": round(cpu-previous_cpu,2), "cpu_freq_mean_mhz": round(freq,1),
      "cpu_freq_trend_mhz": round(0 if previous_freq is None else freq-previous_freq,1),
      "memory_mean": round(mem,2), "memory_max": round(clamp(mem+abs(jitter(1.5)),0,100),2),
      "available_min_gb": round(max(.05,16*available_pct/100),2), "available_pct_min": round(available_pct,2),
      "memory_trend": round(mem-previous_mem,2), "swap_used_gb": round(max(0,(swap_pct/100)*8+jitter(.03)),2),
      "swap_pct_mean": round(clamp(swap_pct,0,100),2), "disk_read_mbps": round(max(.1,(42 if label=="disk_io_bottleneck" else 1.4)+jitter(5)),2),
      "disk_write_mbps": round(max(.1,(29 if label=="disk_io_bottleneck" else .8)+jitter(4)),2), "disk_busy_pct": round(busy,2),
      "disk_latency_ms": round(max(.1,latency),2), "network_rx_mbps": round(max(0,2.5+jitter(.7)),2),
      "network_tx_mbps": round(max(0,.9+jitter(.3)),2), "temperature_max_c": round(49+cpu*.22+jitter(1.5),1),
      "process_count": int(116+jitter(3)), "process_growth_mb_per_sec": round(max(0,growth),2),
    }
make_row._previous = {}

combined=[]
for condition in SCENARIOS:
    scenario_rows=[]
    for run in range(1,6):
        sid=f"{condition}-run-{run:02d}"
        # Several machine IDs ensure the split is not based on one repeated run.
        machine=f"M{1+(run-1)%3}"
        started=datetime(2026,9,27,tzinfo=timezone.utc)+timedelta(days=run-1,minutes=SCENARIOS.index(condition)*30)
        make_row._previous={}
        for window in range(6):
            scenario_rows.append(make_row(condition,sid,machine,window,started))
    combined.extend(scenario_rows)
    filename=f"curio_diagnosis_{condition}.csv"
    with (OUT/filename).open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=BASE);w.writeheader();w.writerows(scenario_rows)
with (OUT/"curio_diagnosis_suite.csv").open("w",newline="",encoding="utf-8") as f:
    w=csv.DictWriter(f,fieldnames=BASE);w.writeheader();w.writerows(combined)
print(f"Created combined suite ({len(combined)} rows, {len(combined)//6} runs) and 4 focused diagnosis datasets.")
