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
├── filters.py       # Ignored paths, entry filter type, redaction, truncation
├── tags.py          # Dotted-path tag extraction, normalization, tagger callbacks
├── auth.py          # Panel access control and CSRF token
├── blueprint.py     # Panel views and Jinja filters
├── cli.py           # flask leeuwenhoek ...
├── storage/         # StorageDriver interface, SQLAlchemy driver, in-memory driver
├── watchers/        # One module per entry type: request, exception, query, log, dump, template, command, http, cache, model
├── templates/leeuwenhoek/
└── static/leeuwenhoek/   # bundled Bulma, CSS and vanilla JS
```

## The life of a batch

1. **`before_request`**: `Leeuwenhoek._begin` decides whether this request is recorded. It isn't if Leeuwenhoek is disabled, the path is the panel or matches `IGNORE_PATHS`, the request is sampled out, or recording is paused. It then creates a `Batch` and stores it in a `ContextVar`. A batch that isn't recorded still exists, with `recording=False`, so watchers can check it cheaply.
2. **During the request**: watchers call `lw.record(type, content)`, which appends an `Entry` to the current batch's in-memory buffer, up to `MAX_ENTRIES_PER_BATCH`. Nothing touches the database yet. An entry recorded outside any batch (a log line at import time, for example) gets its own `standalone` batch and is saved right away.
3. **`after_request`**: the request watcher stores the status and response in `batch.meta`.
4. **`teardown_request`**: `on_finish` callbacks run while the batch is still current, so anything they record joins it. This is where the request entry itself is built, and where the query watcher flags N+1 queries (same normalized SQL at least `N_PLUS_ONE_THRESHOLD` times). Because this happens before filters and taggers, they already see `content["n_plus_one"]` and the `n+1` tag. If `ONLY_ERRORS` is on, batches without an error are dropped here. Otherwise entry filters (`lw.filter`) run first: rejected entries are removed, and if the root entry is rejected the whole batch is dropped. Then tags are applied and the batch is handed to the writer.
5. **Writer**: a daemon thread takes batches from a bounded `queue.Queue` and stores each one with a single insert. When the queue is full, the batch is dropped and a rate-limited warning is logged. A request is never blocked by the storage. The thread is started lazily and restarted if the PID changes (after a fork, for example `gunicorn --preload`). Pending batches are flushed at exit (`atexit`, with `FLUSH_TIMEOUT`). The same thread prunes old entries periodically, with jitter so several instances don't prune at the same time.

`ContextVar` is used instead of `flask.g` because it also works outside an app context, and with threads, gevent and async views.

### Batches outside a request

Requests use `before_request` and `teardown_request`. Anything else that is one unit of work (a CLI command, and later a job) opens its batch with `lw.batch(kind)`, a context manager over `lw.open_batch()` and `lw.close_batch()`, which are what the request hooks use too:

```python
with lw.batch("job", parent_id=dispatching_batch_id) as batch:
    run_the_job()  # queries, logs, exceptions... join `batch`
