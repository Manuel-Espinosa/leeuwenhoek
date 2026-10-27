# Leeuwenhoek — Especificación

Extensión para Flask que funciona **dentro de la misma app** como un microscopio de depuración: registra todo lo que pasa (requests, queries, excepciones, logs, jobs, etc.), lo agrupa por ciclo de ejecución y lo muestra en un panel embebido en `/leeuwenhoek`, renderizado con **Jinja + Bulma**.

- **Paquete:** `flask-leeuwenhoek`
- **Import:** `leeuwenhoek`
- **Gestión del proyecto:** `uv`

> Estado: solo especificación. La implementación empieza en v0.1.

---

## 1. Objetivos

- Se instala con `uv add flask-leeuwenhoek` y se activa con dos líneas: `Leeuwenhoek(app)` o `lw.init_app(app)`.
- Vive dentro de la app: se registra como un **Blueprint** y no necesita servidor, proceso ni frontend aparte.
- No hay que tocar el código de negocio: todo se captura con hooks, señales y eventos.
- Todas las entradas de un mismo ciclo (request HTTP, job o comando CLI) se agrupan bajo un `batch_id`.
- El panel se renderiza en el servidor con Jinja + Bulma, sin build de frontend.
- Pensado para **desarrollo/staging**. En producción, solo con filtros, muestreo y auth.

### No objetivos (v1)
- APM distribuido (para eso está OpenTelemetry).
- Métricas agregadas o series de tiempo.
- Alertas.

---

## 2. Arquitectura

```
flask-leeuwenhoek/
├── pyproject.toml
├── uv.lock
├── README.md
├── src/
│   └── leeuwenhoek/
│       ├── __init__.py          # Clase Leeuwenhoek (init_app), dump()
│       ├── config.py            # Defaults y lectura de app.config
│       ├── context.py           # batch_id y buffer por ciclo (contextvars)
│       ├── models.py            # Tablas de entradas y tags
│       ├── storage/
│       │   ├── base.py          # Interfaz StorageDriver
│       │   ├── sqlalchemy.py    # Driver SQL (SQLite por defecto)
│       │   └── memory.py        # Driver en memoria (tests)
│       ├── watchers/
│       │   ├── base.py          # Watcher abstracto (register, record)
│       │   ├── request.py
│       │   ├── exception.py
│       │   ├── query.py
│       │   ├── log.py
│       │   ├── job.py           # Celery / RQ
│       │   ├── cache.py
│       │   ├── http_client.py   # requests / httpx salientes
│       │   ├── mail.py          # Flask-Mail
│       │   ├── command.py       # flask CLI / Click
│       │   ├── event.py         # señales blinker
│       │   ├── model.py         # eventos ORM de SQLAlchemy
│       │   ├── redis.py
│       │   ├── schedule.py      # APScheduler / Celery beat
│       │   ├── template.py      # template_rendered
│       │   └── dump.py          # leeuwenhoek.dump(var)
│       ├── filters.py           # Ignorar rutas, muestreo, redacción
│       ├── auth.py              # Control de acceso al panel
│       ├── blueprint.py         # Vistas Jinja + endpoints JSON mínimos
│       ├── cli.py               # flask leeuwenhoek ...
│       ├── templates/
│       │   └── leeuwenhoek/
│       │       ├── base.html    # Layout Bulma (navbar + menú lateral)
│       │       ├── _macros.html # Tablas, tags, badges, JSON viewer
│       │       ├── index.html   # Listado genérico por tipo
│       │       ├── batch.html   # Detalle de un batch completo
│       │       └── entries/     # Un parcial por tipo (request.html, query.html, ...)
│       └── static/
│           └── leeuwenhoek/
│               ├── bulma.min.css      # Vendorizado (funciona offline)
│               ├── leeuwenhoek.css    # Ajustes mínimos
│               ├── leeuwenhoek.js     # Polling, tabs, copiar, toggles (vanilla JS)
│               └── highlight/         # Resaltado de SQL/JSON/tracebacks
└── tests/
```

