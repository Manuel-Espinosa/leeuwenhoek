import logging

import pytest
from flask import Flask
from sqlalchemy import create_engine, text

from leeuwenhoek import Leeuwenhoek


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        PROPAGATE_EXCEPTIONS=False,
        LEEUWENHOEK_ENABLED=True,
        LEEUWENHOEK_STORAGE="memory",
        LEEUWENHOEK_ASYNC_WRITE=False,
    )
    app.debug = True
    app.logger.setLevel(logging.DEBUG)
    return app


@pytest.fixture
def engine():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("create table t (id integer primary key, name text)"))
    return engine


@pytest.fixture
def lw(app, engine):
    ext = Leeuwenhoek(app, engine=engine)

    @app.get("/hello")
    def hello():
        app.logger.warning("hola %s", "mundo")
        with engine.connect() as conn:
            conn.execute(text("select * from t where id = :id"), {"id": 1})
        return {"ok": True}

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    @app.post("/login")
    def login():
        return "ok"

    yield ext
    ext.close()


@pytest.fixture
def client(app, lw):
    return app.test_client()


def entries(lw, type=None):
    return lw.storage.list(type, limit=1000)
