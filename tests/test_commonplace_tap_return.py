"""Email confirmation preserves a card tap's workspace and fragment."""

from urllib.parse import parse_qs, urlsplit

from commonplace import urls
from tests.test_commonplace_signup_space import fresh as fresh


def test_email_confirmation_preserves_workspace_and_card_tap(fresh):
    client, mailbox = fresh()
    destination = "/app/?w=W#/card/5"
    status, _, body = client.request(
        "/app/auth/email",
        "POST",
        raw="email=tap%40example.test&next=%2Fapp%2F%3Fw%3DW%23%2Fcard%2F5",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": urls.APP_URL},
    )
    assert status == 202, (status, body)
    link = urlsplit(mailbox.link())
    assert parse_qs(link.query).get("t"), link
    status, _, body = client.request(link.path + "?" + link.query)
    assert status == 200, (status, body)
    status, headers, body = client.verify(link.geturl(), headers={"Origin": urls.APP_URL})
    assert status == 303, (status, body)
    assert headers.get("Location", "").endswith(destination), headers
