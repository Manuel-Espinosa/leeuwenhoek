import pytest
from conftest import entries
from flask import render_template, render_template_string, stream_template

from leeuwenhoek import Leeuwenhoek
from leeuwenhoek.watchers import template as template_watcher

TEMPLATES = {
    "page.html": "{% extends 'layout.html' %}{% block body %}{{ title }}"
    "{% include 'part.html' %}{% endblock %}",
    "layout.html": "<html>{% block body %}{% endblock %}</html>",
    "part.html": "<p>{{ user }}</p>",
    "outer.html": "[{{ inner() }}]",
    "inner.html": "{{ secret }}",
    "broken.html": "{{ 1 // 0 }}",
}


@pytest.fixture
def templates(app, tmp_path):
    for name, source in TEMPLATES.items():
        (tmp_path / name).write_text(source)
    app.template_folder = str(tmp_path)  # read lazily on the first render


@pytest.fixture
def page(app, lw, templates):
    @app.get("/page")
    def view():
        return render_template("page.html", title="Hi", user="ana", password="hunter2")


def test_records_template_with_context_keys(client, lw, page):
    client.get("/page")
    rendered = entries(lw, "template")
    # extends and include render inside the top-level template: a single entry
    assert len(rendered) == 1
    c = rendered[0].content
    assert c["name"] == "page.html"
    assert c["file"].endswith("page.html")
    # keys only, never values; Flask's own g, request and session are left out
    assert c["context_keys"] == ["password", "title", "user"]
    assert "hunter2" not in str(c)
    assert c["duration_ms"] is not None and c["duration_ms"] >= 0
    assert "template:page.html" in rendered[0].tags
    assert rendered[0].batch_id == entries(lw, "request")[0].batch_id
    assert not rendered[0].is_root


def test_panel_templates_not_recorded(client, lw, page):
    client.get("/page")
    batch_id = entries(lw, "template")[0].batch_id
    client.get("/leeuwenhoek/templates")
    client.get(f"/leeuwenhoek/batch/{batch_id}")
    client.get(f"/leeuwenhoek/templates/{entries(lw, 'template')[0].uuid}")
    assert [e.content["name"] for e in entries(lw, "template")] == ["page.html"]


def test_own_templates_never_recorded_even_when_rendered_by_the_app(app, lw, templates):
    with app.app_context():
        render_template("leeuwenhoek/entries/dump.html")
    assert entries(lw, "template") == []


def test_standalone_outside_a_request(app, lw, templates):
    with app.app_context():
        assert render_template("part.html", user="ana") == "<p>ana</p>"
    [entry] = entries(lw, "template")
    assert entry.is_root and entry.content["name"] == "part.html"
    assert entries(lw, "request") == []


def test_string_templates(app, lw):
    with app.app_context():
        render_template_string("{{ x }}", x=1)
    [entry] = entries(lw, "template")
    assert entry.content["name"] is None
    assert entry.content["context_keys"] == ["x"]
    assert entry.tags == []


def test_nested_renders_are_paired(app, lw, templates):
    @app.context_processor
    def helpers():
        return {"inner": lambda: render_template("inner.html", secret="s")}

    with app.app_context():
        assert render_template("outer.html") == "[s]"
    by_name = {e.content["name"]: e.content for e in entries(lw, "template")}
    assert set(by_name) == {"outer.html", "inner.html"}
    assert by_name["outer.html"]["duration_ms"] >= by_name["inner.html"]["duration_ms"]
    assert "inner" in by_name["outer.html"]["context_keys"]


def test_failed_render_leaves_no_pending_start(app, lw, templates):
    with app.app_context():
        with pytest.raises(ZeroDivisionError):
            render_template("broken.html")
        assert entries(lw, "template") == []
        render_template("part.html", user="ana")
    [entry] = entries(lw, "template")
    assert entry.content["name"] == "part.html" and entry.content["duration_ms"] is not None
    # the failed render holds no reference to its context
    assert all(isinstance(key, int) for key, _ in template_watcher._stack.get())


def test_streamed_template(app, lw, templates):
    @app.get("/stream")
    def stream():
        return stream_template("part.html", user="ana")

    assert app.test_client().get("/stream").data == b"<p>ana</p>"
    # rendering ends after the request batch is closed: the entry is standalone
    [entry] = entries(lw, "template")
    assert entry.content["name"] == "part.html" and entry.is_root
    assert entry.content["duration_ms"] is not None


def test_listed_in_menu_and_detail_renders(client, lw, page):
    client.get("/page")
    html = client.get("/leeuwenhoek/templates").get_data(as_text=True)
    assert "Templates" in html and "page.html" in html
    entry = entries(lw, "template")[0]
    detail = client.get(f"/leeuwenhoek/templates/{entry.uuid}").get_data(as_text=True)
    assert "password" in detail and "page.html" in detail
    # related card in the request detail
    request_entry = entries(lw, "request")[0]
    html = client.get(f"/leeuwenhoek/requests/{request_entry.uuid}").get_data(as_text=True)
    assert "Templates" in html and "page.html" in html


def test_disabled_watcher(app, templates):
    app.config["LEEUWENHOEK_WATCHERS"] = ["request"]
    ext = Leeuwenhoek(app)
    with app.app_context():
        render_template("part.html", user="ana")
    assert entries(ext, "template") == []
    ext.close()


def test_unregister_disconnects_signals(app, templates):
    ext = Leeuwenhoek(app)
    ext.close()
    pending = template_watcher._stack.get()
    with app.app_context():
        render_template("part.html", user="ana")
    assert entries(ext, "template") == []
    assert template_watcher._stack.get() == pending