### Flujo
1. `before_request` genera el `batch_id` (UUID) y lo guarda en un `contextvars`.
2. Los watchers registran entradas en un **buffer en memoria** del ciclo actual.
3. En `teardown_request` se aplican filtros y redacción, y se persiste todo el buffer en **un solo insert**, en un hilo de fondo para no bloquear la respuesta.
4. El Blueprint `/leeuwenhoek` lee del storage y renderiza con Jinja.

### Aislamiento dentro de la app
- Los templates viven en `templates/leeuwenhoek/` para no chocar con los de la app.
- Los estáticos se sirven desde el propio Blueprint (`static_url_path="/leeuwenhoek/static"`).
- Usa su **propio engine de SQLAlchemy** (`LEEUWENHOEK_DATABASE_URI`), así sus queries no se mezclan con las de la app ni se graban a sí mismas.
- Las rutas `/leeuwenhoek*` se ignoran siempre.
- No usa el `db.session` de la app ni modifica su `app.logger`. Solo agrega un handler.

---

## 3. Gestión con uv

### Crear el proyecto
```bash
uv init --lib flask-leeuwenhoek
cd flask-leeuwenhoek
uv add flask sqlalchemy
uv add --dev pytest pytest-cov ruff mypy
```

### `pyproject.toml` (base)
```toml
[project]
name = "flask-leeuwenhoek"
version = "0.1.0"
description = "Panel de depuración embebido para Flask"
readme = "README.md"
requires-python = ">=3.10"
license = "MIT"
dependencies = [
    "flask>=2.3",
    "sqlalchemy>=2.0",
]

[project.optional-dependencies]
login   = ["flask-login>=0.6"]
celery  = ["celery>=5.3"]
rq      = ["rq>=1.15"]
cache   = ["flask-caching>=2.0"]
mail    = ["flask-mail>=0.9"]
redis   = ["redis>=5.0"]
http    = ["requests>=2.31", "httpx>=0.27"]
sched   = ["apscheduler>=3.10"]
all     = ["flask-leeuwenhoek[login,celery,rq,cache,mail,redis,http,sched]"]

[project.entry-points."flask.commands"]
leeuwenhoek = "leeuwenhoek.cli:cli"

[build-system]
requires = ["uv_build"]   # deja el rango de versión que genere `uv init --lib`
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "leeuwenhoek"

[dependency-groups]
dev = ["pytest", "pytest-cov", "ruff", "mypy"]

[tool.ruff]
line-length = 100
```

> Los templates y estáticos dentro de `src/leeuwenhoek/` se incluyen solos en el wheel con `uv_build`. Hay que verificarlo con `uv build` y revisando el contenido del `.whl`.

### Comandos del día a día
```bash
uv sync                                  # instala deps + dev
uv run pytest                            # tests
uv run ruff check . && uv run ruff format .
uv run flask --app examples/demo run --debug
uv build                                 # sdist + wheel
uv publish                               # PyPI (cuando toque)
```

### Uso en una app consumidora
```bash
uv add flask-leeuwenhoek                 # núcleo
uv add "flask-leeuwenhoek[celery,cache]" # con integraciones
uv add --editable ../flask-leeuwenhoek   # desarrollo local del paquete
```

### App de ejemplo
`examples/demo/` con Flask + SQLAlchemy + una tarea Celery falsa, para probar todos los watchers con `uv run`.

---

## 4. Modelo de datos

Tabla principal `leeuwenhoek_entries`:

| Columna        | Tipo           | Notas |
|----------------|----------------|-------|
| `id`           | bigint PK      | |
| `uuid`         | uuid, único    | ID público de la entrada |
| `batch_id`     | uuid, índice   | Agrupa las entradas de un request, job o comando |
| `parent_batch_id` | uuid, nullable | Enlaza un job con el request que lo despachó |
| `family_hash`  | string, índice, nullable | Agrupa excepciones iguales |
| `type`         | string, índice | `request`, `query`, `exception`, `log`, `job`, ... |
| `content`      | JSON           | Payload específico del tipo |
| `is_root`      | bool           | Se muestra en el listado principal |
| `created_at`   | datetime, índice | |

