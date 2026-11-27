"""`make email-preview`: the two emails, with made-up people.

The preview exists so the layout can be judged on a phone before anything is
sent. It therefore may not contain anybody real, and it must open on its own —
a `cid:` reference with nothing behind it shows as a broken image.
"""

import os

from sonde.notify import preview


def test_the_preview_is_standalone_and_contains_nobody_real(tmp_path):
    out = tmp_path / "preview"
    assert preview.main(["--out", str(out)]) == 0

    for name in ("interactions.html", "arrivals.html"):
        text = (out / name).read_text()
        assert "cid:" not in text
        assert "data:image/jpeg;base64," in text
        assert "did:plc:" not in text

    assert (out.stat().st_mode & 0o777) == 0o700
    for name in ("interactions.html", "arrivals.html"):
        assert (out / name).stat().st_mode & 0o077 == 0


def test_the_preview_has_both_shapes(tmp_path):
    out = tmp_path / "preview"
    preview.main(["--out", str(out)])

    arrivals = (out / "arrivals.html").read_text()
    interactions = (out / "interactions.html").read_text()
    assert "followed you" in arrivals
    assert "on your posts" in interactions
    assert "view on Bluesky" in interactions
