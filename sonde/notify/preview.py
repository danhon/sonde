"""Both alert emails, rendered from made-up data, for a human to look at.

`make email-preview` writes two files into a mode-700 temporary directory and
prints their paths. The email itself is only ever seen through a mail client's
idea of HTML, which is not this repo; this is the closest thing to a proof of
layout before the first real alert goes out.

It contains nobody real, on purpose (SHOT-2): the names are invented, the
handles are invented, and every picture is a circle drawn here with an initial
in it. No avatar is fetched and no DID from the database appears.

The `cid:` references are swapped for `data:` URIs so the files open on their
own, without the MIME parts around them.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from sonde.notify import email_html

PEOPLE = [
    ("Ana Example", "ana.example.com", "Writes about trains and the cities built around them."),
    ("Bo Example", "bo.example.com", "Forty-one thousand people follow this fictional account."),
    ("Cy Example", "cy.example.com", ""),
    ("Di Example", "di.example.com", "A made-up bio, here to show a long line wrapping at the card's edge."),
]

POSTS = [
    "at://did:example:me/app.bsky.feed.post/first",
    "at://did:example:me/app.bsky.feed.post/second",
]

PREVIEWS = {
    POSTS[0]: "The bus timetable is a love letter to the idea that a city can be legible.",
    POSTS[1]: "Nobody has ever explained why the good bakery closes at two.",
}

WHEN = "2026-11-26T17:09:53.000Z"


def _did(index: int) -> str:
    return f"did:example:person{index}"


def _picture(initial: str, background: tuple[int, int, int]) -> bytes:
    """A 128px circle with a letter in it — nobody's face."""
    image = Image.new("RGB", (128, 128), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((0, 0, 127, 127), fill=background)
    # No truetype file is guaranteed to exist, so the letter is drawn from the
    # default bitmap font; the preview is about layout, not typography.
    draw.text((56, 52), initial, fill=(255, 255, 255))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=80)
    return out.getvalue()


def _person(index: int, **extra) -> dict:
    name, handle, bio = PEOPLE[index % len(PEOPLE)]
    return {
        "did": _did(index),
        "handle": handle,
        "display_name": name,
        "description": bio or None,
        "followers_count": 41_000 if index % 2 else 812,
        "verified_status": "valid" if index % 2 == 0 else "none",
        "trusted_verifier_status": "none",
        "influence_score": 12.0,
        "event": "followed",
        "detected_at": WHEN,
        **extra,
    }


def _arrivals() -> list[dict]:
    return [_person(i) for i in range(3)]


def _interactions() -> list[dict]:
    return [
        _person(0, kind="like", subject=POSTS[0], occurred_at=WHEN),
        _person(1, kind="repost", subject=POSTS[0], occurred_at=WHEN),
        _person(2, kind="like", subject=POSTS[1], occurred_at=WHEN),
    ]


def _images(count: int) -> dict[str, bytes]:
    colours = [(15, 23, 42), (153, 27, 27), (51, 65, 85), (120, 53, 15)]
    return {_did(i): _picture(PEOPLE[i % len(PEOPLE)][0][:1], colours[i % len(colours)])
            for i in range(count)}


def _inline(html: str, images: dict[str, bytes]) -> str:
    """Swap every `cid:` for the JPEG it stands for, in a data URI."""
    pictures = dict(images)
    if email_html.CHECK_CID in html:
        pictures[email_html.CHECK_CID] = email_html.check_png()
    for did, jpeg in pictures.items():
        token = email_html.cid_for(did) if did in images else did
        mime = "jpeg" if did in images else "png"
        data = f"data:image/{mime};base64," + base64.b64encode(jpeg).decode()
        html = html.replace(f"cid:{token}", data)
    return html


def _write(path: Path, text: str) -> None:
    path.write_text(text)
    os.chmod(path, 0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="directory to write both previews into")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)

    images = _images(len(PEOPLE))
    arrivals = email_html.render_arrivals(_arrivals(), images)
    interactions = email_html.render_interactions(_interactions(), PREVIEWS, images)

    for name, html in (("arrivals.html", arrivals), ("interactions.html", interactions)):
        _write(out / name, _inline(html, images))
        print(out / name)

    if re.search(r"cid:", (out / "arrivals.html").read_text()):
        print("preview still has cid: references", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # `make email-preview` runs this as a module
    sys.exit(main())