Tabla `leeuwenhoek_tags`:

| Columna      | Tipo |
|--------------|------|
| `entry_uuid` | uuid FK |
| `tag`        | string, índice |

> Índices recomendados: `(type, created_at)`, `(batch_id)`, `(family_hash)`, `(tag)`.

---

## 5. Watchers

### 5.1 Requests
- **Hook:** `before_request` / `after_request` / `teardown_request`.
- **Datos:** método, URL, regla (`request.url_rule`), endpoint, blueprint, IP, headers (redactados), query params, body (truncado), sesión, usuario (`flask_login.current_user` si existe), status, headers de respuesta, body de respuesta (truncado, solo JSON/texto), duración en ms y memoria pico (opcional, con `tracemalloc`).
- **Tags:** `endpoint:<name>`, `status:<code>`, `user:<id>`.

### 5.2 Excepciones
- **Hook:** señal `got_request_exception`, además de `sys.excepthook` para CLI y jobs.
- **Datos:** clase, mensaje, archivo, línea, traceback completo, snippet de código (±5 líneas).
- **family_hash:** `sha1(clase + archivo + línea)`.

### 5.3 Queries (SQLAlchemy)
- **Hook:** `before_cursor_execute` y `after_cursor_execute` sobre el engine de la app.
- **Datos:** SQL, parámetros, SQL interpolado (solo para mostrar), duración, nombre del engine, y archivo y línea de origen (recorriendo el stack y saltando los frames de SQLAlchemy y Flask).
- **Flags:** `slow` si supera `LEEUWENHOEK_SLOW_QUERY_MS`.
- **N+1:** se marca la misma query normalizada repetida más de `LEEUWENHOEK_N_PLUS_ONE_THRESHOLD` veces en un batch.

### 5.4 Logs
- **Hook:** un `logging.Handler` propio agregado al root logger.
- **Datos:** nivel, mensaje, logger, `extra` y `exc_info`.

### 5.5 Jobs (Celery / RQ)
- **Celery:** `before_task_publish`, `task_prerun`, `task_postrun`, `task_failure`, `task_retry`.
- **Datos:** tarea, args/kwargs (redactados), cola, intentos, estado, duración y excepción.
- **Enlace:** el `batch_id` del request se propaga en los headers de la tarea y queda como `parent_batch_id` en el worker.
- **RQ:** `on_success` / `on_failure` o un wrapper del `Worker`.

### 5.6 Cache
- **Hook:** wrapper de `flask_caching.Cache` (`get`, `set`, `delete`, `clear`).
- **Datos:** operación, key, hit/miss, TTL y valor (truncado, opcional).

### 5.7 HTTP saliente
- **requests:** patch de `Session.send`. **httpx:** `event_hooks`.
- **Datos:** método, URL, headers (redactados), payload, status, respuesta (truncada) y duración.

### 5.8 Mail
- **Hook:** señal `email_dispatched` de Flask-Mail.
- **Datos:** remitente, destinatarios, asunto, HTML/texto y los nombres de los adjuntos.
- **UI:** preview del HTML en un `<iframe sandbox>`.

### 5.9 Comandos CLI
- **Hook:** wrapper de `click.Command.invoke` en `app.cli`.
- **Datos:** comando, argumentos, opciones, exit code y duración.

### 5.10 Eventos (señales blinker)
- **Hook:** señales de Flask y señales custom registradas con `lw.watch_signal(signal)`.
- **Datos:** nombre de la señal, sender y kwargs serializados.

### 5.11 Modelos (ORM)
- **Hook:** `Session.after_flush` (inserts, updates y deletes).
- **Datos:** modelo, acción, PK y cambios (`inspect(obj).attrs[x].history`).

### 5.12 Redis
- **Hook:** patch de `redis.Redis.execute_command`.
- **Datos:** comando, argumentos y duración.

