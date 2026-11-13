# Architecture

How Leeuwenhoek works inside, and why it is built this way. To learn how to use it, see the [README](../README.md).

## Goals and non-goals

Leeuwenhoek is a debugging panel that lives **inside** the app it observes:

- It is enabled with `Leeuwenhoek(app)` or `lw.init_app(app)`. No separate server, process or frontend build.
- Your code doesn't change. Everything is captured through Flask hooks, signals, SQLAlchemy events and logging handlers.
- Everything produced while handling one request (and later one job or one CLI command) is grouped under a single `batch_id`.
- It is built for development and staging. Production use relies on sampling, the only-errors mode and access control.

Non-goals: distributed tracing or APM (that is what OpenTelemetry is for), aggregated metrics or time series, and alerting.

## Package layout

```
src/leeuwenhoek/
├── __init__.py      # Leeuwenhoek extension: init_app, batch lifecycle, record(), persist()
├── config.py        # Defaults, app.config and environment reading, DB URL from parts
├── context.py       # Batch dataclass and the current batch (contextvars)
├── models.py        # Entry dataclass and SQLAlchemy tables
├── writer.py        # Background writer: bounded queue, lazy thread, periodic pruning
├── filters.py       # Ignored paths, redaction, truncation
├── tags.py          # Dotted-path tag extraction, normalization, tagger callbacks
├── auth.py          # Panel access control and CSRF token
├── blueprint.py     # Panel views and Jinja filters
├── cli.py           # flask leeuwenhoek ...
├── storage/         # StorageDriver interface, SQLAlchemy driver, in-memory driver
├── watchers/        # One module per entry type: request, exception, query, log, dump
├── templates/leeuwenhoek/
└── static/leeuwenhoek/   # bundled Bulma, CSS and vanilla JS
```

## The life of a batch

1. **`before_request`**: `Leeuwenhoek._begin` decides whether this request is recorded. It isn't if Leeuwenhoek is disabled, the path is the panel or matches `IGNORE_PATHS`, the request is sampled out, or recording is paused. It then creates a `Batch` and stores it in a `ContextVar`. A batch that isn't recorded still exists, with `recording=False`, so watchers can check it cheaply.
2. **During the request**: watchers call `lw.record(type, content)`, which appends an `Entry` to the current batch's in-memory buffer, up to `MAX_ENTRIES_PER_BATCH`. Nothing touches the database yet. An entry recorded outside any batch (a log line at import time, for example) gets its own `standalone` batch and is saved right away.
3. **`after_request`**: the request watcher stores the status and response in `batch.meta`.
4. **`teardown_request`**: `on_finish` callbacks run while the batch is still current, so anything they record joins it. This is where the request entry itself is built. If `ONLY_ERRORS` is on, batches without an error are dropped here. Otherwise tags are applied and the whole batch is handed to the writer.
5. **Writer**: a daemon thread takes batches from a bounded `queue.Queue` and stores each one with a single insert. When the queue is full, the batch is dropped and a rate-limited warning is logged. A request is never blocked by the storage. The thread is started lazily and restarted if the PID changes (after a fork, for example `gunicorn --preload`). Pending batches are flushed at exit (`atexit`, with `FLUSH_TIMEOUT`). The same thread prunes old entries periodically, with jitter so several instances don't prune at the same time.

`ContextVar` is used instead of `flask.g` because it also works outside an app context, and with threads, gevent and async views.

## Data model

`leeuwenhoek_entries`

| Column | Notes |
|---|---|
| `id` | Auto-increment PK, used as the pagination cursor |
| `uuid` | Public ID of the entry |
| `batch_id` | Groups the entries of one request, job or command |
| `parent_batch_id` | Links a job to the request that dispatched it |
| `family_hash` | `sha1(class + file + line)` for exceptions, so repeated ones are grouped |
| `type` | `request`, `query`, `exception`, `log`, `dump`, ... |
| `content` | JSON payload specific to the type (`JSONB` on Postgres) |
| `is_root` | Shown in the Batches listing (the request entry, standalone entries) |
| `created_at` | |

Indexes: `batch_id`, `family_hash`, `type`, `created_at`, `(type, created_at)`.

`leeuwenhoek_tags` (`entry_uuid`, `tag`) holds tags, indexed by `tag`. `leeuwenhoek_settings` (`key`, `value`) holds state shared by all instances, such as the recording flag.

Pagination is by cursor (`before=<id>` / `after=<id>`), never by offset.

## Isolation from the host app

