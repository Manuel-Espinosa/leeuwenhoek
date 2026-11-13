"""Exceptions that nothing catches: ``sys.excepthook`` and ``threading.excepthook``."""

import subprocess
import sys
import textwrap
import threading

import pytest
from conftest import entries
from flask import Flask
from flask.cli import ScriptInfo
from sqlalchemy import create_engine, text

from leeuwenhoek import Leeuwenhoek, context
from leeuwenhoek.context import Batch


@pytest.fixture
def previous(monkeypatch):
    """Fake hooks installed before Leeuwenhoek, recording what reaches them."""
    calls = {"sys": [], "thread": []}
    monkeypatch.setattr(sys, "excepthook", lambda t, e, tb: calls["sys"].append(e))
    monkeypatch.setattr(threading, "excepthook", lambda args: calls["thread"].append(args))
    return calls


@pytest.fixture
def ext(app, previous):
    ext = Leeuwenhoek(app)
    yield ext
    ext.close()


def raise_uncaught(exc):
    try:
        raise exc
    except BaseException as e:  # noqa: BLE001
        sys.excepthook(type(e), e, e.__traceback__)


def run_thread(target):
    thread = threading.Thread(target=target, name="worker")
    thread.start()
    thread.join()


def boom():
    raise ValueError("thread boom")


def test_thread_exception_is_standalone(ext, previous):
    run_thread(boom)
    [exc] = entries(ext, "exception")
    assert exc.content["class"] == "builtins.ValueError"
    assert exc.content["message"] == "thread boom"
    assert exc.content["handled"] is False
    assert exc.content["frames"][-1]["function"] == "boom"
    assert exc.is_root and exc.family_hash
    assert "exception:ValueError" in exc.tags
    # the previous hook still runs (it prints the traceback by default)
    [args] = previous["thread"]
    assert args.exc_value.args == ("thread boom",) and args.thread.name == "worker"


def test_thread_exception_joins_the_current_batch(ext):
    batch = Batch(kind="job")

    def job():
        context.start_batch(batch)
        boom()

    run_thread(job)
    [exc] = batch.entries
    assert exc.type == "exception" and exc.batch_id == batch.id
    assert entries(ext, "exception") == []  # persisted when the batch closes, not before


def test_same_family_as_a_request_exception(app, ext):
    def fail():
        raise ValueError("thread boom")

    app.add_url_rule("/fail", "fail", lambda: fail())
    app.test_client().get("/fail")
    run_thread(lambda: fail())
    request_exc, thread_exc = sorted(entries(ext, "exception"), key=lambda e: e.id)
    assert request_exc.batch_id != thread_exc.batch_id
    assert request_exc.family_hash == thread_exc.family_hash
    assert set(request_exc.content) == set(thread_exc.content)


def test_sys_excepthook(ext, previous):
    raise_uncaught(RuntimeError("script boom"))
    [exc] = entries(ext, "exception")
    assert exc.content["class"] == "builtins.RuntimeError"
    assert [e.args for e in previous["sys"]] == [("script boom",)]


def test_cli_command_exception(app, ext, previous):
    """Click only handles its own exceptions: anything else reaches ``sys.excepthook``."""

    @app.cli.command("explode")
    def explode():
        raise RuntimeError("command boom")

    with pytest.raises(RuntimeError) as info:
        app.cli.main(["explode"], obj=ScriptInfo(create_app=lambda: app), standalone_mode=True)
    sys.excepthook(info.type, info.value, info.tb)
    [exc] = entries(ext, "exception")
    assert exc.content["message"] == "command boom"
    assert exc.content["frames"][-1]["function"] == "explode"
    assert len(previous["sys"]) == 1


def test_cli_command_exception_end_to_end(tmp_path):
    """A real ``flask`` process: the entry is written by the hook and flushed at exit."""
    (tmp_path / "cliapp.py").write_text(
        textwrap.dedent(
            """
            from flask import Flask
            from leeuwenhoek import Leeuwenhoek

            app = Flask(__name__)
            app.config["LEEUWENHOEK_ENABLED"] = True
            app.config["LEEUWENHOEK_DATABASE_URI"] = "sqlite:///lw.db"
            Leeuwenhoek(app)

            @app.cli.command("explode")
            def explode():
                raise RuntimeError("command boom")
            """
        )
    )
    result = subprocess.run(
        [sys.executable, "-m", "flask", "--app", "cliapp", "explode"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "RuntimeError: command boom" in result.stderr  # the default hook still printed it
    engine = create_engine(f"sqlite:///{tmp_path / 'lw.db'}")
    with engine.connect() as conn:
        rows = conn.execute(
            text("select content, batch_id from leeuwenhoek_entries where type = 'exception'")
        ).all()
        commands = conn.execute(
            text("select content, batch_id from leeuwenhoek_entries where type = 'command'")
        ).all()
    engine.dispose()
    assert len(rows) == 1 and "command boom" in rows[0][0]
    # the command watcher recorded it in the command's batch, and the hook didn't record it again
    assert len(commands) == 1 and '"exit_code": 1' in commands[0][0]
    assert rows[0][1] == commands[0][1]


def test_request_exception_is_not_recorded_twice(app, ext, previous):
    app.config["PROPAGATE_EXCEPTIONS"] = True

    def fail():
        raise RuntimeError("request boom")

    app.add_url_rule("/fail", "fail", fail)
    with pytest.raises(RuntimeError) as info:
        app.test_client().get("/fail")
    sys.excepthook(info.type, info.value, info.tb)
    assert len(entries(ext, "exception")) == 1
    assert len(previous["sys"]) == 1  # the previous hook is called anyway


def test_keyboard_interrupt_is_ignored(ext, previous):
    raise_uncaught(KeyboardInterrupt())
    assert entries(ext, "exception") == []
    assert len(previous["sys"]) == 1


def test_hooks_restored_after_close(app, previous):
    sys_hook, thread_hook = sys.excepthook, threading.excepthook
    ext = Leeuwenhoek(app)
    assert sys.excepthook != sys_hook and threading.excepthook != thread_hook
    ext.close()
    assert sys.excepthook is sys_hook and threading.excepthook is thread_hook


def test_several_instances(app, previous):
    other = Flask("other")
    other.config.update(app.config)
    original = sys.excepthook
    first, second = Leeuwenhoek(app), Leeuwenhoek(other)

    raise_uncaught(RuntimeError("both"))
    assert len(entries(first, "exception")) == len(entries(second, "exception")) == 1

    # Closed out of order: the first hook stays in the chain but only delegates.
    first.close()
    raise_uncaught(RuntimeError("second only"))
    assert len(entries(first, "exception")) == 1
    assert len(entries(second, "exception")) == 2

    second.close()
    assert sys.excepthook is not original  # the first one's pass-through remains
    raise_uncaught(RuntimeError("none"))
    assert len(previous["sys"]) == 3


def test_repeated_init_restores_in_order(app, previous):
    original = sys.excepthook, threading.excepthook
    for n in range(3):
        fresh = Flask(f"app{n}")
        fresh.config.update(app.config)
        Leeuwenhoek(fresh).close()
    assert (sys.excepthook, threading.excepthook) == original
