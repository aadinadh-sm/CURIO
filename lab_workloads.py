"""Opt-in controlled workload helpers for CURIO's student dataset experiments.

Nothing runs unless a student starts this script directly. It never changes OS settings.
"""
from __future__ import annotations

import argparse
import hashlib
import multiprocessing as mp
import os
import random
import shutil
import tempfile
import time

import psutil


def cpu_worker(stop):
    seed = b"CURIO controlled CPU workload"
    while not stop.is_set():
        seed = hashlib.sha256(seed).digest()


def run_cpu(seconds, workers):
    stop = mp.Event()
    processes = [mp.Process(target=cpu_worker, args=(stop,), daemon=True) for _ in range(workers)]
    print(f"Starting {workers} CPU worker(s) for {seconds}s. This will increase CPU load; stop with Ctrl+C.", flush=True)
    for p in processes: p.start()
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        print("Stopping workload early.")
    finally:
        stop.set()
        for p in processes: p.join(timeout=2)
        for p in processes:
            if p.is_alive(): p.terminate(); p.join(timeout=1)


def run_memory(seconds, max_mb, fraction):
    available = psutil.virtual_memory().available
    # Keep at least 1 GiB free for Windows and other active applications.
    safe_ceiling = max(0, available - 1024 * 1024 * 1024)
    target = min(max_mb * 1024 * 1024, int(available * fraction), safe_ceiling)
    if target < 128 * 1024 * 1024:
        raise SystemExit("Too little safe headroom for this experiment. Close applications or use a separate test computer.")
    print(f"Allocating about {target // 1048576} MB for {seconds}s (leaving at least 1 GiB free). Stop with Ctrl+C.", flush=True)
    blocks = []
    try:
        for _ in range(target // (8 * 1024 * 1024)):
            blocks.append(bytearray(os.urandom(8 * 1024 * 1024)))
        time.sleep(seconds)
    except KeyboardInterrupt:
        print("Stopping workload early.")
    finally:
        blocks.clear()


def run_disk(seconds, size_mb):
    free = shutil.disk_usage(tempfile.gettempdir()).free
    size = size_mb * 1024 * 1024
    if free < size + 1024 * 1024 * 1024:
        raise SystemExit("Not enough temporary-drive headroom for the disk experiment; 1 GiB is reserved.")
    block_size = 4096
    block = os.urandom(block_size)
    print(f"Running random writes in a temporary {size_mb} MB file for {seconds}s. The file is removed on exit. Stop with Ctrl+C.", flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix="curio-lab-") as folder:
            path = os.path.join(folder, "controlled-io.tmp")
            with open(path, "w+b", buffering=0) as handle:
                handle.truncate(size)
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    handle.seek(random.randrange(size // block_size) * block_size)
                    handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
    except KeyboardInterrupt:
        print("Stopping workload early; temporary data is being cleaned up.")


def main():
    parser = argparse.ArgumentParser(description="Create short-lived, controlled workloads for a CURIO research dataset.")
    sub = parser.add_subparsers(dest="mode", required=True)
    cpu = sub.add_parser("cpu", help="CPU saturation experiment")
    cpu.add_argument("--seconds", type=int, default=45)
    cpu.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    memory = sub.add_parser("memory", help="Memory pressure experiment")
    memory.add_argument("--seconds", type=int, default=45)
    memory.add_argument("--max-mb", type=int, default=512)
    memory.add_argument("--fraction", type=float, default=.35)
    disk = sub.add_parser("disk", help="Disk I/O experiment")
    disk.add_argument("--seconds", type=int, default=45)
    disk.add_argument("--size-mb", type=int, default=128)
    args = parser.parse_args()
    if not 10 <= args.seconds <= 180: parser.error("--seconds must be between 10 and 180")
    if args.mode == "cpu":
        if not 1 <= args.workers <= (os.cpu_count() or 1): parser.error("--workers must be between 1 and the logical CPU count")
        run_cpu(args.seconds, args.workers)
    elif args.mode == "memory":
        if not 128 <= args.max_mb <= 4096 or not .1 <= args.fraction <= .7: parser.error("--max-mb must be 128..4096 and --fraction 0.1..0.7")
        run_memory(args.seconds, args.max_mb, args.fraction)
    else:
        if not 32 <= args.size_mb <= 512: parser.error("--size-mb must be 32..512")
        run_disk(args.seconds, args.size_mb)


if __name__ == "__main__":
    mp.freeze_support()
    main()
