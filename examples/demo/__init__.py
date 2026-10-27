"""Demo app: `uv run flask --app examples/demo run --debug`, then open /leeuwenhoek."""

import logging

from flask import Flask, jsonify, request
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from leeuwenhoek import Leeuwenhoek, dump


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "demo"
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        conn.execute(text("create table users (id integer primary key, name text)"))
        conn.execute(text("insert into users (name) values ('ada'), ('grace')"))

    Leeuwenhoek(app, engine=engine)

    @app.get("/")
    def index():
        return (
            '<ul><li><a href="/users">/users</a></li><li><a href="/boom">/boom</a></li>'
            '<li><a href="/dump">/dump</a></li></ul>'
        )

    @app.get("/users")
    def users():
        app.logger.info("listing users")
        with engine.connect() as conn:
            rows = conn.execute(text("select * from users where id > :min"), {"min": 0}).all()
        return jsonify([r._asdict() for r in rows])

    @app.post("/login")
    def login():
        app.logger.warning("login attempt for %s", request.json.get("user"))
        return jsonify(ok=True)

    @app.get("/boom")
    def boom():
        raise RuntimeError("demo failure")

    @app.get("/dump")
    def dump_view():
        dump({"hello": "world", "items": [1, 2, 3]})
        return "dumped"

    logging.getLogger().setLevel(logging.DEBUG)
    return app
