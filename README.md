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

Add the `redis` extra (`flask-leeuwenhoek[redis]`) to record the commands sent with [redis-py](https://redis.readthedocs.io/). The Redis watcher turns on by itself when `redis` is installed.

Add the `celery` extra (`flask-leeuwenhoek[celery]`) to record [Celery](https://docs.celeryq.dev/) tasks, see [Celery jobs](#celery-jobs). The watcher turns on by itself when Celery is installed.

Add the `http` extra to install [requests](https://requests.readthedocs.io/) and [HTTPX](https://www.python-httpx.org/) for the outgoing HTTP watcher. Either one is enough, and the watcher turns on by itself if your app already uses one of them.

Add the `sched` extra (`flask-leeuwenhoek[sched]`) to record [APScheduler](https://apscheduler.readthedocs.io/en/3.x/) job runs. Only APScheduler 3.x is supported (`apscheduler>=3.10,<4`); version 4 has a different API. The watcher turns on by itself when APScheduler is installed.

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
| `job` | Celery task runs: task name and id, args and kwargs, queue, attempt, state (`SUCCESS`, `FAILURE`, `RETRY`), duration, result, exception and retry reason. Each run gets its own batch, linked to the request that dispatched it, see [Celery jobs](#celery-jobs). |
| `http` | Outgoing HTTP calls made with [requests](https://requests.readthedocs.io/) or [HTTPX](https://www.python-httpx.org/) (sync and async clients): method, URL, request headers, payload, status, response headers, response body (JSON/text only), duration, redirects, and the error if the call raised. Enabled when either library is installed. |
| `cache` | Flask-Caching operations (`get`, `get_many`, `get_dict`, `has`, `set`, `add`, `set_many`, `delete`, `delete_many`, `unlink`, `clear`), including the ones made by `@cache.cached` and `@cache.memoize` (with Flask-Caching 2.5 or newer; older versions, the last ones for Python 3.10, make the decorators talk to the backend directly and only the direct calls are recorded): keys, hit or miss for reads, TTL for writes, backend and duration. Values are stored only with `LEEUWENHOEK_CACHE_VALUES`. The Cache card of a request shows its hit and miss counts. |
| `model` | Inserts, updates and deletes made through the SQLAlchemy ORM, in any `Session` (Flask-SQLAlchemy's or your own): model, table, action (`created`, `updated`, `deleted`), primary key and the changed columns with their old and new values. Recorded when the transaction commits, see [ORM models](#orm-models). |
| `event` | Blinker signals: the Flask signals in `LEEUWENHOEK_SIGNALS` (by default `message_flashed`) and your own signals added with `lw.watch_signal()`: signal name, sender (class name or truncated `repr`) and keyword arguments (redacted with `LEEUWENHOEK_HIDDEN_PARAMS`, truncated to `LEEUWENHOEK_MAX_BODY_KB`), tagged `event:<name>`. See [Signals](#signals). |
| `redis` | Commands sent with [redis-py](https://redis.readthedocs.io/) (`redis.Redis` and `redis.asyncio.Redis`): command, arguments (redacted and truncated), server, database, duration and the error if the command raised. A pipeline is one entry with its list of commands. See [Redis](#redis). |
| `schedule` | Each run of an APScheduler 3.x job: id, name, function, trigger, executor, scheduled and next run time, arguments (redacted with `LEEUWENHOEK_HIDDEN_PARAMS`), result (`repr`, truncated) or exception, and duration; also runs missed past their misfire grace time and runs skipped because the job reached `max_instances`. Each run gets its own batch, see [Scheduled jobs](#scheduled-jobs). Needs the `sched` extra. |
| `rq` | RQ jobs: function path, queue, job id, status, arguments and keyword arguments (redacted with `LEEUWENHOEK_HIDDEN_PARAMS`, truncated to `LEEUWENHOEK_MAX_BODY_KB`), duration and exception. Each execution gets its own batch, linked to the request that enqueued the job, see [RQ jobs](#rq-jobs). Needs the `rq` extra. |

By default every watcher whose library is installed is enabled, so integration watchers (mail, Celery, Redis...) turn on by themselves when you install the package they observe. Set `LEEUWENHOEK_WATCHERS` to pick them explicitly, e.g. `LEEUWENHOEK_WATCHERS=request,exception`. Only enabled watchers appear in the panel menu.

Requests automatically get the tags `endpoint:<name>`, `status:<code>` and `user:<id>`. Commands get `command:<name>` and `exit_code:<code>`, Celery tasks `task:<name>` and `state:<STATE>`, RQ entries `rq_job:<id>`, `rq_queue:<name>` and `rq_status:<status>`, scheduled job runs `job:<id>` and `job_status:<success|error|missed|skipped>`. Log entries get `level:<LEVEL>`, templates `template:<name>` and ORM changes `model:<Name>`. Slow queries get `slow`, show a yellow **slow** badge in the panel, and the Queries card of a request shows how many of its queries were slow. Repeated queries get `n+1` (see [N+1 queries](#n1-queries)).

### N+1 queries

When a batch closes, its queries are grouped by their normalized SQL: string and number literals and bound parameters (`?`, `%s`, `:name`, `%(name)s`, `$1`) become `?`, lists such as `IN (1, 2, 3)` collapse to `IN (?)`, whitespace is collapsed and keywords are upper-cased. So `select * from users where id = 1` and `SELECT * FROM users WHERE id = 2` are the same statement.

Every query in a group of at least `LEEUWENHOEK_N_PLUS_ONE_THRESHOLD` (default `5`) gets `content["n_plus_one"] = True` and the `n+1` tag, and shows a red **n+1** badge in the panel. The Queries card of the request lists each repeated statement with its count, for example `SELECT * FROM users WHERE id = ? × 12`. Set the threshold to `0` to turn detection off.

The detection runs as an `on_finish` callback, before the batch is saved, so [filters and taggers](#tags) already see the flag and the tag (`lw.filter(lambda e: not e.content.get("n_plus_one"))` works, for example). Queries a filter drops afterwards were still counted.

### Outgoing HTTP

Every call made through `requests` or `httpx` (including `httpx.AsyncClient`) is recorded, whichever client or session made it, and appears in the **HTTP client** section and as a card in the detail of the request that made it. Calls made outside a request are stored as standalone entries. The URL's query parameters and the payload and JSON response keys listed in `LEEUWENHOEK_HIDDEN_PARAMS` are masked, as are the headers in `LEEUWENHOEK_HIDDEN_HEADERS`; bodies are cut at `LEEUWENHOEK_MAX_BODY_KB`. Only JSON and text response bodies are kept, and never for streamed responses (`stream=True`), which are left unread. A `requests` redirect chain is one entry with the final URL and the number of redirects. Entries are tagged `host:<hostname>`, and calls that raised are tagged `failed`.

### ORM models

The `model` watcher listens to `after_flush` on SQLAlchemy's `Session` class, so every session is covered without configuration. For each object a flush inserts, updates or deletes it records the model class, the table, the action, the primary key (also for new rows, whose key the database just assigned) and the changes, as `{"column": {"old": ..., "new": ...}}`:

- **created**: every loaded column, from `null`.
- **updated**: only the columns that really changed (an object in `session.dirty` whose values are the same is skipped). An old value that was never loaded is `null`.
- **deleted**: every loaded column, to `null`.

Only columns are recorded; relationships are skipped, since their foreign key columns already show the link. Values are made JSON-safe (datetimes as ISO strings, `Decimal` as a string, binary data as `<N bytes>`), columns named in `LEEUWENHOEK_HIDDEN_PARAMS` are masked, and each value is cut at `LEEUWENHOEK_MAX_BODY_KB`. Nothing is ever loaded from the database to fill a value.

Changes are captured at flush time, while their history still exists, but only **recorded when the transaction commits**. Changes flushed and then rolled back (including a rolled-back `begin_nested()` savepoint) or left uncommitted when the session closes are dropped, so the panel only shows what reached the database. A commit made after the request ended is stored as standalone. Leeuwenhoek's own storage (its engine and its tables) is never recorded.

### Signals

Watch your own [blinker](https://blinker.readthedocs.io/) signals with `lw.watch_signal()`, before or after `init_app`. Every send is recorded as an `event` entry in the Events section and in the batch of the request, command or job that sent it, or as a standalone entry outside one. Watching a signal twice does nothing.

```python
from blinker import signal

order_placed = signal("order-placed")
lw.watch_signal(order_placed)  # or lw.watch_signal("order-placed")
lw.watch_signal(anonymous_signal, name="cart-emptied")  # a name for a Signal() without one


@app.post("/orders")
def create_order():
    order_placed.send(current_user, order_id=7, total=99.5)
```

Flask's own signals are picked by name with `LEEUWENHOEK_SIGNALS` (`message_flashed`, `request_started`, `request_finished`, `request_tearing_down`, `appcontext_pushed`, `appcontext_popped`, `appcontext_tearing_down`, `template_rendered`, `before_render_template`, `got_request_exception`), and only the ones the app sends are recorded. The default is just `message_flashed`: the request and app context signals fire on every request and repeat what the request entry already shows. `request_started` fires before the request's batch opens and `request_tearing_down` and the app context ones after it closes, so they are stored as standalone entries.

A signal that another active watcher already records is never recorded twice: `template_rendered` and `before_render_template` while the `template` watcher is on, `got_request_exception` while the `exception` watcher is on, and Flask-Mail's `email_dispatched` while the `mail` watcher is on. Signals sent while the panel is served, for ignored paths, while recording is paused, or by Leeuwenhoek itself are never recorded.

### Redis

Every command sent through `redis.Redis` or `redis.asyncio.Redis` (and their subclasses, such as `fakeredis`) is recorded, whichever client sent it, and appears in the **Redis** section and as a card, with the count and total time, in the detail of the request that sent it. Commands sent outside a request are standalone entries. A pipeline (`r.pipeline()`, with or without a transaction) is one `PIPELINE` entry listing its commands, the error of each one that failed with `raise_on_error=False`, and the time of the whole round trip; its queued commands are not recorded again. Commands that a pipeline in `WATCH` mode sends right away are recorded on their own. Return values are never stored.

Arguments are decoded as UTF-8 (binary values are shown as `[binary: N bytes]`); at most 20 are kept per command, each cut at 200 characters, and at most 50 commands per pipeline. Passwords are never stored: the arguments of `AUTH`, the password of `HELLO ... AUTH` and `MIGRATE ... AUTH`/`AUTH2`, `CONFIG SET requirepass`/`masterauth` and the password rules of `ACL SETUSER` are replaced by `********`. When a key contains a name in `LEEUWENHOEK_HIDDEN_PARAMS` (`user:1:token`), every argument after it is masked, and a hash field or argument that contains one (`HSET user:1 password ...`) masks the value that follows it. Commands that raised are tagged `failed`.

Libraries that use Redis are recorded at the command level too. With Flask-Caching's `RedisCache`, a `cache.get()` gives a `cache` entry and the `GET` it sent as a `redis` entry, in the same batch. RQ's commands are recorded as well: those sent while enqueueing join the request, and those a worker sends between jobs (polling, heartbeats) are standalone; drop them with [`lw.filter()`](#tags) or leave `redis` out of `LEEUWENHOEK_WATCHERS` if they are noise. Redis Cluster clients (`RedisCluster`) and Pub/Sub connections are not covered.

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

### Scheduled jobs

Every run of an [APScheduler](https://apscheduler.readthedocs.io/en/3.x/) 3.x job gets its own batch, with a `schedule` entry as its root, so the queries, logs and exceptions of one run are grouped under it, as they are under a request. It works with every scheduler (`BackgroundScheduler`, `BlockingScheduler`, `AsyncIOScheduler`...), every job store and coroutine jobs.

```python
scheduler = BackgroundScheduler()


@scheduler.scheduled_job("interval", minutes=5, id="sync")
def sync_users():
    with app.app_context():  # needed to use Flask-SQLAlchemy and friends
        app.logger.info("syncing users")
        db.session.execute(...)
```

Leeuwenhoek never pushes an app context for a job: a job that needs one must push it itself, as it would without Leeuwenhoek, so the job behaves the same whether it is installed or not. Recording needs no app context.

A run records the job id and name, the function, the trigger (`interval[0:05:00]`, `cron[...]`), the executor, the time it was scheduled for and the next run time, the arguments, its duration, and its `repr()`'d result (cut at 1000 characters) or the exception it raised, which is also recorded in the batch by the exception watcher (as handled, since APScheduler catches it). Runs missed by more than the job's `misfire_grace_time` and runs skipped because the job already had `max_instances` running are recorded as `missed` and `skipped` entries, without a batch of their own. A run started while a batch is current in the scheduler's thread (a `flask` command running a `BlockingScheduler`, for example) keeps that batch as its parent.

APScheduler logs `Running job ...` before a run and `Job ... raised an exception` after it, outside the run, so the log watcher stores these lines as standalone entries.

With the `ProcessPoolExecutor`, the job runs in another process and is **not recorded**: Leeuwenhoek's wrapper does not cross the process boundary, and the child process gets the original job. Use a thread pool (the default) or record from inside the job. Celery beat is not part of this watcher: scheduled Celery tasks are recorded by the Celery watcher.

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

### Celery jobs

Pass your Celery app to `init_app` (or to the constructor):

```python
celery = Celery(__name__, broker="redis://localhost:6379/0")
lw = Leeuwenhoek()
lw.init_app(app, celery=celery)
```

Every run of one of its tasks gets its own batch (`kind="job"`), so the queries, logs and exceptions of the task are grouped under it, and the batch page shows the job on top. Without `celery=`, the tasks of every Celery app in the process are recorded (Celery's signals are global). The worker must create the Flask app and call `init_app` too (the usual Flask + Celery setup does), and Leeuwenhoek must be enabled there (`LEEUWENHOEK_ENABLED`).

When a task is dispatched while a request (or a command, or another job) is being recorded, the id of that batch travels in the message headers, and the job's entries get it as `parent_batch_id`. The dispatching batch also gets a `job` entry with the status `dispatched`, shown in the Jobs card of the request detail with a **View job** link to the job's batch; the job's page links back with **Dispatched by**. A retry (`self.retry()`) is a new run with its own batch and the same parent. The **Jobs** listing shows the runs only.

In eager mode (`task_always_eager`, or `task.apply()`), Celery doesn't publish a message, so there are no headers: the task runs inside the batch that dispatched it, and that batch becomes its parent. Tasks dispatched outside any batch (a script, `celery beat`) get a job batch without a parent.

Args and kwargs are redacted with `LEEUWENHOEK_HIDDEN_PARAMS` (by key, at any depth) and cut at `LEEUWENHOEK_MAX_BODY_KB`, like the result. `content["args_redacted"]` is `false` only when the stored `args` and `kwargs` are exactly the ones the task received (nothing masked, truncated or unserializable), so the job could be dispatched again with them.

### Peak memory

Set `LEEUWENHOEK_TRACK_MEMORY=true` to record how much memory each request allocated at its peak. It is shown as **Peak memory** in the request detail and stored as `memory_peak_kb` in the entry.

It uses `tracemalloc`, which is off by default because it slows down every allocation while it runs (about 1 ms more per request in the [benchmark](docs/architecture.md#benchmark)). Leeuwenhoek starts tracing when a recorded request begins and stops it when none is running. If tracing was already started by something else (`python -X tracemalloc`, a profiler), it is left running, and only memory allocated during the request counts.

`tracemalloc` measures the whole process, not one thread. With concurrent requests (a threaded server, gevent), each peak includes the other requests in flight, so treat the numbers as approximate there. They are exact when requests are handled one at a time.

## The panel

| URL | Shows |
|---|---|
| `/leeuwenhoek/requests` | Requests (also `/queries`, `/exceptions`, `/logs`, `/dumps`, `/commands`, `/http`, `/cache`, `/models`, `/events`, `/redis`, `/schedule`, `/rq`, `/jobs`), newest first, with search (`?q=`) and tag filter (`?tag=`); `/exceptions?resolved=1` includes resolved families |
| `/leeuwenhoek/batches` | One row per batch |
| `/leeuwenhoek/<type>/<uuid>` | One entry, plus everything else from its batch grouped by type |
| `/leeuwenhoek/batch/<batch_id>` | A whole batch, with its request, command or scheduled job run, RQ job or Celery task on top |
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
| `LEEUWENHOEK_SIGNALS` | `["message_flashed"]` | Flask signals the event watcher records, by name, see [Signals](#signals) |
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
