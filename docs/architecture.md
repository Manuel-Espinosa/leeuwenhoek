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

Planned watchers and their hooks are described in the [issues](https://github.com/Manuel-Espinosa/flask-leeuwenhoek/issues?q=label%3Awatcher).

## Performance

- One buffer per batch and a single insert per batch.
- Asynchronous writes, with a bounded queue that drops batches instead of blocking.
- Payloads truncated to `MAX_BODY_KB`, at most `MAX_ENTRIES_PER_BATCH` entries per batch, and the query stack walk limited to a fixed number of frames.
- SQLite runs in WAL mode so concurrent writes don't block reads.
- Target overhead: under 5 ms per request in development. A benchmark is tracked in [#14](https://github.com/Manuel-Espinosa/flask-leeuwenhoek/issues/14).