### 5.13 Tareas programadas
- **Hook:** listeners de APScheduler (`EVENT_JOB_EXECUTED`, `EVENT_JOB_ERROR`) o Celery beat.
- **Datos:** job, trigger, próxima ejecución y resultado.

### 5.14 Templates
- **Hook:** señal `template_rendered`.
- **Datos:** template, las keys del contexto y la duración.

### 5.15 Dumps
- **API:** `from leeuwenhoek import dump; dump(obj)`.
- **Datos:** `pprint`/`repr` del objeto, archivo y línea.
- **UI:** pestaña "Dumps" con auto-refresh.

---

## 6. Contexto por ciclo (`context.py`)

```python
@dataclass
class Batch:
    id: str
    kind: Literal["request", "job", "command", "standalone"]
    parent_id: str | None
    entries: list[Entry]
    recording: bool  # False si el ciclo quedó fuera por filtro o muestreo
```

- Usa `contextvars.ContextVar`, que funciona con hilos, gevent y vistas async.
- Fuera de un batch, cada entrada recibe su propio `batch_id` (`standalone`).

---

## 7. Configuración

| Config | Default | Descripción |
|--------|---------|-------------|
| `LEEUWENHOEK_ENABLED` | `app.debug` | Switch general |
| `LEEUWENHOEK_URL_PREFIX` | `/leeuwenhoek` | Ruta del panel |
| `LEEUWENHOEK_WATCHERS` | todos los disponibles | Watchers activos |
| `LEEUWENHOEK_IGNORE_PATHS` | `["/static*", "/favicon.ico", "/health*"]` | Globs a ignorar (el panel siempre se ignora) |
| `LEEUWENHOEK_IGNORE_COMMANDS` | `[]` | Comandos CLI a ignorar |
| `LEEUWENHOEK_SAMPLE_RATE` | `1.0` | Muestreo de batches (0–1) |
| `LEEUWENHOEK_ONLY_ERRORS` | `False` | Guardar solo batches con excepción o status ≥ 500 |
| `LEEUWENHOEK_HIDDEN_HEADERS` | `["authorization", "cookie", "x-api-key"]` | Se reemplazan por `********` |
| `LEEUWENHOEK_HIDDEN_PARAMS` | `["password", "password_confirmation", "token", "secret"]` | Igual, en body, query y args de jobs |
| `LEEUWENHOEK_MAX_BODY_KB` | `64` | Truncado de bodies |
| `LEEUWENHOEK_MAX_ENTRIES_PER_BATCH` | `500` | Tope por ciclo |
| `LEEUWENHOEK_SLOW_QUERY_MS` | `100` | Umbral de query lenta |
| `LEEUWENHOEK_N_PLUS_ONE_THRESHOLD` | `5` | Repeticiones para marcar N+1 |
| `LEEUWENHOEK_PRUNE_HOURS` | `24` | Retención |
| `LEEUWENHOEK_DATABASE_URI` | `sqlite:///instance/leeuwenhoek.db` | **BD separada** de la de la app |
| `LEEUWENHOEK_ASYNC_WRITE` | `True` | Persistir en un hilo de fondo |
| `LEEUWENHOEK_POLL_SECONDS` | `4` | Refresco del panel |
| `LEEUWENHOEK_BULMA_CDN` | `False` | `True` para cargar Bulma desde CDN en vez de la copia vendorizada |

Extensibilidad:
```python
lw.filter(lambda entry: ...)   # True para guardar
lw.tag(lambda entry: [...])    # tags custom
```

---

## 8. Acceso al panel

- Por defecto solo está accesible si `app.debug` es `True`.
- Se puede configurar una regla de acceso:
  ```python
  lw.authorize(lambda: current_user.is_authenticated and current_user.is_admin)
  ```
- Si la regla falla, responde 403.
- Opcional: `LEEUWENHOEK_BASIC_AUTH = ("user", "pass")`.
- Los formularios que modifican datos (limpiar, pausar) usan POST con token CSRF propio. Si la app ya usa Flask-WTF, se reutiliza `csrf_token()`.

---

