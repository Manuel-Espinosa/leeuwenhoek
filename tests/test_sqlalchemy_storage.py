import pytest
from flask import Flask
from sqlalchemy import create_engine, text

from leeuwenhoek import Leeuwenhoek


@pytest.fixture
def app(tmp_path):
    app = Flask(__name__)
    app.config.update(
        LEEUWENHOEK_ENABLED=True,
        LEEUWENHOEK_DATABASE_URI=f"sqlite:///{tmp_path}/sub/lw.db",
    )
    return app


def test_async_write_roundtrip_and_flush(app):
    engine = create_engine("sqlite://")
    lw = Leeuwenhoek(app, engine=engine)

    @app.get("/q")
    def q():
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return "ok"

    app.test_client().get("/q")
    lw.flush()
    req = lw.storage.list("request")[0]
    assert req.tags and req.content["status"] == 200
    assert lw.storage.counts() == {"request": 1, "query": 1, "batches": 1}
    assert lw.storage.get(req.uuid).batch_id == req.batch_id
    assert len(lw.storage.batch(req.batch_id)) == 2
    assert lw.storage.list("request", tag="status:200")
    assert lw.storage.list("query", q="SELECT 1")
    assert lw.prune(0) == 2
    lw.close()


def test_global_query_listener_without_engine(app):
    lw = Leeuwenhoek(app)
    engine = create_engine("sqlite://")

    @app.get("/q")
    def q():
        with engine.connect() as conn:
            conn.execute(text("select 42"))
        return "ok"

    app.test_client().get("/q")
    lw.flush()
    sqls = [e.content["sql"] for e in lw.storage.list("query")]
    assert sqls == ["select 42"]  # no leeuwenhoek_* queries or own PRAGMAs
    lw.close()


def test_cli(app):
    lw = Leeuwenhoek(app)
    runner = app.test_cli_runner()
    from leeuwenhoek.cli import cli

    assert "Tables created" in runner.invoke(cli, ["install"]).output
    runner.invoke(cli, ["pause"])
    assert lw.storage.get_setting("recording") == "0"
    runner.invoke(cli, ["resume"])
    assert lw.storage.get_setting("recording") == "1"
    assert "entries deleted" in runner.invoke(cli, ["prune", "--hours", "48"]).output
    assert "entries deleted" in runner.invoke(cli, ["clear"]).output
    lw.close()
