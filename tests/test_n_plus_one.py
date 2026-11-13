import pytest
from conftest import entries
from sqlalchemy import text

from leeuwenhoek import Leeuwenhoek
from leeuwenhoek.watchers.query import normalize_sql

N_PLUS_ONE_TAG = '<span class="tag is-danger">n+1</span>'


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("select * from users where id = 1", "select * from users where id = 2"),
        ("SELECT * FROM users WHERE id = ?", "select * from users where id = 42"),
        ("select * from users where name = 'a'", "select * from users where name = 'o''hara'"),
        ("select * from users where id = :id", "select * from users where id = %s"),
        ("select * from users where id = %(id)s", "select * from users where id = $1"),
        ("select * from t where id in (1, 2, 3)", "select * from t where id in (?)"),
        ("select * from t where id in (?, ?)", "select * from t where id IN (%s,%s,%s)"),
        ("insert into t (a) values (1), (2)", "insert into t (a) values (?)"),
        ("select *\n  from t\twhere x = 1.5", "select * from t where x = 2e3"),
    ],
)
def test_normalize_sql_matches_same_shape(a, b):
    assert normalize_sql(a) == normalize_sql(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("select * from users where id = 1", "select * from posts where id = 1"),
        ("select * from users where id = 1", "select * from users where id is null"),
        ("select a from t1 where x = 1", "select a from t2 where x = 1"),
    ],
)
def test_normalize_sql_keeps_different_shapes_apart(a, b):
    assert normalize_sql(a) != normalize_sql(b)


def test_normalize_sql_output():
    assert normalize_sql("select name from users_1 where id = 7 and tag = 'x'") == (
        "SELECT name FROM users_1 WHERE id = ? AND tag = ?"
    )
    assert normalize_sql("select x::int from t where y = :y") == "SELECT x::int FROM t WHERE y = ?"


@pytest.fixture
def loop(app, engine):
    """Build an extension with ``/loop/<n>``: one count query, then the same query ``n`` times."""
    made: list[Leeuwenhoek] = []

    @app.get("/loop/<int:n>")
    def loop_view(n):
        with engine.connect() as conn:
            conn.execute(text("select count(*) from t"))
            for i in range(n):
                conn.execute(text("select * from t where id = :id"), {"id": i})
        return "ok"

    def make(**config):
        app.config.update(config)
        ext = Leeuwenhoek(app, engine=engine)
        made.append(ext)
        return ext

    yield make
    for ext in made:
        ext.close()


def repeated(ext):
    return [q for q in entries(ext, "query") if "where id" in q.content["sql"]]


def test_loop_of_threshold_queries_is_flagged(app, loop):
    ext = loop()
    client = app.test_client()
    client.get("/loop/5")
    queries = repeated(ext)
    assert len(queries) == 5
    assert all(q.content["n_plus_one"] is True and "n+1" in q.tags for q in queries)
    other = next(q for q in entries(ext, "query") if "count" in q.content["sql"])
    assert "n_plus_one" not in other.content and "n+1" not in other.tags

    assert N_PLUS_ONE_TAG in client.get("/leeuwenhoek/queries").text
    assert N_PLUS_ONE_TAG in client.get(f"/leeuwenhoek/queries/{queries[0].uuid}").text
    req = entries(ext, "request")[0]
    page = client.get(f"/leeuwenhoek/requests/{req.uuid}").text
    assert "Possible N+1 queries" in page
    summary = page.split("Possible N+1 queries")[1].split("</ul>")[0]
    assert "<code>SELECT * FROM t WHERE id = ?</code> × 5" in summary
    assert "count(*)" not in summary
    batch_page = client.get(f"/leeuwenhoek/batch/{req.batch_id}").text
    assert "<code>SELECT * FROM t WHERE id = ?</code> × 5" in batch_page


def test_loop_below_threshold_is_not_flagged(app, loop):
    ext = loop()
    client = app.test_client()
    client.get("/loop/4")
    queries = repeated(ext)
    assert len(queries) == 4
    assert all("n_plus_one" not in q.content and "n+1" not in q.tags for q in queries)
    assert N_PLUS_ONE_TAG not in client.get("/leeuwenhoek/queries").text
    req = entries(ext, "request")[0]
    assert "Possible N+1" not in client.get(f"/leeuwenhoek/requests/{req.uuid}").text


def test_threshold_is_configurable(app, loop):
    ext = loop(LEEUWENHOEK_N_PLUS_ONE_THRESHOLD=3)
    app.test_client().get("/loop/3")
    assert all(q.content.get("n_plus_one") for q in repeated(ext))


def test_zero_threshold_disables_detection(app, loop):
    ext = loop(LEEUWENHOEK_N_PLUS_ONE_THRESHOLD=0)
    app.test_client().get("/loop/50")
    assert not any(q.content.get("n_plus_one") for q in repeated(ext))


def test_filters_and_taggers_see_the_flag(app, loop):
    ext = loop()
    seen: list[tuple[bool, bool]] = []

    @ext.filter
    def keep(entry):
        if entry.type == "query":
            seen.append((bool(entry.content.get("n_plus_one")), "n+1" in entry.tags))
        return True

    @ext.tag
    def tagger(entry):
        return ["looped"] if "n+1" in entry.tags else None

    app.test_client().get("/loop/5")
    assert seen.count((True, True)) == 5
    assert all("looped" in q.tags for q in repeated(ext))