```

`open_batch` applies the same decision as a request (enabled, sampling, recording paused) plus the caller's own `skip`, and makes the batch current. `close_batch` runs the `on_finish` callbacks with the exception, if any, applies `ONLY_ERRORS`, persists the batch and restores the previous one. The context manager persists the batch also when the block raises, and lets the exception propagate.

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

`leeuwenhoek_tags` (`entry_uuid`, `tag`) holds tags, indexed by `tag`. `leeuwenhoek_settings` (`key`, `value`) holds state shared by all instances, such as the recording flag and resolved exception families. Drivers expose it through `get_setting`, `set_setting`, `delete_setting` and `settings(prefix)`.

A resolved family is the setting `resolved:<family_hash>`, whose value is the resolve time (ISO, UTC). It reopens lazily: a family counts as resolved only while its newest occurrence is not newer than that time, so nothing is written when an exception is recorded and the writer thread never races with the panel. The Exceptions listing (and its live-polling rows) reads every `resolved:` setting, checks them with one grouped `MAX(created_at)` query (`last_seen`), and leaves the still-resolved families out with a `family_hash NOT IN (...)` filter. An occurrence recorded before the resolve time but written later (async writer) does not reopen the family, because `created_at` is taken when the exception happens. Resolve times come from the panel's clock and `created_at` from the recording instance's, so keep instance clocks in sync.

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

The exception watcher also chains `sys.excepthook` and `threading.excepthook`, so exceptions that nothing catches are recorded too: in scripts, in threads, and in `flask` CLI commands (Click only handles its own exceptions and lets the rest reach `sys.excepthook`). The previous hook is always called afterwards. The exception joins the batch that is current in the failing thread (a job running in a worker thread, for example); otherwise it is stored as `standalone`. By the time `sys.excepthook` runs, a CLI command's batch is already closed, so the command watcher captures the exception itself, inside the command's batch, before closing it. An exception is marked once recorded, so one that was already captured by the request path or by the command watcher and then reaches a hook is not recorded twice. `unregister()` restores the previous hooks if they are still ours. If another hook was chained on top since, ours stays in the chain and only delegates, so several instances, or repeated `init_app` in tests, can be closed in any order.

The command watcher wraps `click.Command.invoke`, which every Click command (and every group dispatching to a subcommand) goes through. It picks the commands of `app.cli` when they run, by looking the command up in the `app.cli` tree, so nested groups, blueprint commands and commands registered after `init_app` are all found, and commands outside `app.cli` (`flask routes`, plugins) are not. Groups are skipped: the batch belongs to the leaf command, named by its full path (`db migrations apply`). The wrapper opens a `kind="command"` batch with `lw.batch()`, records the command as the root entry when the command returns or raises, and passes exceptions that are not Click's own exits or errors to the exception watcher's `capture()`. Like the exception hooks, the wrapper is chained: `unregister()` puts the previous `invoke` back if ours is still installed, otherwise ours stays and only delegates.

The mail watcher connects to Flask-Mail's `email_dispatched` signal with the app as sender, so several apps in one process only see their own mail. Flask-Mail sends the signal even when delivery is suppressed (`MAIL_SUPPRESS_SEND`, on by default with `TESTING`), so mail sent in tests is recorded too. `unregister()` disconnects the receiver from every sender, because blinker only drops a receiver from `Signal.receivers` that way. The HTML body is rendered in an `<iframe sandbox="">` through `srcdoc`, which Jinja autoescapes like any attribute: the sandbox grants nothing, so the mail's scripts don't run and it can't reach the panel's origin.

The template watcher listens to Flask's `before_render_template` and `template_rendered` signals for the app. They fire once per top-level render (`render_template`, `render_template_string`, `stream_template`), not for `{% extends %}` or `{% include %}`. A render can start another one (a context processor or a filter calling `render_template`), so starts are kept in a per-context stack and each `template_rendered` is paired with the innermost start for the same context dict, which both signals receive. Only the dict's `id` is stored, so a render that raised (and never sent `template_rendered`) keeps no reference to its context; the stack is capped so those leftovers stay bounded. Templates under `leeuwenhoek/` are skipped in both handlers, so rendering the panel never creates entries. A streamed template finishes rendering after the request batch has closed, so it is stored as `standalone`. Leeuwenhoek connects to these signals nowhere else, so nothing is recorded twice. `unregister()` disconnects both.

The outgoing HTTP watcher (`watchers/http_client.py`) needs `requests` or `httpx`, not both, so it overrides `missing()` to report its modules only when neither is installed, and `register` patches only the libraries that are present. It replaces `send` on `requests.Session`, `httpx.Client` and `httpx.AsyncClient`. Patching the class, instead of using httpx event hooks or a transport wrapper, catches every client, including those created before `init_app` and inside third-party libraries, and it is the one place that sees the duration, the exception if the call raised, and the final response after redirects. `requests` sends each redirect hop through `Session.send` again, so a context variable marks the outermost call and the hops are folded into it. Patches follow the same rule as the exception hooks: `unregister()` puts the original method back if our wrapper is still on top; otherwise the wrapper stays and only delegates, and the next wrapper of ours to be removed from the top skips it, so once every instance is closed the original method is back.

The cache watcher wraps the proxy methods of `flask_caching.Cache` (`get`, `get_many`, `get_dict`, `has`, `set`, `add`, `set_many`, `delete`, `delete_many`, `unlink`, `clear`) on the class, so every `Cache` instance is covered, including the calls made by `@cache.cached` and `@cache.memoize`. The patch is applied once, shared by every active watcher, and removed when the last one unregisters, so `lw.close()` leaves `Cache` as it was. A wrapped call finds the watcher through the Leeuwenhoek instance of the current app (or of `cache.app` outside an app context). A context variable marks that a wrapped method is running, so a proxy that calls another one (`unlink` calls `delete_many`) is recorded once. A read is a hit when the value is not `None`, as Flask-Caching itself assumes, and `has` is a hit when it returns `True`. Writes record the explicit timeout or the backend's `default_timeout`. `inc` and `dec` are not proxied by `Cache` (only by the backend in `cache.cache`), so they are not recorded. Values are opt-in (`CACHE_VALUES`): they can be large, and turning arbitrary objects into JSON costs time on every cache call. `_group()` in the blueprint adds the `hits` and `misses` of the batch's cache entries, shown on the Cache card.

The model watcher listens to `after_flush`, `after_commit`, `after_soft_rollback` and `after_transaction_end` on the `Session` class, so Flask-SQLAlchemy's scoped session and plain sessions are covered alike. Attribute history only exists until the flush finishes, so `after_flush` builds the content (reading `session.new`, the `session.dirty` objects for which `session.is_modified()` is true, and `session.deleted`, from the instance state, never loading anything) and keeps it in `session.info` with the innermost transaction it was flushed in. `after_commit`, which only fires for the outermost transaction, records it. `after_soft_rollback` drops what was flushed in the rolled back transaction or a savepoint inside it, and the end of the outermost transaction without a commit (`close()`) drops the rest. Recording at commit rather than at flush keeps rolled-back writes out of the panel, at the cost of an entry's time being the commit's. A flush is skipped when it belongs to another app's instance (the app context's `leeuwenhoek` extension is not ours), when the session is bound to Leeuwenhoek's engine, and per object when it is mapped onto a Leeuwenhoek table. The storage itself only uses Core, so it never triggers these events. `unregister()` removes the four listeners with `event.remove`.

Planned watchers and their hooks are described in the [issues](https://github.com/Manuel-Espinosa/flask-leeuwenhoek/issues?q=label%3Awatcher).

## Performance

- One buffer per batch and a single insert per batch.
- Asynchronous writes, with a bounded queue that drops batches instead of blocking.
- Payloads truncated to `MAX_BODY_KB`, at most `MAX_ENTRIES_PER_BATCH` entries per batch, and the query stack walk limited to a fixed number of frames.
- SQLite runs in WAL mode so concurrent writes don't block reads.
- Target overhead: under 5 ms per request in development.
- Peak memory (`TRACK_MEMORY`) is opt-in, because `tracemalloc` slows down every allocation.

### Peak memory

With `TRACK_MEMORY` on, the request watcher starts `tracemalloc` in `before_request` (only for recorded requests) and stores the peak in `content["memory_peak_kb"]` when the request entry is built. `tracemalloc` is process-global, so `watchers.request.MemoryTracker` reference-counts the requests using it: the first one starts tracing and the last one stops it. If something else had already started tracing (`python -X tracemalloc`, a profiler, a test), it is never stopped. Each request calls `tracemalloc.reset_peak()` and reports the peak above the memory traced when it started, so memory allocated earlier isn't counted. That reset also clears the peak seen by whoever else is tracing.

Limitation: with concurrent requests (a threaded server, gevent), the peak covers the allocations of every request in flight, and each request's reset clears the peak of the others. The value is exact when requests run one at a time and approximate otherwise.

### Benchmark

[`benchmarks/request_overhead.py`](../benchmarks/request_overhead.py) measures the overhead with the Flask test client. Each request runs 3 SQLAlchemy queries against a SQLite file and writes one log line. The same app is measured without Leeuwenhoek (`baseline`), with the background writer (`async`, the default), with `LEEUWENHOEK_ASYNC_WRITE=False` (`sync`), and with the background writer plus `LEEUWENHOEK_TRACK_MEMORY=True` (`memory`). Leeuwenhoek stores into a SQLite file, as it does by default in development, with all default watchers enabled and automatic pruning off. Each run has 200 warm-up requests, the four modes are interleaved over 3 rounds, and the script checks that the entries were actually stored.

Results on an AMD Ryzen 7 7445HS (12 logical cores), Linux 7.0, CPython 3.13.15, Flask 3.1.3, SQLAlchemy 2.1.6, SQLite 3.53.1, 6000 measured requests per mode:

| Mode | p50 (ms) | p95 (ms) | Mean (ms) | Overhead p50 (ms) | Overhead p95 (ms) |
|---|---:|---:|---:|---:|---:|
| baseline | 0.298 | 0.548 | 0.334 | — | — |
| async | 1.179 | 2.139 | 1.282 | +0.881 | +1.591 |
| sync | 0.968 | 1.755 | 1.092 | +0.670 | +1.207 |
| memory | 2.166 | 3.650 | 2.333 | +1.868 | +3.101 |

The overhead is about 0.9 ms at p50 and 1.6 ms at p95, well under the 5 ms target. Most of it is building the entries (request details, query call sites, redaction, JSON) rather than writing them. With a local SQLite file the synchronous insert is cheap, so `sync` comes out slightly faster than `async` here: the background thread competes with the request for the GIL. The async writer pays off when the storage is slow or remote (Postgres over the network), because the request never waits for it.

Tracking memory roughly doubles the overhead: about 1 ms more than `async` at p50 and 1.5 ms more at p95. While `tracemalloc` is tracing, every allocation in the process is recorded, and here tracing is also started and stopped on every request because nothing else keeps it running. That is why `TRACK_MEMORY` is off by default.

To reproduce:

```bash
uv run python benchmarks/request_overhead.py
uv run python benchmarks/request_overhead.py --iterations 5000 --warmup 500 --rounds 5 --queries 10
```

Absolute numbers depend on the machine. Compare the overhead columns rather than the latencies.
