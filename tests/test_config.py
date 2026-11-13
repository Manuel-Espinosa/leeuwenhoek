from flask import Flask

from leeuwenhoek.config import Config


def cfg(monkeypatch, env=None, **config):
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    app = Flask(__name__)
    app.config.update(config)
    return Config(app)


def test_env_is_coerced_by_default_type(monkeypatch):
    c = cfg(
        monkeypatch,
        {
            "LEEUWENHOEK_ENABLED": "1",
            "LEEUWENHOEK_SAMPLE_RATE": "0.25",
            "LEEUWENHOEK_PAGE_SIZE": "20",
            "LEEUWENHOEK_ONLY_ERRORS": "true",
            "LEEUWENHOEK_WATCHERS": "request, query",
            "LEEUWENHOEK_BASIC_AUTH": "me:s:ecret",
            "LEEUWENHOEK_REQUEST_TAGS": '{"imported_id": ["body.order.imported_id"]}',
        },
    )
    assert c.enabled is True and c.sample_rate == 0.25 and c.page_size == 20
    assert c.only_errors is True and c.watchers == ["request", "query"]
    assert c.basic_auth == ("me", "s:ecret")
    assert c.request_tags == {"imported_id": ["body.order.imported_id"]}


def test_app_config_beats_env_and_empty_env_is_unset(monkeypatch):
    c = cfg(
        monkeypatch,
        {"LEEUWENHOEK_PAGE_SIZE": "20", "LEEUWENHOEK_SAMPLE_RATE": ""},
        LEEUWENHOEK_PAGE_SIZE=5,
    )
    assert c.page_size == 5 and c.sample_rate == 1.0 and c.enabled is None


def test_track_memory_off_by_default_and_read_from_env(monkeypatch):
    assert cfg(monkeypatch).track_memory is False
    assert cfg(monkeypatch, {"LEEUWENHOEK_TRACK_MEMORY": "true"}).track_memory is True


def test_database_url_from_parts(monkeypatch):
    c = cfg(
        monkeypatch,
        {
            "LEEUWENHOEK_DB_HOST": "localhost",
            "LEEUWENHOEK_DB_PORT": "3355",
            "LEEUWENHOEK_DB_USERNAME": "user",
            "LEEUWENHOEK_DB_PASSWORD": "p@ss/word",
            "LEEUWENHOEK_DB_DATABASE": "leeuwenhoek",
        },
    )
    assert c.database_uri == "postgresql+psycopg://user:p%40ss%2Fword@localhost:3355/leeuwenhoek"


def test_database_url_cloudsql_socket(monkeypatch):
    c = cfg(
        monkeypatch,
        {
            "LEEUWENHOEK_DB_SOCKET": "proj:us-central1:inst",
            "LEEUWENHOEK_DB_USERNAME": "u",
            "LEEUWENHOEK_DB_PASSWORD": "p",
            "LEEUWENHOEK_DB_DATABASE": "lw",
        },
    )
    assert (
        c.database_uri
        == "postgresql+psycopg://u:p@/lw?host=%2Fcloudsql%2Fproj%3Aus-central1%3Ainst"
    )


def test_explicit_uri_wins_and_sqlite_default(monkeypatch):
    assert cfg(monkeypatch, LEEUWENHOEK_DATABASE_URI="sqlite://").database_uri == "sqlite://"
    assert cfg(monkeypatch).database_uri.startswith("sqlite:///")
