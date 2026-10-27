import pytest
from conftest import entries
from flask import jsonify

from leeuwenhoek import Leeuwenhoek, add_tags
from leeuwenhoek.tags import from_paths, normalize, resolve


def test_resolve_paths():
    data = {
        "order": {"imported_id": "A1"},
        "items": [{"id": 1}, {"id": 2}],
        "raw": '{"a": {"b": 3}}',
    }
    assert resolve(data, "order.imported_id") == ["A1"]
    assert resolve(data, "items.id") == [1, 2]
    assert resolve(data, "items.1.id") == [2]
    assert resolve(data, "raw.a.b") == [3]  # JSON strings are parsed on demand
    assert resolve(data, "missing.path") == []
    assert resolve(data, "order") == []  # mappings are not scalars
    assert from_paths(data, {"id": ["items.id", "order.imported_id"]}) == ["id:1", "id:2", "id:A1"]


def test_normalize():
    assert normalize([" a ", "a", "", None, 5]) == ["a", "None", "5"]


@pytest.fixture
def tagged(app, engine):
    app.config["LEEUWENHOEK_REQUEST_TAGS"] = {
        "imported_id": ["body.order.imported_id", "view_args.imported_id", "query.imported_id"],
        "response_id": "response_body.data.id",
    }
    lw = Leeuwenhoek(app, engine=engine)

    @app.post("/deliveries")
    def create():
        return jsonify(data={"id": 77})

    @app.get("/deliveries/<imported_id>")
    def show(imported_id):
        app.logger.warning("looking up")
        return "ok"

    @app.get("/manual")
    def manual():
        add_tags("tenant:acme", "tenant:acme")
        return "ok"

    yield lw
    lw.close()


def test_config_tags_from_body_response_and_view_args(app, tagged):
    c = app.test_client()
    c.post("/deliveries", json={"order": {"imported_id": "IMP-1"}})
    c.get("/deliveries/IMP-2?imported_id=IMP-3")
    tags = {t for e in entries(tagged, "request") for t in e.tags}
    assert {"imported_id:IMP-1", "response_id:77", "imported_id:IMP-2", "imported_id:IMP-3"} <= tags


def test_batch_tags_reach_every_entry(app, tagged):
    app.test_client().get("/deliveries/IMP-9")
    rows = entries(tagged)
    assert {e.type for e in rows} == {"request", "log"}
    assert all("imported_id:IMP-9" in e.tags for e in rows)
    assert len(tagged.storage.list(tag="imported_id:IMP-9")) == 2


def test_add_tags_and_tagger_callback(app, tagged):
    @tagged.tag
    def by_type(entry):
        if entry.type == "request":
            return [f"verb:{entry.content['method']}"]

    @tagged.tag
    def broken(entry):
        raise ValueError("boom")

    app.test_client().get("/manual")
    (req,) = entries(tagged, "request")
    assert req.tags.count("tenant:acme") == 1
    assert "verb:GET" in req.tags


def test_add_tags_outside_batch(app, tagged):
    assert add_tags("x") is False
    with app.app_context():
        assert add_tags("x") is False


def test_panel_filters_by_custom_tag(app, tagged):
    c = app.test_client()
    c.get("/deliveries/IMP-5")
    assert "IMP-5" in c.get("/leeuwenhoek/batches?tag=imported_id:IMP-5").text
    assert "lw-row" not in c.get("/leeuwenhoek/batches?tag=imported_id:nope").text
    entry = entries(tagged, "request")[0]
    page = c.get(f"/leeuwenhoek/requests/{entry.uuid}").text
    assert "?tag=imported_id:IMP-5" in page  # url_for keeps ":" unescaped
