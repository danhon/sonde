"""The HTML alternative in both alert emails.

Two shapes, one family: cards with a post and its people for likes and reposts,
a list of people for arrivals. These tests are about the ways a designed email
goes wrong — an escaped name collapsing the layout, a `cid:` with no part
behind it, a template that grows past Gmail's clip — rather than about the
exact markup.
"""

from email.message import EmailMessage

from sonde.notify import email_html as h

ROW = {
    "did": "did:plc:a",
    "handle": "ana.example.com",
    "display_name": "Ana Example",
    "description": "Writes about trains.",
    "followers_count": 41_000,
    "verified_status": "valid",
    "trusted_verifier_status": "none",
    "influence_score": 12.0,
    "event": "followed",
    "detected_at": "2026-11-26T17:09:53.000Z",
}
IMG = b"\xff\xd8\xff\xd9"


def rows(n: int = 1) -> list[dict]:
    return [{**ROW, "did": f"did:plc:{i:02d}"} for i in range(n)]


def interactions() -> list[dict]:
    base = {k: ROW[k] for k in ("handle", "display_name", "description",
                               "followers_count", "verified_status",
                               "trusted_verifier_status")}
    return [
        {**base, "did": "did:plc:a", "kind": "like",
         "subject": "at://did:plc:me/app.bsky.feed.post/1",
         "occurred_at": "2026-11-26T17:09:53.000Z"},
        {**base, "did": "did:plc:b", "kind": "repost",
         "subject": "at://did:plc:me/app.bsky.feed.post/2",
         "occurred_at": "2026-11-26T17:41:00.000Z"},
    ]


def test_a_person_with_a_picture_gets_a_cid_reference():
    html = h.render_arrivals(rows(), {"did:plc:00": IMG})
    assert f'src="cid:{h.cid_for("did:plc:00")}"' in html


def test_a_person_without_a_picture_gets_initials_not_an_image():
    html = h.render_arrivals(rows(), {})
    assert "<img" not in html
    assert ">AE<" in html


def test_injectable_text_is_escaped():
    nasty = [{**ROW, "display_name": "<script>alert(1)</script>",
              "description": '"><img src="http://evil/x">'}]
    html = h.render_arrivals(nasty, {})
    assert "<script" not in html
    # The text may say http://evil — what it must not do is become a tag.
    assert '<img src="http://evil' not in html
    assert "&lt;script&gt;" in html


def test_every_image_is_a_cid_reference():
    html = h.render_arrivals(rows(3), {r["did"]: IMG for r in rows(3)})
    for tag in html.split("<img")[1:]:
        assert tag.split(">")[0].count('src="cid:') == 1


def test_the_arrival_cap_hides_nobody():
    html = h.render_arrivals(rows(14), {}, limit=12)
    assert "and 2 more" in html


def test_the_markup_budget_holds_at_twelve_people():
    people = rows(12)
    html = h.render_arrivals(people, {r["did"]: IMG for r in people}, limit=12)
    assert len(html.encode()) < h.MARKUP_BUDGET


def test_interactions_group_by_post_and_link_to_it():
    html = h.render_interactions(
        interactions(),
        {"at://did:plc:me/app.bsky.feed.post/1": "The bus timetable is a love letter"},
        {},
    )
    assert "The bus timetable" in html
    assert "bsky.app/profile/did:plc:me/post/1" in html or \
           "bsky.app/profile/did%3Aplc%3Ame/post/1" in html


def test_a_post_with_no_preview_is_still_a_card():
    html = h.render_interactions(interactions(), {}, {})
    assert html.count("A post of yours") == 2


def test_attach_adds_inline_parts_with_no_filename():
    message = EmailMessage()
    message.set_content("plain")
    h.attach(message, h.render_arrivals(rows(), {"did:plc:00": IMG}),
             {"did:plc:00": IMG})
    images = [p for p in message.walk() if p.get_content_type() == "image/jpeg"]
    assert len(images) == 1
    disposition = images[0]["Content-Disposition"]
    assert disposition.startswith("inline")
    assert "filename" not in disposition
    assert message.get_body(("html",)) is not None


def test_the_markup_budget_holds_for_a_twelve_person_card():
    """The card shape is the bigger of the two, so it gets its own guard."""
    people = []
    for i in range(12):
        people.append({**ROW, "did": f"did:plc:{i:02d}", "kind": "like",
                       "subject": "at://did:plc:me/app.bsky.feed.post/1",
                       "occurred_at": "2026-11-26T17:09:53.000Z"})
    html = h.render_interactions(
        people, {"at://did:plc:me/app.bsky.feed.post/1": "One post, twelve people"}, {})
    assert len(html.encode()) < h.MARKUP_BUDGET
