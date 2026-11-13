# flask-leeuwenhoek

A debugging microscope that lives inside your Flask app. Leeuwenhoek records what happens during each request (the request itself, SQL queries, exceptions, log lines and ad-hoc dumps), groups it all by request, and shows it in a panel at `/leeuwenhoek`.

- **No separate service.** It is a Blueprint plus a few hooks. The panel is rendered on the server with Jinja and Bulma, with no frontend build and no CDN.
- **No changes to your code.** Everything is captured through Flask hooks, signals, SQLAlchemy events and a logging handler.
- **Grouped by batch.** Every entry produced while handling one request shares a `batch_id`, so you can open a request and see every query, log line and exception it produced.
- **Its own database.** Entries go to a separate SQLAlchemy engine (SQLite by default, Postgres in production), so they never mix with your app's data or record themselves.
- **Safe enough for staging.** Sensitive values are redacted, payloads are truncated, and writes happen in a background thread with a bounded queue. Sampling, an only-errors mode and access control are built in.

> **Status:** early (v0.1). See the [roadmap](#roadmap) for what's coming.

## Installation

Requires Python 3.10+, Flask 2.3+ and SQLAlchemy 2.x.

Leeuwenhoek isn't on PyPI yet ([#23](https://github.com/Manuel-Espinosa/flask-leeuwenhoek/issues/23)). For now, install it from GitHub:

```bash
uv add git+https://github.com/Manuel-Espinosa/flask-leeuwenhoek
# or
pip install git+https://github.com/Manuel-Espinosa/flask-leeuwenhoek
```

Add the `login` extra (`flask-leeuwenhoek[login] @ git+...`) to record the current [Flask-Login](https://flask-login.readthedocs.io/) user on each request. If Flask-Login is already installed, this happens automatically.

Add the `cache` extra (`flask-leeuwenhoek[cache]`) to record [Flask-Caching](https://flask-caching.readthedocs.io/) operations. As with Flask-Login, the cache watcher turns on by itself when Flask-Caching is installed.

Add the `mail` extra (`flask-leeuwenhoek[mail] @ git+...`) to record emails sent with [Flask-Mail](https://flask-mail.readthedocs.io/). As with every integration watcher, it turns on by itself when Flask-Mail is installed.

Add the `http` extra to install [requests](https://requests.readthedocs.io/) and [HTTPX](https://www.python-httpx.org/) for the outgoing HTTP watcher. Either one is enough, and the watcher turns on by itself if your app already uses one of them.

Add the `rq` extra (`flask-leeuwenhoek[rq]`) to record [RQ](https://python-rq.org/) jobs (RQ 1.15 or newer). The watcher turns on by itself when RQ is installed.

## Quickstart

```python
from flask import Flask
from leeuwenhoek import Leeuwenhoek

app = Flask(__name__)
Leeuwenhoek(app)
```

Or with the application factory pattern:

```python
lw = Leeuwenhoek()


def create_app():
    app = Flask(__name__)
    lw.init_app(app, engine=db.engine)  # engine: only record queries from this engine
    return app
```

Run the app in debug mode (`flask run --debug`), make a few requests, then open **http://localhost:5000/leeuwenhoek**.

By default Leeuwenhoek is enabled only when `app.debug` is `True`, and the panel is only reachable in debug mode. See [Access control](#access-control) to use it anywhere else.

### Try the demo

```bash
git clone https://github.com/Manuel-Espinosa/flask-leeuwenhoek
cd flask-leeuwenhoek
uv sync
uv run flask --app examples/demo run --debug
```

Visit `/users`, `/boom` and `/dump`, then open `/leeuwenhoek`.

## What it records

| Watcher | Captures |
|---|---|
| `request` | Method, URL, URL rule, endpoint, blueprint, view args, IP, headers, query string, body, session, user, status, response headers, response body (JSON/text only), duration, and optionally peak memory |
| `query` | SQL, parameters, duration, and the file and line in *your* code that ran the query. Queries slower than `LEEUWENHOEK_SLOW_QUERY_MS` are flagged as slow, and queries repeated many times in one request as [N+1](#n1-queries). |
| `exception` | Class, message, full traceback with a code snippet for each frame. Repeated exceptions are grouped into **families** (same class, file and line). Also captures exceptions turned into 5xx responses by your own error handlers, and uncaught exceptions in scripts, CLI commands and threads (through `sys.excepthook` and `threading.excepthook`, which are chained and restored by `lw.close()`). |
| `log` | Level, message, logger name, `extra` fields and `exc_info`, from a handler on the root logger |
| `dump` | Whatever you pass to `dump()`, with the file and line it was called from |
| `mail` | Emails sent with Flask-Mail (its `email_dispatched` signal, which also fires when sending is suppressed with `MAIL_SUPPRESS_SEND` or `TESTING`): sender, to/cc/bcc, reply-to, subject, extra headers (redacted with `LEEUWENHOEK_HIDDEN_HEADERS`), HTML and text bodies (truncated to `LEEUWENHOEK_MAX_BODY_KB`) and attachment names and types, never their content. The panel previews the HTML body in a fully sandboxed `<iframe>` (no scripts, no same-origin). Needs the `mail` extra. |
| `template` | Each Jinja template rendered with `render_template`, `render_template_string` or `stream_template`: name, file, context keys (never their values, and without Flask's own `g`, `request` and `session`) and render duration. Templates pulled in with `{% extends %}` or `{% include %}` are part of the top-level render, not separate entries. Leeuwenhoek's own templates are never recorded. |
| `command` | `flask` CLI commands of `app.cli`: full name (`db seed` for subcommands), arguments, options, exit code and duration. Each command gets its own batch, see [CLI commands](#cli-commands). |
| `http` | Outgoing HTTP calls made with [requests](https://requests.readthedocs.io/) or [HTTPX](https://www.python-httpx.org/) (sync and async clients): method, URL, request headers, payload, status, response headers, response body (JSON/text only), duration, redirects, and the error if the call raised. Enabled when either library is installed. |
| `cache` | Flask-Caching operations (`get`, `get_many`, `get_dict`, `has`, `set`, `add`, `set_many`, `delete`, `delete_many`, `unlink`, `clear`), including the ones made by `@cache.cached` and `@cache.memoize` (with Flask-Caching 2.5 or newer; older versions, the last ones for Python 3.10, make the decorators talk to the backend directly and only the direct calls are recorded): keys, hit or miss for reads, TTL for writes, backend and duration. Values are stored only with `LEEUWENHOEK_CACHE_VALUES`. The Cache card of a request shows its hit and miss counts. |
| `rq` | RQ jobs: function path, queue, job id, status, arguments and keyword arguments (redacted with `LEEUWENHOEK_HIDDEN_PARAMS`, truncated to `LEEUWENHOEK_MAX_BODY_KB`), duration and exception. Each execution gets its own batch, linked to the request that enqueued the job, see [RQ jobs](#rq-jobs). Needs the `rq` extra. |

By default every watcher whose library is installed is enabled, so integration watchers (mail, Celery, Redis...) turn on by themselves when you install the package they observe. Set `LEEUWENHOEK_WATCHERS` to pick them explicitly, e.g. `LEEUWENHOEK_WATCHERS=request,exception`. Only enabled watchers appear in the panel menu.

Requests automatically get the tags `endpoint:<name>`, `status:<code>` and `user:<id>`. Commands get `command:<name>` and `exit_code:<code>`. RQ entries get `rq_job:<id>`, `rq_queue:<name>` and `rq_status:<status>`. Log entries get `level:<LEVEL>` and templates `template:<name>`. Slow queries get `slow`, show a yellow **slow** badge in the panel, and the Queries card of a request shows how many of its queries were slow. Repeated queries get `n+1` (see [N+1 queries](#n1-queries)).

### N+1 queries

When a batch closes, its queries are grouped by their normalized SQL: string and number literals and bound parameters (`?`, `%s`, `:name`, `%(name)s`, `$1`) become `?`, lists such as `IN (1, 2, 3)` collapse to `IN (?)`, whitespace is collapsed and keywords are upper-cased. So `select * from users where id = 1` and `SELECT * FROM users WHERE id = 2` are the same statement.

Every query in a group of at least `LEEUWENHOEK_N_PLUS_ONE_THRESHOLD` (default `5`) gets `content["n_plus_one"] = True` and the `n+1` tag, and shows a red **n+1** badge in the panel. The Queries card of the request lists each repeated statement with its count, for example `SELECT * FROM users WHERE id = ? × 12`. Set the threshold to `0` to turn detection off.

The detection runs as an `on_finish` callback, before the batch is saved, so [filters and taggers](#tags) already see the flag and the tag (`lw.filter(lambda e: not e.content.get("n_plus_one"))` works, for example). Queries a filter drops afterwards were still counted.

### Outgoing HTTP

Every call made through `requests` or `httpx` (including `httpx.AsyncClient`) is recorded, whichever client or session made it, and appears in the **HTTP client** section and as a card in the detail of the request that made it. Calls made outside a request are stored as standalone entries. The URL's query parameters and the payload and JSON response keys listed in `LEEUWENHOEK_HIDDEN_PARAMS` are masked, as are the headers in `LEEUWENHOEK_HIDDEN_HEADERS`; bodies are cut at `LEEUWENHOEK_MAX_BODY_KB`. Only JSON and text response bodies are kept, and never for streamed responses (`stream=True`), which are left unread. A `requests` redirect chain is one entry with the final URL and the number of redirects. Entries are tagged `host:<hostname>`, and calls that raised are tagged `failed`.

### `dump()`

```python
from leeuwenhoek import dump


@app.get("/checkout")
def checkout():
    dump(cart)  # shows up in the Dumps section and in this request's batch
    return "ok"
```

### CLI commands

Every command you add to `app.cli` (with `@app.cli.command()`, a nested group or a blueprint's `cli`) gets its own batch, so the queries, logs and exceptions it produces are grouped under it, as they are under a request. Commands registered after `init_app` are recorded too.

```python
@app.cli.command("seed")
@click.argument("count", type=int)
@click.option("--token")
def seed(count, token):
    app.logger.info("seeding %s users", count)
    db.session.execute(...)
```

`flask seed 10 --token=abc` records a `command` entry with `{"count": 10}` as arguments, `{"token": "********"}` as options (values are redacted with `LEEUWENHOEK_HIDDEN_PARAMS`), the exit code and the duration, plus the log line and the query, all in one batch. The exit code is `0`, the code given to `sys.exit()` or `ctx.exit()`, the code of a `click.ClickException` (`2` for a usage error), or `1` when the command raises. An exception raised by the command is recorded in its batch, and the batch is saved even though the command failed.

Like requests, commands are only recorded while Leeuwenhoek is enabled: run them with `flask --debug ...` or set `LEEUWENHOEK_ENABLED`. Skip some with `LEEUWENHOEK_IGNORE_COMMANDS`: an entry matches a command by its full name, by a group prefix (`"db"` skips `db upgrade`) or as a glob (`"seed-*"`). Nothing that happens inside an ignored command is recorded. `flask leeuwenhoek ...`, `flask run` and `flask shell` never get a command entry or a batch: each request served by `flask run` gets its own batch as usual, and a shell session, which can last for hours and isn't one unit of work, stores what you run in it as standalone entries.

### RQ jobs

Jobs are recorded twice, as RQ sees them:

- **The dispatch**: `queue.enqueue(...)` (or `enqueue_call`, or `enqueue_job` with a job built by `Job.create`) records an `rq` entry with `event: "enqueue"` in the current batch, so a request's detail lists the jobs it enqueued, and stores the batch id in `job.meta["leeuwenhoek_parent_batch_id"]`. Its status is `queued`, `deferred` for a job waiting on a dependency, or the outcome for a synchronous queue. Outside a batch (a script), it is stored as `standalone`.
- **Each execution**: the worker runs the job in a new batch (`kind="job"`) whose `parent_batch_id` is the dispatching batch. Its root entry (`event: "perform"`) has the status `finished` or `failed`, the duration and, on failure, the exception class and message. Queries, logs, HTTP calls and the exception raised by the job join this batch, and the job's detail links back to the request. A retried job gets one batch per attempt.

The `rq_job:<id>` tag ties the dispatch and every execution of one job together: the job detail links to them.

```python
queue = Queue(connection=redis)


@app.post("/reports")
def create_report():
    # Recorded kwargs: {"report_id": 7, "token": "********"}
    queue.enqueue(build_report, report_id=7, token="abc")
    return "", 202
```

The worker must create the app (and so call `Leeuwenhoek.init_app`) before it starts, for example with a small script that builds the app and then runs `Worker([queue], connection=redis).work()`. Forking workers (the default `Worker`), `SimpleWorker` and `Queue(is_async=False)` are all covered. A forked work horse ends with `os._exit()`, which skips the `atexit` flush, so the watcher writes the job's batch before handing control back to RQ (waiting up to `LEEUWENHOEK_FLUSH_TIMEOUT`). The worker's own work between jobs (polling, heartbeats, registry cleanup, its log lines) is never recorded. A work horse killed by RQ (`SIGKILL` after a hard timeout) records nothing. Jobs that RQ moves on its own (the scheduler enqueuing a job scheduled with `enqueue_at`, a dependency becoming ready, a retry) are not recorded again as dispatches, only when they run.

`args` and `kwargs` hold the job's original arguments when `args_redacted` is `false`: nothing was masked, truncated or turned into a string to fit in JSON. When it is `true`, they are only for display. `job.description` is not stored, because it repeats the arguments unredacted.

### Peak memory

Set `LEEUWENHOEK_TRACK_MEMORY=true` to record how much memory each request allocated at its peak. It is shown as **Peak memory** in the request detail and stored as `memory_peak_kb` in the entry.

It uses `tracemalloc`, which is off by default because it slows down every allocation while it runs (about 1 ms more per request in the [benchmark](docs/architecture.md#benchmark)). Leeuwenhoek starts tracing when a recorded request begins and stops it when none is running. If tracing was already started by something else (`python -X tracemalloc`, a profiler), it is left running, and only memory allocated during the request counts.

`tracemalloc` measures the whole process, not one thread. With concurrent requests (a threaded server, gevent), each peak includes the other requests in flight, so treat the numbers as approximate there. They are exact when requests are handled one at a time.

## The panel

| URL | Shows |
|---|---|
| `/leeuwenhoek/requests` | Requests (also `/queries`, `/exceptions`, `/logs`, `/dumps`, `/commands`, `/http`, `/cache`, `/rq`), newest first, with search (`?q=`) and tag filter (`?tag=`); `/exceptions?resolved=1` includes resolved families |
| `/leeuwenhoek/batches` | One row per batch |
| `/leeuwenhoek/<type>/<uuid>` | One entry, plus everything else from its batch grouped by type |
| `/leeuwenhoek/batch/<batch_id>` | A whole batch, with its request, command or job on top |
| `/leeuwenhoek/exceptions/family/<hash>` | Every occurrence of one exception, with a Resolve / Reopen button |
| `POST /leeuwenhoek/exceptions/family/<hash>/resolve` (`/reopen`) | Marks the family as resolved (or open again); needs the CSRF token |
| `/leeuwenhoek/<type>/rows?after=<id>` | HTML `<tr>` rows of the entries newer than `<id>`, with the same filters as the listing (used by live polling) |
| `/leeuwenhoek/api/status` | JSON: recording state and counts per type |

Listings update live: every `LEEUWENHOEK_POLL_SECONDS` the panel fetches the new rows and puts them at the top of the table, and refreshes the counters in the menu. Polling pauses while the tab is hidden and doesn't add rows on an older page (`?before=`).

The navbar has buttons to pause or resume recording and to clear all entries. They, like the Resolve / Reopen buttons below, use POST with the panel's own CSRF token. Apps using Flask-WTF don't need any changes, because the panel is exempted from Flask-WTF's CSRF check.

### Resolving exceptions

Once a bug is fixed, mark its exception family as resolved with the **Resolve** button on the Exceptions listing, an exception's detail page or its family page. Resolved families are hidden from the Exceptions listing (and from its live polling); **Show resolved** (`/leeuwenhoek/exceptions?resolved=1`) lists them again, marked as resolved and with a **Reopen** button.

A new occurrence after the resolve time reopens the family by itself: it shows up in the listing again with all its occurrences. The state lives in the settings table (`resolved:<family_hash>` with the resolve time), so every instance sharing the database sees it.

## Configuration

Each setting is read from `app.config["LEEUWENHOEK_<KEY>"]` first, then from an environment variable with the same name. In environment variables, booleans accept `1/true/yes/on`, lists accept JSON or comma-separated values, and `BASIC_AUTH` uses the form `user:password`.

| Key | Default | Description |
|---|---|---|
| `LEEUWENHOEK_ENABLED` | `app.debug` | Master switch |
| `LEEUWENHOEK_URL_PREFIX` | `/leeuwenhoek` | Where the panel is mounted. Requests under it are never recorded. |
| `LEEUWENHOEK_WATCHERS` | every watcher whose library is installed | Watchers to enable. A listed watcher whose library is missing is skipped with a warning. |
| `LEEUWENHOEK_IGNORE_PATHS` | `["/static*", "/favicon.ico", "/health*"]` | Glob patterns of paths that are not recorded |
| `LEEUWENHOEK_IGNORE_COMMANDS` | `[]` | CLI commands that are not recorded: full names, group prefixes or glob patterns, see [CLI commands](#cli-commands) |
| `LEEUWENHOEK_HIDDEN_HEADERS` | `["authorization", "cookie", "x-api-key"]` | Headers replaced by `********` |
| `LEEUWENHOEK_HIDDEN_PARAMS` | `["password", "password_confirmation", "token", "secret"]` | Keys replaced by `********` in bodies, query strings and sessions, at any depth |
| `LEEUWENHOEK_MAX_BODY_KB` | `64` | Bodies larger than this are truncated |
| `LEEUWENHOEK_MAX_ENTRIES_PER_BATCH` | `500` | Maximum entries per batch. The request entry itself is always kept. |
| `LEEUWENHOEK_SLOW_QUERY_MS` | `100` | Queries that take longer than this many milliseconds are flagged as slow and tagged `slow` |
| `LEEUWENHOEK_CACHE_VALUES` | `False` | Store the values the cache watcher reads and writes, truncated to `LEEUWENHOEK_MAX_BODY_KB`. Fields named in `LEEUWENHOEK_HIDDEN_PARAMS` are redacted inside values, and the whole value is replaced by `********` when its key contains one of those names (`user:1:token`). |
| `LEEUWENHOEK_N_PLUS_ONE_THRESHOLD` | `5` | Queries with the same normalized SQL repeated this many times in a batch are tagged `n+1`, see [N+1 queries](#n1-queries). `0` disables it. |
| `LEEUWENHOEK_REQUEST_TAGS` | `{}` | Tags built from request data, see [Tags](#tags) |
| `LEEUWENHOEK_SAMPLE_RATE` | `1.0` | Fraction of requests to record (0–1) |
| `LEEUWENHOEK_ONLY_ERRORS` | `False` | Keep only batches with an exception or a 5xx response |
| `LEEUWENHOEK_TRACK_MEMORY` | `False` | Record each request's peak memory with `tracemalloc`, see [Peak memory](#peak-memory) |
| `LEEUWENHOEK_HANDLED_EXCEPTION_MIN_STATUS` | `500` | Record exceptions handled by your error handlers when the response status is at least this |
| `LEEUWENHOEK_STORAGE` | `sqlalchemy` | `sqlalchemy` or `memory` (for tests) |
| `LEEUWENHOEK_DATABASE_URI` | `sqlite:///<instance_path>/leeuwenhoek.db` | Storage database, separate from your app's |
| `LEEUWENHOEK_DB_DRIVER`, `_USERNAME`, `_PASSWORD`, `_HOST`, `_PORT`, `_DATABASE`, `_SOCKET` | — | Build the URI from parts instead. The driver defaults to `postgresql+psycopg`. `_SOCKET` takes a Cloud SQL instance connection name (`project:region:instance`). |
| `LEEUWENHOEK_ENGINE_OPTIONS` | `{}` | Extra `create_engine()` arguments (pool size, etc.) |
| `LEEUWENHOEK_AUTO_INSTALL` | `True` | Create the tables in `init_app` |
| `LEEUWENHOEK_ASYNC_WRITE` | `True` | Write batches in a background thread |
| `LEEUWENHOEK_QUEUE_MAX_BATCHES` | `1000` | Pending batches kept in memory. When the queue is full, new batches are dropped (with a warning) and requests are never blocked. |
| `LEEUWENHOEK_FLUSH_TIMEOUT` | `5.0` | Seconds to wait for pending writes at exit |
| `LEEUWENHOEK_PRUNE_HOURS` | `24` | Retention period |
| `LEEUWENHOEK_AUTO_PRUNE` | `True` | Prune old entries from the background writer |
| `LEEUWENHOEK_PRUNE_INTERVAL_MINUTES` | `30` | How often to prune |
| `LEEUWENHOEK_BASIC_AUTH` | `None` | `("user", "password")` to protect the panel |
| `LEEUWENHOEK_PAGE_SIZE` | `50` | Rows per page in the panel |
| `LEEUWENHOEK_POLL_SECONDS` | `4` | How often the panel polls for new rows and menu counts. `0` disables live polling. |
| `LEEUWENHOEK_BULMA_CDN` | `False` | Load Bulma from a CDN instead of the bundled copy |

## Access control

- **Debug mode:** the panel is open.
- **Outside debug mode:** the panel returns 403 unless you configure access. Use either a rule:

  ```python
  from flask_login import current_user

  lw.authorize(lambda: current_user.is_authenticated and current_user.is_admin)
  ```

  or basic auth:

  ```bash
  LEEUWENHOEK_BASIC_AUTH=admin:s3cret
  ```

  If both are set, both must pass.
- **When Leeuwenhoek is disabled** (`LEEUWENHOEK_ENABLED` is false), the panel returns 404.

## Tags

Tags make entries searchable from the panel with `?tag=`. There are three ways to add them:

```python
# 1. From request data, with dotted paths (lists are traversed, and JSON or base64 JSON strings are parsed)
app.config["LEEUWENHOEK_REQUEST_TAGS"] = {
    "order": "body.order_id",  # -> order:1234
    "sku": "body.items.sku",  # -> one tag per item
    "tenant": ["headers.X-Tenant-Id", "query.tenant"],
    "event": "body.message.data.event_type",  # a Pub/Sub push message
}


# 2. A tagger callback, run for every entry before it is saved
@lw.tag
def tenant_tag(entry):
    return [f"tenant:{g.tenant_id}"] if hasattr(g, "tenant_id") else []


# 3. From anywhere while a request is being handled; the tags reach every entry in its batch
from leeuwenhoek import add_tags

add_tags(f"customer:{customer.id}")
```

## Filters

A filter decides which entries are stored. It receives an entry and returns `True` to keep it. Several can be registered, and `lw.filter` works as a decorator too:

```python
# Skip "304 Not Modified" responses
lw.filter(lambda e: not (e.type == "request" and e.content["status"] == 304))


@lw.filter
def no_debug_logs(entry):
    return not (entry.type == "log" and entry.content["level"] == "DEBUG")
```

Filters run when a batch is saved, before tagging. If a filter drops the root entry of a batch (the request entry, for example), the whole batch is dropped. A filter that raises is logged and the entry is kept.

## Running in production or staging

Leeuwenhoek is built for development first. For shared environments, a reasonable setup is:

```bash
LEEUWENHOEK_ENABLED=true
LEEUWENHOEK_SAMPLE_RATE=0.1          # or LEEUWENHOEK_ONLY_ERRORS=true
LEEUWENHOEK_BASIC_AUTH=admin:...     # or lw.authorize(...)
LEEUWENHOEK_DB_HOST=10.0.0.5         # Postgres instead of SQLite
LEEUWENHOEK_DB_USERNAME=leeuwenhoek
LEEUWENHOEK_DB_PASSWORD=...
LEEUWENHOEK_DB_DATABASE=leeuwenhoek
```

For Postgres, install a driver too, for example `psycopg[binary]`.

Things to know:
- Writes never block a request. If the database is slow or down, batches are dropped and a warning is logged.
- Every instance prunes old entries on its own schedule, with jitter so they don't all prune at once.
- Designed for gunicorn (including `--preload`, since the writer thread is restarted after a fork), uWSGI, the Werkzeug dev server and async views.

## CLI

```bash
flask leeuwenhoek install            # create the tables (done automatically unless AUTO_INSTALL is off)
flask leeuwenhoek prune --hours=48   # delete entries older than 48 hours
flask leeuwenhoek clear              # delete everything
flask leeuwenhoek pause              # stop recording (shared by all instances)
flask leeuwenhoek resume
```

## Testing your app with Leeuwenhoek

Use the in-memory storage and synchronous writes, so entries can be checked right after a request:

```python
app.config.update(
    LEEUWENHOEK_ENABLED=True,
    LEEUWENHOEK_STORAGE="memory",
    LEEUWENHOEK_ASYNC_WRITE=False,
)
lw = Leeuwenhoek(app)
...
lw.storage.list("request")
lw.close()  # remove global handlers and listeners
```

## Roadmap

Planned work is tracked in [GitHub issues](https://github.com/Manuel-Espinosa/flask-leeuwenhoek/issues):

- **[v0.2](https://github.com/Manuel-Espinosa/flask-leeuwenhoek/milestone/1):** watchers for outgoing HTTP, templates, CLI commands, mail, cache and Celery jobs. Slow-query and N+1 detection, `lw.filter()`, live polling and dark mode.
- **[v0.3](https://github.com/Manuel-Espinosa/flask-leeuwenhoek/milestone/2):** watchers for RQ, ORM models, Redis, scheduled tasks and blinker events. Retrying failed jobs, resolving exceptions, and publishing to PyPI.

Out of scope: distributed tracing or APM (use OpenTelemetry), aggregated metrics, and alerting.

## Development

```bash
uv sync                      # install dependencies and dev tools
uv run pytest                # tests
uv run ruff check .          # lint
uv run ruff format .         # format
uv build                     # sdist + wheel
```

See [docs/architecture.md](docs/architecture.md) for how it works inside.

## License

MIT
