"""Measure the per-request overhead Leeuwenhoek adds to a small Flask app.

Every request runs a few SQLAlchemy queries against SQLite and writes one log line. The same
app is measured four times with the Flask test client:

- ``baseline``: without Leeuwenhoek
- ``async``: Leeuwenhoek with the background writer (the default)
- ``sync``: Leeuwenhoek writing each batch inside the request (``LEEUWENHOEK_ASYNC_WRITE=False``)
- ``memory``: like ``async``, plus peak memory per request (``LEEUWENHOEK_TRACK_MEMORY=True``)

Leeuwenhoek stores entries in a SQLite file, as it does by default in development.

Usage::

    uv run python benchmarks/request_overhead.py
    uv run python benchmarks/request_overhead.py --iterations 5000 --warmup 500 --queries 5
"""

from __future__ import annotations

import argparse
import gc
import importlib.metadata
import logging
import os
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from collections.abc import Callable

from flask import Flask, jsonify
from sqlalchemy import create_engine, text

from leeuwenhoek import Leeuwenhoek

MODES = ("baseline", "async", "sync", "memory")

log = logging.getLogger("bench.app")


def build_app(mode: str, workdir: str, queries: int) -> tuple[Flask, Callable[[], None]]:
    """Return the app for ``mode`` and a cleanup callback."""
    app = Flask(__name__, instance_path=os.path.join(workdir, mode))
    app.debug = True

    engine = create_engine(f"sqlite:///{os.path.join(workdir, mode + '-app.db')}")
    with engine.begin() as conn:
        conn.execute(text("drop table if exists users"))
        conn.execute(text("create table users (id integer primary key, name text)"))
        conn.execute(
            text("insert into users (name) values (:name)"),
            [{"name": f"user {i}"} for i in range(100)],
        )

    lw = None
    if mode != "baseline":
        app.config.update(
            LEEUWENHOEK_ENABLED=True,
            LEEUWENHOEK_ASYNC_WRITE=mode != "sync",
            LEEUWENHOEK_TRACK_MEMORY=mode == "memory",
            LEEUWENHOEK_DATABASE_URI=f"sqlite:///{os.path.join(workdir, mode + '-lw.db')}",
            # Keep pruning out of the measurement.
            LEEUWENHOEK_AUTO_PRUNE=False,
        )
        lw = Leeuwenhoek(app, engine=engine)

    @app.get("/users/<int:user_id>")
    def show_user(user_id: int):
        rows = []
        with engine.connect() as conn:
            for offset in range(queries):
                rows.append(
                    conn.execute(
                        text("select id, name from users where id = :id"),
                        {"id": (user_id + offset) % 100 + 1},
                    ).one()
                )
        log.info("Loaded %d users starting at %d", len(rows), user_id)
        return jsonify([{"id": r.id, "name": r.name} for r in rows])

    def cleanup() -> None:
        if lw is not None:
            # Make sure the batches were really written, so the numbers aren't of a no-op.
            if not lw.flush(timeout=30):
                raise RuntimeError(f"{mode}: pending writes not flushed")
            stored = lw.storage.counts().get("request", 0)
            if stored == 0:
                raise RuntimeError(f"{mode}: no request entries were stored")
            if mode == "memory":
                last = lw.storage.list("request", limit=1)[0]
                if "memory_peak_kb" not in last.content:
                    raise RuntimeError(f"{mode}: the peak memory was not recorded")
            lw.close()  # removes global hooks
        engine.dispose()

    return app, cleanup


def measure(mode: str, workdir: str, iterations: int, warmup: int, queries: int) -> list[float]:
    """Latency of each measured request, in milliseconds."""
    app, cleanup = build_app(mode, workdir, queries)
    client = app.test_client()
    try:
        for i in range(warmup):
            client.get(f"/users/{i % 100}")
        gc.collect()
        timings = []
        for i in range(iterations):
            start = time.perf_counter()
            response = client.get(f"/users/{i % 100}")
            timings.append((time.perf_counter() - start) * 1000)
            if response.status_code != 200:
                raise RuntimeError(f"{mode}: unexpected status {response.status_code}")
        return timings
    finally:
        cleanup()


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def environment() -> list[str]:
    return [
        f"Python {platform.python_version()} ({platform.python_implementation()})",
        f"{platform.system()} {platform.release()} {platform.machine()}",
        f"CPU: {cpu_name()} ({os.cpu_count()} logical cores)",
        f"Flask {importlib.metadata.version('flask')}",
        f"SQLAlchemy {importlib.metadata.version('sqlalchemy')}",
        f"SQLite {sqlite3.sqlite_version}",
    ]


def cpu_name() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--iterations", type=int, default=2000, help="measured requests per run")
    parser.add_argument("--warmup", type=int, default=200, help="unmeasured requests per run")
    parser.add_argument("--queries", type=int, default=3, help="SQL queries per request")
    parser.add_argument(
        "--rounds", type=int, default=3, help="runs per mode, interleaved; results are pooled"
    )
    args = parser.parse_args(argv)

    # Log lines go to a no-op handler, so the baseline doesn't print them to stderr.
    logging.getLogger().addHandler(logging.NullHandler())
    log.setLevel(logging.INFO)
    results: dict[str, list[float]] = {mode: [] for mode in MODES}
    with tempfile.TemporaryDirectory(prefix="lw-bench-") as workdir:
        for round_ in range(args.rounds):
            for mode in MODES:
                print(f"round {round_ + 1}/{args.rounds}: {mode}...", file=sys.stderr)
                results[mode] += measure(mode, workdir, args.iterations, args.warmup, args.queries)

    base50 = percentile(results["baseline"], 50)
    base95 = percentile(results["baseline"], 95)
    print("\n".join(environment()))
    print(
        f"{args.rounds} rounds x {args.iterations} requests per mode "
        f"({args.warmup} warm-up requests per round), {args.queries} queries + 1 log line each\n"
    )
    print("| Mode | p50 (ms) | p95 (ms) | Mean (ms) | Overhead p50 (ms) | Overhead p95 (ms) |")
    print("|---|---:|---:|---:|---:|---:|")
    for mode in MODES:
        values = results[mode]
        p50, p95 = percentile(values, 50), percentile(values, 95)
        over50 = "—" if mode == "baseline" else f"{p50 - base50:+.3f}"
        over95 = "—" if mode == "baseline" else f"{p95 - base95:+.3f}"
        print(
            f"| {mode} | {p50:.3f} | {p95:.3f} | {statistics.fmean(values):.3f} "
            f"| {over50} | {over95} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
