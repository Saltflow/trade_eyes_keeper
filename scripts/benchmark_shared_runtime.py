"""Bounded, offline before/after runtime measurements; never send messages."""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_source(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    import psutil

    from src.personal_monitor import MonitorStore

    stats = {"connections": 0, "reads": 0, "writes": 0, "business_scans": 0}
    stats_lock = threading.Lock()

    class CountingStore(MonitorStore):
        @contextmanager
        def connection(self, write=False):
            with stats_lock:
                stats["connections"] += 1
            with super().connection(write=write) as db:

                def observe(statement):
                    upper = statement.upper().lstrip()
                    with stats_lock:
                        stats["reads"] += int(upper.startswith("SELECT"))
                        stats["writes"] += int(
                            upper.startswith(("INSERT", "UPDATE", "DELETE"))
                        )
                        stats["business_scans"] += int(
                            upper.startswith("SELECT")
                            and any(
                                "FROM " + table in upper
                                for table in ("REMINDERS", "OUTBOX", "POSITIONS")
                            )
                        )

                db.set_trace_callback(observe)
                yield db

    evidence = args.evidence.resolve()
    folder = evidence / ("benchmark_" + args.mode)
    folder.mkdir(parents=True, exist_ok=True)
    config = {
        "storage": {"data_dir": str(folder)},
        "interactive": {},
        "scheduler": {
            "daily_enabled": False,
            "optimize_enabled": False,
            "run_time": "00:00",
            "brief_reports": [],
        },
    }
    if args.mode == "baseline":
        before = evidence / "before"
        load_source(
            "src.core.schedule_manager", before / "src/core/schedule_manager.py"
        )
        host_module = load_source(
            "src.core._benchmark_host", before / "src/core/runtime_service.py"
        )
        personal_module = load_source(
            "src._benchmark_personal", before / "src/personal_monitor_runtime.py"
        )
    else:
        import src.core.runtime_service as host_module
        import src.personal_monitor_runtime as personal_module

    store = CountingStore(folder / "personal.sqlite3")
    host = host_module.RuntimeService(config)

    def forbidden_send(*_args):
        raise AssertionError("benchmark outbound is forbidden")

    kwargs = {}
    if args.mode == "candidate":
        kwargs["execution_runtime"] = host.execution_runtime
    personal = personal_module.PersonalMonitorRuntime(
        store, config, forbidden_send, observation_provider=forbidden_send, **kwargs
    )
    errors = []

    def run_host():
        try:
            host.run(on_started=personal.start)
        except Exception as exc:  # noqa: BLE001 - surface any host failure in the benchmark result
            errors.append(type(exc).__name__)

    worker = threading.Thread(target=run_host, name="benchmark-host")
    worker.start()
    proc = psutil.Process()
    measurements = []
    try:
        time.sleep(2)
        for index in range(args.rounds):
            started = time.monotonic()
            initial_cpu = sum(proc.cpu_times()[:2])
            initial_stats = dict(stats)
            rss = []
            root_rss = []
            process_counts = []
            identities = {}
            tree, refresh_tree = [proc], 0.0
            while time.monotonic() - started < args.seconds:
                if time.monotonic() >= refresh_tree:
                    tree = [proc, *proc.children(recursive=True)]
                    refresh_tree = time.monotonic() + 60
                samples = []
                for member in tree:
                    try:
                        samples.append(member.memory_info().rss)
                        identities[member.pid] = {
                            "pid": member.pid,
                            "created": member.create_time(),
                            "name": member.name(),
                        }
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                rss.append(sum(samples))
                root_rss.append(proc.memory_info().rss)
                process_counts.append(len(samples))
                time.sleep(min(1, args.seconds))
                if errors or not worker.is_alive():
                    raise RuntimeError("benchmark host stopped")
            elapsed = time.monotonic() - started
            measurements.append(
                {
                    "round": index + 1,
                    "elapsed_seconds": elapsed,
                    "cpu_seconds": sum(proc.cpu_times()[:2]) - initial_cpu,
                    "cpu_one_core_percent": (
                        (sum(proc.cpu_times()[:2]) - initial_cpu) / elapsed * 100
                    ),
                    "rss_max": max(rss),
                    "rss_mean": sum(rss) / len(rss),
                    "root_rss_max": max(root_rss),
                    "processes_max": max(process_counts),
                    "process_identities": list(identities.values()),
                    "threads": proc.num_threads(),
                    "business_database": {
                        key: stats[key] - initial_stats[key] for key in stats
                    },
                }
            )
            result = {
                "mode": args.mode,
                "profile": "offline-empty-registered-host-and-personal",
                "outbound": False,
                "rounds": measurements,
                "errors": errors,
                "environment": {
                    "platform": platform.platform(),
                    "python": sys.version,
                    "logical_cpus": psutil.cpu_count(),
                    "rss_scope": "sum of host process and all observed descendants",
                    "cpu_scope": "host process; child identities retained separately",
                    "tree_membership_refresh_seconds": 60,
                    "sampling_limit": "idle fixture only; not short-lived report subprocess peaks",
                },
            }
            (evidence / ("benchmark_" + args.mode + ".json")).write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
            print(json.dumps(measurements[-1]), flush=True)
    finally:
        personal.stop()
        host.stop()
        worker.join(timeout=30)
        if worker.is_alive():
            raise RuntimeError("benchmark did not stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
