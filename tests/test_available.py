import logging

from leeuwenhoek import Leeuwenhoek
from leeuwenhoek.watchers import AVAILABLE, Watcher, default_watchers


class MissingLibWatcher(Watcher):
    name = "missing"
    entry_type = "missing"
    label = "Missing"
    requires = "leeuwenhoek_no_such_module"

    def register(self, app):
        raise AssertionError("must not be registered")


def test_requirements():
    assert all(cls.available() for cls in AVAILABLE.values())
    assert not MissingLibWatcher.available()
    assert MissingLibWatcher.missing() == ["leeuwenhoek_no_such_module"]

    class Partial(MissingLibWatcher):
        requires = ("flask", "no_such_parent.child")

    assert Partial.missing() == ["no_such_parent.child"]

    class Empty(MissingLibWatcher):
        requires = ""

    assert Empty.available()


def test_default_skips_watchers_with_missing_library(app, monkeypatch):
    monkeypatch.setitem(AVAILABLE, "missing", MissingLibWatcher)
    assert default_watchers() == [name for name in AVAILABLE if name != "missing"]
    ext = Leeuwenhoek(app)
    assert list(ext.watchers) == default_watchers()
    ext.close()


def test_configured_watcher_with_missing_library_warns(app, monkeypatch, caplog):
    monkeypatch.setitem(AVAILABLE, "missing", MissingLibWatcher)
    app.config["LEEUWENHOEK_WATCHERS"] = ["request", "missing"]
    with caplog.at_level(logging.WARNING, logger="leeuwenhoek"):
        ext = Leeuwenhoek(app)
    assert list(ext.watchers) == ["request"]
    assert "missing" in caplog.text and "leeuwenhoek_no_such_module" in caplog.text
    ext.close()


def test_watchers_from_env(app, monkeypatch):
    monkeypatch.setenv("LEEUWENHOEK_WATCHERS", "request, log")
    ext = Leeuwenhoek(app)
    assert list(ext.watchers) == ["request", "log"]
    ext.close()