- **Separate engine.** Leeuwenhoek writes through its own SQLAlchemy engine (`DATABASE_URI`), never through the app's session. Its own queries are skipped by the query watcher, and everything it does internally runs inside `context.internal()`, so it never records itself.
- **Namespaced templates and filters.** Templates live under `templates/leeuwenhoek/`, and Jinja filters are prefixed `lw_`, so they don't collide with the app's.
- **Static files** are served by the Blueprint under the panel prefix. Bulma is bundled so the panel works offline.
- **Logging.** It adds one handler to the root logger and never modifies `app.logger`. The handler ignores the `leeuwenhoek` and `werkzeug` loggers.
- **CSRF.** The panel uses its own CSRF token (a double-submit cookie), so it works without a `SECRET_KEY`. It is exempted from Flask-WTF's CSRF check when the app uses Flask-WTF.

## Watchers

A watcher subclasses `watchers.base.Watcher`, declares `name`, `entry_type` and `label`, and implements `register(app)` and `unregister()`. `register` attaches hooks, and the watcher calls `self.record(content, ...)` when something happens. `AVAILABLE` in `watchers/__init__.py` maps names to classes. The panel shows a section for each active watcher whose entry type has a URL slug in `blueprint.SLUGS`.

A watcher for an optional library declares it in `requires` (a module name such as `"flask_mail"`, or a tuple of them) and imports it inside `register`, never at module level, so `watchers/__init__.py` still loads without it. `Watcher.available()` checks the requirements with `importlib.util.find_spec`, and `Watcher.missing()` lists the ones not installed. When `LEEUWENHOEK_WATCHERS` is not set, `default_watchers()` enables every watcher in `AVAILABLE` that is available. A configured watcher whose requirement is missing is skipped with a warning.

The exception watcher also chains `sys.excepthook` and `threading.excepthook`, so exceptions that nothing catches are recorded too: in scripts, in threads, and in `flask` CLI commands (Click only handles its own exceptions and lets the rest reach `sys.excepthook`). The previous hook is always called afterwards. The exception joins the batch that is current in the failing thread (a job running in a worker thread, for example); otherwise it is stored as `standalone`. A CLI command has no batch of its own yet, and by the time `sys.excepthook` runs its context is gone, so its exception is standalone. An exception is marked once recorded, so one that was already captured by the request path and then reaches a hook is not recorded twice. `unregister()` restores the previous hooks if they are still ours. If another hook was chained on top since, ours stays in the chain and only delegates, so several instances, or repeated `init_app` in tests, can be closed in any order.

Planned watchers and their hooks are described in the [issues](https://github.com/Manuel-Espinosa/flask-leeuwenhoek/issues?q=label%3Awatcher).

## Performance

- One buffer per batch and a single insert per batch.
- Asynchronous writes, with a bounded queue that drops batches instead of blocking.
- Payloads truncated to `MAX_BODY_KB`, at most `MAX_ENTRIES_PER_BATCH` entries per batch, and the query stack walk limited to a fixed number of frames.
- SQLite runs in WAL mode so concurrent writes don't block reads.
- Target overhead: under 5 ms per request in development.

### Benchmark

[`benchmarks/request_overhead.py`](../benchmarks/request_overhead.py) measures the overhead with the Flask test client. Each request runs 3 SQLAlchemy queries against a SQLite file and writes one log line. The same app is measured without Leeuwenhoek (`baseline`), with the background writer (`async`, the default) and with `LEEUWENHOEK_ASYNC_WRITE=False` (`sync`). Leeuwenhoek stores into a SQLite file, as it does by default in development, with all default watchers enabled and automatic pruning off. Each run has 200 warm-up requests, the three modes are interleaved over 3 rounds, and the script checks that the entries were actually stored.

Results on an AMD Ryzen 7 7445HS (12 logical cores), Linux 7.0, CPython 3.13.15, Flask 3.1.3, SQLAlchemy 2.1.6, SQLite 3.53.1, 6000 measured requests per mode:

| Mode | p50 (ms) | p95 (ms) | Mean (ms) | Overhead p50 (ms) | Overhead p95 (ms) |
|---|---:|---:|---:|---:|---:|
| baseline | 0.270 | 0.512 | 0.310 | — | — |
| async | 0.989 | 1.830 | 1.108 | +0.719 | +1.318 |
| sync | 0.913 | 1.502 | 1.006 | +0.643 | +0.990 |

The overhead is about 0.7 ms at p50 and 1.3 ms at p95, well under the 5 ms target. Most of it is building the entries (request details, query call sites, redaction, JSON) rather than writing them. With a local SQLite file the synchronous insert is cheap, so `sync` comes out slightly faster than `async` here: the background thread competes with the request for the GIL. The async writer pays off when the storage is slow or remote (Postgres over the network), because the request never waits for it.

To reproduce:

```bash
uv run python benchmarks/request_overhead.py
uv run python benchmarks/request_overhead.py --iterations 5000 --warmup 500 --rounds 5 --queries 10
```

Absolute numbers depend on the machine. Compare the overhead columns rather than the latencies.
