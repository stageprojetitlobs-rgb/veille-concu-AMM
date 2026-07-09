"""Tests du collecteur RSS générique — parsing pur, sans réseau."""
from __future__ import annotations

from veille.sources.generic.rss import _parse_items, _parse_date

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Laprovet</title>
  <item>
    <title>Les Trypanocides LAPROVET</title>
    <link>https://www.laprovet.com/wp/laprovet-trypanocides/</link>
    <guid>http://www.laprovet.com/wp/?p=760</guid>
    <pubDate>Wed, 24 Feb 2016 10:00:00 +0000</pubDate>
    <description>Gamme trypanocide pour l'Afrique.</description>
  </item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Exemple</title>
  <entry>
    <title>Nouveau vaccin aviaire</title>
    <link href="https://example.com/a"/>
    <id>tag:example.com,2026:1</id>
    <updated>2026-06-01T08:00:00Z</updated>
    <summary>Lancement.</summary>
  </entry>
</feed>"""


def test_parse_rss():
    items = _parse_items(RSS)
    assert len(items) == 1
    it = items[0]
    assert it["titre"] == "Les Trypanocides LAPROVET"
    assert it["uid"] == "http://www.laprovet.com/wp/?p=760"
    assert it["lien"].endswith("laprovet-trypanocides/")
    assert it["date"].year == 2016


def test_parse_atom():
    items = _parse_items(ATOM)
    assert len(items) == 1
    it = items[0]
    assert it["titre"] == "Nouveau vaccin aviaire"
    assert it["uid"] == "tag:example.com,2026:1"
    assert it["lien"] == "https://example.com/a"
    assert it["date"].year == 2026


def test_parse_date_formats():
    assert _parse_date("Wed, 24 Feb 2016 10:00:00 +0000").year == 2016  # RFC822
    assert _parse_date("2026-06-01T08:00:00Z").month == 6               # ISO8601
    assert _parse_date("") is None
    assert _parse_date("pas une date") is None
