import pytest
from conftest import entries
from flask_mail import Attachment, Mail, Message, email_dispatched

from leeuwenhoek import Leeuwenhoek


@pytest.fixture
def mail(app):
    # TESTING suppresses delivery, but Flask-Mail still sends ``email_dispatched``.
    app.config.update(MAIL_DEFAULT_SENDER=("Shop", "shop@example.com"))
    return Mail(app)


@pytest.fixture
def lw(app, mail):
    ext = Leeuwenhoek(app)

    @app.post("/signup")
    def signup():
        mail.send(
            Message(
                "Welcome",
                recipients=["ana@example.com", ("Bob", "bob@example.com")],
                cc=["cc@example.com"],
                bcc=["audit@example.com"],
                body="Hello Ana",
                html="<p>Hello <b>Ana</b></p>",
                attachments=[Attachment("invoice.pdf", "application/pdf", b"%PDF-secret")],
                extra_headers={"X-Api-Key": "abc", "X-Campaign": "welcome"},
            )
        )
        return "ok"

    yield ext
    ext.close()


def test_mail_in_request_joins_batch(app, lw):
    app.test_client().post("/signup")
    (mail,) = entries(lw, "mail")
    (req,) = entries(lw, "request")
    assert mail.batch_id == req.batch_id
    assert not mail.is_root
    c = mail.content
    assert c["subject"] == "Welcome"
    assert c["sender"] == "Shop <shop@example.com>"
    assert c["to"] == ["ana@example.com", "Bob <bob@example.com>"]
    assert c["cc"] == ["cc@example.com"]
    assert c["bcc"] == ["audit@example.com"]
    assert c["text"] == "Hello Ana"
    assert c["html"] == "<p>Hello <b>Ana</b></p>"
    assert c["attachments"] == [{"filename": "invoice.pdf", "content_type": "application/pdf"}]
    assert "PDF-secret" not in str(c)
    assert c["headers"] == {"X-Api-Key": "********", "X-Campaign": "welcome"}


def test_mail_outside_request_is_standalone(app, lw, mail):
    with app.app_context():
        mail.send(Message("Report", recipients=["ops@example.com"], body="Daily"))
    (entry,) = entries(lw, "mail")
    assert entry.is_root
    assert lw.storage.batch(entry.batch_id) == [entry]
    assert entry.content["html"] is None


def test_mail_bodies_truncated(app, mail):
    app.config["LEEUWENHOEK_MAX_BODY_KB"] = 1
    ext = Leeuwenhoek(app)
    with app.app_context():
        mail.send(Message("Big", recipients=["a@example.com"], body="x" * 5000, html="y" * 5000))
    ext.close()
    c = entries(ext, "mail")[0].content
    for key in ("text", "html"):
        assert len(c[key]) < 1200
        assert c[key].endswith("[truncated: 5000 characters]")


def test_mail_detail_iframe_is_sandboxed_and_escaped(app, lw, mail):
    html = '<p onclick="x()">Hi & bye</p><script>alert("pwn")</script>'
    with app.app_context():
        mail.send(Message("XSS", recipients=["a@example.com"], html=html, body="plain text"))
    entry = entries(lw, "mail")[0]
    page = app.test_client().get(f"/leeuwenhoek/mail/{entry.uuid}").text
    start = page.index("<iframe")
    iframe = page[start : page.index(">", page.index("srcdoc=", start))]
    assert 'sandbox=""' in iframe
    assert "allow-scripts" not in page and "allow-same-origin" not in page
    assert (
        'srcdoc="&lt;p onclick=&#34;x()&#34;&gt;Hi &amp; bye&lt;/p&gt;'
        '&lt;script&gt;alert(&#34;pwn&#34;)&lt;/script&gt;"' in iframe
    )
    assert "<script>alert" not in page
    assert "plain text" in page


def test_mail_listing_and_related_card(app, lw):
    client = app.test_client()
    client.post("/signup")
    listing = client.get("/leeuwenhoek/mail")
    assert listing.status_code == 200 and "Welcome" in listing.text
    req = entries(lw, "request")[0]
    detail = client.get(f"/leeuwenhoek/requests/{req.uuid}").text
    assert 'card-header-title">Mail ' in detail and "Welcome" in detail
    assert "Mail" in client.get("/leeuwenhoek/requests").text


def test_unregister_disconnects_signal(app, mail):
    before = len(email_dispatched.receivers)
    ext = Leeuwenhoek(app)
    assert len(email_dispatched.receivers) == before + 1
    ext.close()
    assert len(email_dispatched.receivers) == before
    with app.app_context():
        mail.send(Message("After", recipients=["a@example.com"], body="x"))
    assert entries(ext, "mail") == []
