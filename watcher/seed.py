"""First-boot seed data: the hifi searches this project was originally scoped around.

Only runs if the searches table is empty, so it never overwrites searches
you've added or edited via the admin UI. Review scope/location on each of
these after first boot - they default to national/no-location since your
actual city wasn't known when this was written.
"""
from __future__ import annotations

import sqlite3

from watcher import searches as searches_repo
from watcher.models import Search, WatchedModel


def seed_default_searches(conn: sqlite3.Connection) -> None:
    if searches_repo.list_searches(conn):
        return
    for search in _default_searches():
        searches_repo.create_search(conn, search)


def _default_searches():
    return [
        Search(
            name="Living room - AV receiver",
            scope="national",
            scoring_mode="rated",
            max_price=3000,
            hard_criteria=[
                "AV receiver with 4K/HDCP 2.2 passthrough required (HDMI 2.0 is enough, HDMI 2.1 is not required)",
                "Marantz '00x' series (e.g. SR4001/SR5001/SR6001/SR7001/SR8001, NR1501) lacks 4K - exclude",
            ],
            soft_criteria=["Network-playback capable (AirPlay / Chromecast / DLNA)"],
            watched_models=[
                WatchedModel(pattern="TX-NR646", note="HDCP 2.2 only on some inputs - verify which before buying", good_price="1500-2500 SEK"),
                WatchedModel(pattern="TX-NR656", note="solid mid-range Onkyo, full HDCP 2.2", good_price="1800-2800 SEK"),
                WatchedModel(pattern="TX-NR676*", note="", good_price="2000-3000 SEK"),
                WatchedModel(pattern="NR1606", note="Marantz, ~2015", good_price="2000-3000 SEK"),
                WatchedModel(pattern="SR5010", note="Marantz, ~2015", good_price="2500-3500 SEK"),
                WatchedModel(pattern="SR6010", note="Marantz, ~2015", good_price="3000-4000 SEK"),
            ],
            excluded_models=["TX-SR607", "TX-SR578", "NR1604", "SR3001", "SR6003", "AVR-1509"],
            search_phrases=["onkyo tx-nr", "marantz sr", "marantz nr"],
            marketplaces=["blocket"],
        ),
        Search(
            name="Living room - subwoofer",
            scope="national",
            scoring_mode="rated",
            max_price=5000,
            hard_criteria=["Standalone active subwoofer, max ~37cm (370mm) wide"],
            soft_criteria=["Other 12-inch subs within the width limit are also interesting, not just the watched models"],
            watched_models=[
                WatchedModel(pattern="R-120SW", note="Klipsch, new or used", good_price="3000-4500 SEK"),
                WatchedModel(pattern="99 W12.16", note="XTZ, used", good_price="3000-4500 SEK"),
                WatchedModel(pattern="Spirit Sub 12", note="XTZ, used", good_price="2500-4000 SEK"),
            ],
            excluded_models=["Sub 12.17"],
            search_phrases=["klipsch subwoofer", "xtz sub"],
            marketplaces=["blocket"],
        ),
        Search(
            name="Living room - front speakers",
            scope="national",
            scoring_mode="rated",
            hard_criteria=[
                "Must replace Jamo E470 - needs to be a clear step up in sound quality, otherwise not interesting",
                "Black finish strongly preferred",
                "Standard dual binding-post terminals (flag anything proprietary)",
                "Verify nominal impedance - flag anything 4 ohm nominal for AV receiver compatibility",
            ],
            soft_criteria=[
                "Standmount/bookshelf is a plus (frees up floor space next to the subwoofer)",
                "Deep bass extension is not a priority - the subwoofer handles low frequencies",
            ],
            watched_models=[
                WatchedModel(pattern="Oberon 1", note="Dali, standmount, 6 ohm, black ash available, clear step up from Jamo E470", good_price="3000-4500 SEK/pair"),
                WatchedModel(pattern="Oberon 3", note="Dali, standmount, 6 ohm, black ash available", good_price="3500-5000 SEK/pair"),
                WatchedModel(pattern="Oberon 5", note="Dali, floorstanding, 6 ohm, black ash available", good_price="5000-7000 SEK/pair"),
                WatchedModel(pattern="Oberon 7", note="Dali, floorstanding, 6 ohm, black ash available", good_price="6000-9000 SEK/pair"),
                WatchedModel(pattern="Oberon 9", note="Dali, floorstanding, 4 ohm nominal - verify receiver compatibility", good_price="7000-10000 SEK/pair"),
                WatchedModel(pattern="Spektor*", note="Dali, 6 ohm, black available, but only a marginal step up from Jamo E470 (Dali's own budget tier)", good_price="1500-2500 SEK/pair"),
                WatchedModel(pattern="Rubi*", note="Dali Rubicon/Rubikore, 4 ohm nominal - verify receiver compatibility, premium price, Rubicon being discontinued in favor of Rubikore", good_price=""),
                WatchedModel(pattern="Diamond 12.1i", note="Wharfedale, standmount, 8 ohm, Deep Black finish available, clear step up", good_price="2500-4000 SEK/pair"),
                WatchedModel(pattern="Diamond 12.3i", note="Wharfedale, floorstanding, 8 ohm, Deep Black finish available", good_price="3500-5500 SEK/pair"),
                WatchedModel(pattern="EVO 4.1", note="Wharfedale, standmount, 8 ohm nominal (min ~4 ohm), black ash available", good_price="4000-6000 SEK/pair"),
                WatchedModel(pattern="EVO 4.2", note="Wharfedale, standmount, 8 ohm nominal (min ~4 ohm), black ash available", good_price="4000-6000 SEK/pair"),
                WatchedModel(pattern="EVO 4.3", note="Wharfedale, floorstanding, 8 ohm nominal", good_price="5000-8000 SEK/pair"),
                WatchedModel(pattern="EVO 4.4", note="Wharfedale, floorstanding, 8 ohm nominal", good_price="5000-8000 SEK/pair"),
                WatchedModel(pattern="Linton*", note="Wharfedale, retro wood-veneer standmount - NOT a black modern look despite being a sonic step up, only interesting if the black-finish requirement is relaxed", good_price=""),
                WatchedModel(pattern="Denton*", note="Wharfedale, retro wood-veneer standmount, 4 ohm nominal, same aesthetic caveat as Linton", good_price=""),
                WatchedModel(pattern="301*i", note="Q Acoustics 3010i/3020i/3030i, standmount, 6 ohm nominal (4 ohm min), Carbon Black available", good_price="2000-3500 SEK/pair"),
                WatchedModel(pattern="3050i", note="Q Acoustics, floorstanding, 6 ohm nominal, Carbon Black available", good_price="4000-6000 SEK/pair"),
            ],
            search_phrases=[
                "dali oberon", "dali spektor", "dali rubicon",
                "wharfedale diamond", "wharfedale evo",
                "q acoustics 3",
            ],
            marketplaces=["blocket"],
        ),
        Search(
            name="Living room - center speaker",
            scope="national",
            scoring_mode="rated",
            soft_criteria=[
                "Low priority - current Proson Reality center (exact model unknown) is acceptable but could be upgraded",
                "A center from the same series/brand as the new front speakers scores higher (tonal matching)",
                "A standalone center with no matching front speakers only scores high at a really good price",
            ],
            search_phrases=["centerhögtalare"],
            marketplaces=["blocket"],
        ),
        Search(
            name="Living room - rear speakers",
            scope="national",
            scoring_mode="rated",
            enabled=False,
            soft_criteria=["Not actively watched - only score 8+ for a major upgrade in a discreet/compact format"],
            search_phrases=["bakhögtalare surround"],
            marketplaces=["blocket"],
        ),
        Search(
            name="Stugan hifi",
            scope="national",
            scoring_mode="rated",
            hard_criteria=[
                "Max 2.1 configuration - no large receivers, space is limited",
                "Aesthetics are important",
            ],
            soft_criteria=["Phono stage is a plus (vinyl is a long-term goal)", "Sub-out is a plus"],
            watched_models=[
                WatchedModel(pattern="Stereo 130", note="Leak, walnut/silver finish"),
                WatchedModel(pattern="SX-450", note="Pioneer, small 70s wood-cabinet receiver, preferred over larger SX models"),
                WatchedModel(pattern="SX-550", note="Pioneer, small 70s wood-cabinet receiver, preferred over larger SX models"),
                WatchedModel(pattern="Sansui*", note="70s wood-cabinet receiver"),
                WatchedModel(pattern="KR-*", note="Kenwood, 70s wood-cabinet receiver"),
                WatchedModel(pattern="22*", note="Marantz 22xx series, 70s wood-cabinet receiver"),
                WatchedModel(pattern="Luxor*", note="70s wood-cabinet receiver"),
                WatchedModel(pattern="Tandberg*", note="70s wood-cabinet receiver"),
                WatchedModel(pattern="Douk Audio*", note="retro amplifier"),
                WatchedModel(pattern="Nobsound*", note="retro amplifier"),
            ],
            search_phrases=[
                "leak stereo 130", "pioneer sx-450", "pioneer sx-550",
                "sansui receiver", "kenwood kr", "marantz 22",
                "luxor receiver", "tandberg receiver",
                "douk audio", "nobsound",
            ],
            marketplaces=["blocket"],
        ),
    ]
