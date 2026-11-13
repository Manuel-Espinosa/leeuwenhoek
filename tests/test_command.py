"""``flask`` CLI commands: one batch per command."""

import logging
import sys

import click
import pytest
from conftest import entries
from flask import Blueprint
from sqlalchemy import text

from leeuwenhoek import Leeuwenhoek


@pytest.fixture
def runner(app, lw, engine):
    @app.cli.command("seed")
    @click.argument("count", type=int)
    @click.option("--table", default="t")
    @click.option("--token")
    def seed(count, table, token):
        logging.getLogger("app").warning("seeding %s rows", count)
        with engine.connect() as conn:
            conn.execute(text(f"select * from {table}"))
        click.echo("done")

    return app.test_cli_runner()


def commands(lw):
    return entries(lw, "command")


def test_command_batch_groups_query_log_and_command(runner, lw):
    result = runner.invoke(args=["seed", "3", "--token", "abc"])
    assert result.exit_code == 0, result.output
    [command] = commands(lw)
    batch = lw.storage.batch(command.batch_id)
    assert sorted(e.type for e in batch) == ["command", "log", "query"]
    assert command.is_root
    c = command.content
    assert c["name"] == "seed"
    assert c["arguments"] == {"count": 3}
    assert c["options"] == {"table": "t", "token": "********"}
    assert c["exit_code"] == 0
    assert c["duration_ms"] >= 0
    assert {"command:seed", "exit_code:0"} <= set(command.tags)


def test_nested_group_and_late_registration(app, lw):
    db = click.Group("db")
    app.cli.add_command(db)  # registered after init_app

    @db.group("migrations")
    def migrations():
        pass

    @migrations.command("apply")
    @click.argument("name")
    def apply(name):
        pass

    result = app.test_cli_runner().invoke(args=["db", "migrations", "apply", "0001"])
    assert result.exit_code == 0, result.output
    [command] = commands(lw)
    assert command.content["name"] == "db migrations apply"
    assert command.content["arguments"] == {"name": "0001"}


def test_blueprint_commands(app, lw):
    bp = Blueprint("users", __name__)

    @bp.cli.command("purge")
    def purge():
        pass

    app.register_blueprint(bp)
    app.test_cli_runner().invoke(args=["users", "purge"])
    assert [c.content["name"] for c in commands(lw)] == ["users purge"]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (lambda: None, 0),
        (lambda: sys.exit(3), 3),
        (lambda: sys.exit("bad"), 1),
        (lambda: click.get_current_context().exit(4), 4),
        (lambda: (_ for _ in ()).throw(click.UsageError("nope")), 2),
        (lambda: (_ for _ in ()).throw(click.Abort()), 1),
    ],
)
def test_exit_codes(app, lw, body, code):
    app.cli.command("job")(lambda: body())
    result = app.test_cli_runner().invoke(args=["job"])
    assert result.exit_code == code
    [command] = commands(lw)
    assert command.content["exit_code"] == code
    assert entries(lw, "exception") == []  # exits and Click errors are not bugs


def test_failing_command_is_persisted_with_its_exception(app, lw, engine):
    @app.cli.command("explode")
    def explode():
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        raise RuntimeError("command boom")

    result = app.test_cli_runner().invoke(args=["explode"])
    assert result.exit_code == 1 and isinstance(result.exception, RuntimeError)
    [command] = commands(lw)
    [exc] = entries(lw, "exception")
    assert command.content["exit_code"] == 1
    assert exc.batch_id == command.batch_id
    assert {e.type for e in lw.storage.batch(command.batch_id)} == {
        "command",
        "exception",
        "query",
    }


