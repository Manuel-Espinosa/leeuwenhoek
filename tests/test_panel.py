import pytest
from conftest import entries


@pytest.fixture
def populated(client, lw):
    client.get("/hello")
    client.get("/boom")
    lw.app.logger.error("standalone")
    return lw


@pytest.mark.parametrize(
    "path",
    ["/", "/requests", "/batches", "/queries", "/logs", "/exceptions", "/dumps", "/api/status"],
)
def test_views_render(populated, client, path):
    resp = client.get("/leeuwenhoek" + path, follow_redirects=True)
    assert resp.status_code == 200, resp.text


def test_detail_batch_and_family(populated, client, lw):
    for type_, slug in [
        ("request", "requests"),
        ("query", "queries"),
        ("log", "logs"),
        ("exception", "exceptions"),
    ]:
        entry = entries(lw, type_)[0]
        resp = client.get(f"/leeuwenhoek/{slug}/{entry.uuid}")
        assert resp.status_code == 200, (type_, resp.text)
    exc = entries(lw, "exception")[0]
    assert client.get(f"/leeuwenhoek/batch/{exc.batch_id}").status_code == 200
    assert client.get(f"/leeuwenhoek/exceptions/family/{exc.family_hash}").status_code == 200


def test_dump_view(app, lw, client):
    from leeuwenhoek import dump

    app.add_url_rule("/d", "d", lambda: dump([1, 2]) and "ok")
    client.get("/d")
    d = entries(lw, "dump")[0]
    assert client.get(f"/leeuwenhoek/dumps/{d.uuid}").status_code == 200


def test_unknown_section_and_wrong_slug(populated, client, lw):
    assert client.get("/leeuwenhoek/nope").status_code == 404
    req = entries(lw, "request")[0]
    assert client.get(f"/leeuwenhoek/logs/{req.uuid}").status_code == 404


def test_filters_and_pagination(client, lw):
    for _ in range(5):
        client.get("/hello")
    page = lw.storage.page("request", limit=2)
    assert len(page.entries) == 2 and page.older and not page.newer
    page2 = lw.storage.page("request", before=page.older, limit=2)
    assert page2.newer and page2.older
    back = lw.storage.page("request", after=page2.newer, limit=2)
    assert [e.id for e in back.entries] == [e.id for e in page.entries]
    assert client.get("/leeuwenhoek/requests?tag=status:200").status_code == 200
    assert len(lw.storage.list("request", q="HELLO")) == 5
    assert lw.storage.list("request", tag="status:404") == []


def test_toggle_requires_csrf_and_clear(populated, client, lw):
    assert client.post("/leeuwenhoek/clear").status_code == 400
    client.get("/leeuwenhoek/requests")  # issues the cookie
    token = client.get_cookie("lw_csrf").value
    assert (
        client.post("/leeuwenhoek/recording/toggle", data={"csrf_token": token}).status_code == 302
    )
    assert lw.is_recording() is False
    client.post("/leeuwenhoek/clear", data={"csrf_token": token})
    assert entries(lw) == []


def test_access_control(app, lw, client):
    app.debug = False
    assert client.get("/leeuwenhoek/requests").status_code == 403
    lw.authorize(lambda: True)
    assert client.get("/leeuwenhoek/requests").status_code == 200
    lw.authorize(lambda: False)
    assert client.get("/leeuwenhoek/requests").status_code == 403


def test_basic_auth(app, engine):
    from leeuwenhoek import Leeuwenhoek

    app.config["LEEUWENHOEK_BASIC_AUTH"] = ("u", "p")
    ext = Leeuwenhoek(app, engine=engine)
    c = app.test_client()
    assert c.get("/leeuwenhoek/requests").status_code == 401
    auth = {"Authorization": "Basic dTpw"}
    assert c.get("/leeuwenhoek/requests", headers=auth).status_code == 200
    ext.close()


def test_disabled_is_404(app, engine):
    from leeuwenhoek import Leeuwenhoek

    app.config["LEEUWENHOEK_ENABLED"] = False
    ext = Leeuwenhoek(app, engine=engine)
    assert app.test_client().get("/leeuwenhoek/requests").status_code == 404
    ext.close()


def test_prune_and_clear(populated, lw):
    assert lw.prune(hours=1) == 0
    assert lw.prune(hours=0) > 0
    assert entries(lw) == []


def test_theme_toggle_and_head_script(client):
    html = client.get("/leeuwenhoek/requests").text
    head = html.split("</head>", 1)[0]
    # The theme is applied in <head>, before the stylesheets, to avoid a flash on load.
    assert '<script id="lw-theme-init">' in head
    assert 'localStorage.getItem("leeuwenhoek-theme")' in head
    assert head.index("lw-theme-init") < head.index("bulma.min.css")
    assert 'id="lw-theme-toggle"' in html.split("</head>", 1)[1]


def test_theme_script_uses_host_csp_nonce(app, client):
    app.jinja_env.globals["csp_nonce"] = lambda: "abc123"
    html = client.get("/leeuwenhoek/requests").text
    assert '<script id="lw-theme-init" nonce="abc123">' in html


def test_bundled_bulma_supports_data_theme(client):
    css = client.get("/leeuwenhoek/static/leeuwenhoek/bulma.min.css").text
    assert "bulma.io v1." in css
    assert "[data-theme=dark]" in css and "[data-theme=light]" in css


def test_static_and_no_template_collision(app, client):
    app.add_url_rule("/", "index", lambda: "mi index")
    assert client.get("/").text == "mi index"
    assert client.get("/leeuwenhoek/static/leeuwenhoek/bulma.min.css").status_code == 200