## 9. Vistas (Blueprint, Jinja + Bulma)

El renderizado es en el servidor. El JSON solo se usa para el polling.

| Método | Ruta | Render | Descripción |
|--------|------|--------|-------------|
| GET | `/leeuwenhoek/` | HTML | Redirige a Requests |
| GET | `/leeuwenhoek/<type>` | HTML | Listado por tipo, con `?tag=&q=&before=` |
| GET | `/leeuwenhoek/<type>/<uuid>` | HTML | Detalle de la entrada y su batch relacionado |
| GET | `/leeuwenhoek/batch/<batch_id>` | HTML | Todo el batch agrupado por tipo |
| GET | `/leeuwenhoek/exceptions/family/<hash>` | HTML | Ocurrencias de una misma excepción |
| GET | `/leeuwenhoek/<type>/rows?after=<id>` | HTML parcial | Filas nuevas para el polling |
| POST | `/leeuwenhoek/recording/toggle` | redirect | Pausar o reanudar la grabación |
| POST | `/leeuwenhoek/clear` | redirect | Borrar todo |
| GET | `/leeuwenhoek/api/status` | JSON | Estado de grabación y conteos por tipo (badges del menú) |

- La paginación es por cursor (`before=<id>`): botones "Más antiguos" y "Más recientes".
- El polling lo hace `leeuwenhoek.js`: cada N segundos pide `/rows?after=<último id>` y antepone las filas (HTML ya renderizado por Jinja).

### Layout (`base.html`)
- **`navbar`** de Bulma con el nombre, un switch de grabación (`tag is-success`/`is-danger`), un botón de limpiar y la búsqueda.
- **`columns`**: a la izquierda un `menu` de Bulma con las secciones y sus contadores (`tag is-rounded`); a la derecha el contenido.
- Modo oscuro con `data-theme="dark"`, que Bulma 1.x soporta de forma nativa.

### Secciones del menú
Requests · Commands · Schedule · Jobs · Batches · Cache · Dumps · Events · Exceptions · HTTP Client · Logs · Mail · Models · Queries · Redis · Templates

Solo aparecen las secciones de los watchers activos.

### Listados (`index.html`)
- `table is-fullwidth is-hoverable is-narrow` con columnas por tipo, definidas en el parcial de cada tipo:
  - Requests: verbo, path, status, duración, hace cuánto.
  - Queries: SQL truncado, duración, origen.
  - Exceptions: clase, mensaje, ocurrencias, última vez.
- Badges de status y nivel con `tag`: `is-success` (2xx), `is-info` (3xx), `is-warning` (4xx), `is-danger` (5xx o error).
- Filtro por tag con `field has-addons`.

### Detalle (`entries/<type>.html` + `batch.html`)
- Una `box` de resumen con `level` (método, URL, status, duración, IP, usuario).
- Bulma `tabs` para Payload · Headers · Session · Response, que se alternan con JS mínimo.
- Una `card` por cada sección relacionada del batch (queries, logs, excepciones, jobs, cache, HTTP saliente, mails), cada una con su conteo y tiempo total.
- Queries con el SQL formateado y resaltado. Las lentas llevan `tag is-warning` y las N+1 `tag is-danger`.
- Excepciones con el traceback colapsable (`<details>`) y un snippet de código con la línea marcada.
- JSON viewer recursivo hecho con una macro Jinja (`<details>` anidados) que no requiere librerías.

### Macros (`_macros.html`)
`status_tag(code)`, `duration(ms)`, `time_ago(dt)`, `json_tree(value)`, `sql_block(sql)`, `traceback(frames)`, `entry_table(entries, columns)`, `pagination(cursor)`.

### Filtros Jinja registrados
`lw_duration`, `lw_timeago`, `lw_truncate`, `lw_json`, `lw_sql`.

Se registran con prefijo para no colisionar con los filtros de la app.

---

## 10. CLI

Se registra como `flask leeuwenhoek` con el entry point de `flask.commands`.