def test_exception_is_not_recorded_again_by_excepthook(app, monkeypatch):
    """Click lets the exception reach ``sys.excepthook``: it is already in the command batch."""
    previous = []
    monkeypatch.setattr(sys, "excepthook", lambda t, e, tb: previous.append(e))
    ext = Leeuwenhoek(app)

    @app.cli.command("explode")
    def explode():
        raise RuntimeError("command boom")

    exc = app.test_cli_runner().invoke(args=["explode"]).exception
    sys.excepthook(type(exc), exc, exc.__traceback__)
    [entry] = entries(ext, "exception")
    [command] = commands(ext)
    assert entry.batch_id == command.batch_id
    assert previous == [exc]  # the previous hook still runs
    ext.close()


def test_ignored_commands(app, engine, monkeypatch):
    monkeypatch.setenv("LEEUWENHOEK_IGNORE_COMMANDS", "db, seed-*")
    ext = Leeuwenhoek(app)
    db = click.Group("db")
    app.cli.add_command(db)
    for name in ("upgrade", "downgrade"):
        db.command(name)(lambda: logging.getLogger("app").warning("inside"))
    for name in ("seed-users", "report", "run", "shell"):
        app.cli.command(name)(lambda: logging.getLogger("app").warning("inside"))

    runner = app.test_cli_runner()
    for args in (["db", "upgrade"], ["seed-users"], ["report"], ["run"], ["shell"]):
        assert runner.invoke(args=args).exit_code == 0
    assert [c.content["name"] for c in commands(ext)] == ["report"]
    # inside an ignored command nothing is recorded, not even as standalone
    assert len(entries(ext, "log")) == 1
    ext.close()


def test_leeuwenhoek_commands_are_ignored(app, lw):
    from leeuwenhoek.cli import cli

    app.cli.add_command(cli)
    assert app.test_cli_runner().invoke(args=["leeuwenhoek", "pause"]).exit_code == 0
    assert app.test_cli_runner().invoke(args=["leeuwenhoek", "resume"]).exit_code == 0
    assert commands(lw) == []


def test_commands_outside_app_cli_are_not_recorded(lw):
    @click.command()
    def other():
        pass

    assert other.main([], standalone_mode=False) is None
    assert commands(lw) == []


def test_disabled(app, engine):
    app.config["LEEUWENHOEK_ENABLED"] = False
    ext = Leeuwenhoek(app)
    app.cli.command("job")(lambda: None)
    assert app.test_cli_runner().invoke(args=["job"]).exit_code == 0
    assert commands(ext) == []
    ext.close()


def test_only_errors_keeps_failing_commands(app, engine):
    app.config["LEEUWENHOEK_ONLY_ERRORS"] = True
    ext = Leeuwenhoek(app)

    @app.cli.command("ok")
    def ok():
        pass

    @app.cli.command("fail")
    def fail():
        raise RuntimeError("boom")

    runner = app.test_cli_runner()
    runner.invoke(args=["ok"])
    runner.invoke(args=["fail"])
    assert [c.content["name"] for c in commands(ext)] == ["fail"]
    ext.close()


def test_invoke_restored_after_close(app):
    original = click.Command.invoke
    ext = Leeuwenhoek(app)
    assert click.Command.invoke is not original
    ext.close()
    assert click.Command.invoke is original


def test_panel(app, runner, lw):
    runner.invoke(args=["seed", "1"])
    [command] = commands(lw)
    app.debug = True  # loading the app for the command set it from FLASK_DEBUG
    client = app.test_client()
    listing = client.get("/leeuwenhoek/commands").get_data(as_text=True)
    assert "seed" in listing
    detail = client.get(f"/leeuwenhoek/commands/{command.uuid}").get_data(as_text=True)
    assert "Exit code" in detail and "Logs" in detail and "Queries" in detail
    batch = client.get(f"/leeuwenhoek/batch/{command.batch_id}").get_data(as_text=True)
    assert "Exit code" in batch and "seeding 1 rows" in batch
    batches = client.get("/leeuwenhoek/batches").get_data(as_text=True)
    assert "<code>seed</code>" in batches
    menu = client.get("/leeuwenhoek/requests").get_data(as_text=True)
    assert 'href="/leeuwenhoek/commands"' in menu