```bash
uv run flask leeuwenhoek install            # crea tablas en LEEUWENHOEK_DATABASE_URI
uv run flask leeuwenhoek prune --hours=48   # borra entradas viejas
uv run flask leeuwenhoek clear              # borra todo
uv run flask leeuwenhoek pause | resume     # toggle de grabación
```

---

## 11. Rendimiento

- Buffer en memoria y **un solo flush por batch**.
- Escritura asíncrona con `queue.Queue` y un hilo daemon, con flush en `atexit`.
- Truncado de payloads y tope de entradas por batch.
- Captura del stack de queries limitada a N frames.
- SQLite en modo WAL para que las escrituras concurrentes no bloqueen.
- Overhead objetivo: menos de 5 ms por request en dev.

---

## 12. Compatibilidad

- Python ≥ 3.10, Flask ≥ 2.3 / 3.x, SQLAlchemy 2.x.
- Bulma 1.x (vendorizado en `static/`).
- Servidores: Werkzeug dev, gunicorn (sync/gthread/gevent) y uWSGI.
- Vistas async de Flask (gracias a contextvars).
- Las integraciones se detectan en runtime con `importlib.util.find_spec` y se instalan como extras de uv: SQLAlchemy, Flask-SQLAlchemy, Flask-Login, Flask-Caching, Flask-Mail, Celery, RQ, redis-py, requests, httpx y APScheduler.

---

## 13. API pública

```python
from flask import Flask
from flask_login import current_user
from leeuwenhoek import Leeuwenhoek, dump

app = Flask(__name__)
app.config["LEEUWENHOEK_ENABLED"] = True

lw = Leeuwenhoek()
lw.init_app(app, engine=db.engine, celery=celery_app)

lw.authorize(lambda: current_user.is_authenticated and current_user.is_admin)
lw.filter(lambda e: not (e.type == "request" and e.content["status"] == 304))
lw.tag(lambda e: [f"tenant:{g.tenant_id}"] if hasattr(g, "tenant_id") else [])

@app.get("/debug")
def debug():
    dump({"hola": "mundo"})
    return "ok"
```

---

## 14. Testing

- `uv run pytest` usa el driver de storage en memoria.
- Hay tests por watcher con `app.test_client()`.
- **Test de integración:** un request que dispara una query, un log, una excepción y un job. Se verifica que todo quede en un solo `batch_id` y que el job tenga `parent_batch_id`.
- Redacción de headers y params sensibles.
- Que ni las rutas ignoradas ni `/leeuwenhoek*` se graben.
- Que el panel renderice cada template sin errores (smoke test de cada vista).
- Que no haya colisión con templates ni filtros Jinja de una app de prueba.
- Que el wheel generado con `uv build` incluya `templates/` y `static/`.
- Benchmark de overhead con y sin la extensión.

---

## 15. Roadmap

**v0.1 (MVP)**
- Watchers de requests, excepciones, queries, logs y dumps
- Storage SQLAlchemy (SQLite)
- Panel Jinja + Bulma: layout, listados y detalle con batch
- Redacción, rutas ignoradas, control de acceso, `prune`
- Proyecto con uv, app de ejemplo y CI (`uv sync` + `uv run pytest`)

**v0.2**
- Jobs (Celery), HTTP saliente, cache, mail, comandos y templates
- Tags y filtros custom
- Detección de N+1 y queries lentas
- Polling de filas nuevas y modo oscuro

**v0.3**
- Modelos, Redis, tareas programadas y eventos
- Reintento de jobs fallidos y marcar excepciones como resueltas
- Muestreo y modo solo errores para staging
- Publicación en PyPI con `uv publish`

---

## 16. Referencias

- Flask signals: https://flask.palletsprojects.com/en/stable/signals/
- Flask Blueprints: https://flask.palletsprojects.com/en/stable/blueprints/
- SQLAlchemy core events: https://docs.sqlalchemy.org/en/20/core/events.html
- Celery signals: https://docs.celeryq.dev/en/stable/userguide/signals.html
- Bulma: https://bulma.io/documentation/
- uv: https://docs.astral.sh/uv/
