"""Swedish county (län) <-> municipality (kommun) geography, as Blocket's own
search API reports it (fetched live from the "location" facet of a search
response, 2026-10 - 21 counties, 290 municipalities total). Blocket is the
only marketplace with a confirmed working server-side location filter, but
the municipality lists are plain Swedish geography and are reused by
pipeline._filter_by_scope for every marketplace's client-side location match
(see that function's docstring for why a naive substring match isn't enough).

backlog #26 previously concluded Blocket's search API 400s on *any* location
value - true for a plain county name like "Norrbotten", but that was the
wrong param shape. Confirmed live (2026-10): passing the county's own facet
code (e.g. "0.300025" for Norrbotten) in the same `location` query param
returns a correctly server-side-filtered result set.
"""
from __future__ import annotations

from typing import Dict, List, Optional

COUNTY_CODE: Dict[str, str] = {
    "Blekinge": "0.300010",
    "Dalarna": "0.300020",
    "Gotland": "0.300009",
    "Gävleborg": "0.300021",
    "Halland": "0.300013",
    "Jämtland": "0.300023",
    "Jönköping": "0.300006",
    "Kalmar": "0.300008",
    "Kronoberg": "0.300007",
    "Norrbotten": "0.300025",
    "Skåne": "0.300012",
    "Stockholm": "0.300001",
    "Södermanland": "0.300004",
    "Uppsala": "0.300003",
    "Värmland": "0.300017",
    "Västerbotten": "0.300024",
    "Västernorrland": "0.300022",
    "Västmanland": "0.300019",
    "Västra Götaland": "0.300014",
    "Örebro": "0.300018",
    "Östergötland": "0.300005",
}

COUNTY_MUNICIPALITIES: Dict[str, List[str]] = {
    "Blekinge": ["Karlshamn", "Karlskrona", "Olofström", "Ronneby", "Sölvesborg"],
    "Dalarna": ["Avesta", "Borlänge", "Falun", "Gagnef", "Hedemora", "Leksand", "Ludvika", "Malung-Sälen", "Mora", "Orsa", "Rättvik", "Smedjebacken", "Säter", "Vansbro", "Älvdalen"],
    "Gotland": ["Gotland"],
    "Gävleborg": ["Bollnäs", "Gävle", "Hofors", "Hudiksvall", "Ljusdal", "Nordanstig", "Ockelbo", "Ovanåker", "Sandviken", "Söderhamn"],
    "Halland": ["Falkenberg", "Halmstad", "Hylte", "Kungsbacka", "Laholm", "Varberg"],
    "Jämtland": ["Berg", "Bräcke", "Härjedalen", "Krokom", "Ragunda", "Strömsund", "Åre", "Östersund"],
    "Jönköping": ["Aneby", "Eksjö", "Gislaved", "Gnosjö", "Habo", "Jönköping", "Mullsjö", "Nässjö", "Sävsjö", "Tranås", "Vaggeryd", "Vetlanda", "Värnamo"],
    "Kalmar": ["Borgholm", "Emmaboda", "Hultsfred", "Högsby", "Kalmar", "Mönsterås", "Mörbylånga", "Nybro", "Oskarshamn", "Torsås", "Vimmerby", "Västervik"],
    "Kronoberg": ["Alvesta", "Lessebo", "Ljungby", "Markaryd", "Tingsryd", "Uppvidinge", "Växjö", "Älmhult"],
    "Norrbotten": ["Arjeplog", "Arvidsjaur", "Boden", "Gällivare", "Haparanda", "Jokkmokk", "Kalix", "Kiruna", "Luleå", "Pajala", "Piteå", "Älvsbyn", "Överkalix", "Övertorneå"],
    "Skåne": ["Bjuv", "Bromölla", "Burlöv", "Båstad", "Eslöv", "Helsingborg", "Hässleholm", "Höganäs", "Hörby", "Höör", "Klippan", "Kristianstad", "Kävlinge", "Landskrona", "Lomma", "Lund", "Malmö", "Osby", "Perstorp", "Simrishamn", "Sjöbo", "Skurup", "Staffanstorp", "Svalöv", "Svedala", "Tomelilla", "Trelleborg", "Vellinge", "Ystad", "Ängelholm", "Åstorp", "Örkelljunga", "Östra Göinge"],
    "Stockholm": ["Botkyrka", "Danderyd", "Ekerö", "Haninge", "Huddinge", "Järfälla", "Lidingö", "Nacka", "Norrtälje", "Nykvarn", "Nynäshamn", "Salem", "Sigtuna", "Sollentuna", "Solna", "Stockholm", "Sundbyberg", "Södertälje", "Tyresö", "Täby", "Upplands Väsby", "Upplands-Bro", "Vallentuna", "Vaxholm", "Värmdö", "Österåker"],
    "Södermanland": ["Eskilstuna", "Flen", "Gnesta", "Katrineholm", "Nyköping", "Oxelösund", "Strängnäs", "Trosa", "Vingåker"],
    "Uppsala": ["Enköping", "Heby", "Håbo", "Knivsta", "Tierp", "Uppsala", "Älvkarleby", "Östhammar"],
    "Värmland": ["Arvika", "Eda", "Filipstad", "Forshaga", "Grums", "Hagfors", "Hammarö", "Karlstad", "Kil", "Kristinehamn", "Munkfors", "Storfors", "Sunne", "Säffle", "Torsby", "Årjäng"],
    "Västerbotten": ["Bjurholm", "Dorotea", "Lycksele", "Malå", "Nordmaling", "Norsjö", "Robertsfors", "Skellefteå", "Sorsele", "Storuman", "Umeå", "Vilhelmina", "Vindeln", "Vännäs", "Åsele"],
    "Västernorrland": ["Härnösand", "Kramfors", "Sollefteå", "Sundsvall", "Timrå", "Ånge", "Örnsköldsvik"],
    "Västmanland": ["Arboga", "Fagersta", "Hallstahammar", "Kungsör", "Köping", "Norberg", "Sala", "Skinnskatteberg", "Surahammar", "Västerås"],
    "Västra Götaland": ["Ale", "Alingsås", "Bengtsfors", "Bollebygd", "Borås", "Dals-Ed", "Essunga", "Falköping", "Färgelanda", "Grästorp", "Gullspång", "Göteborg", "Götene", "Herrljunga", "Hjo", "Härryda", "Karlsborg", "Kungälv", "Lerum", "Lidköping", "Lilla Edet", "Lysekil", "Mariestad", "Mark", "Mellerud", "Munkedal", "Mölndal", "Orust", "Partille", "Skara", "Skövde", "Sotenäs", "Stenungsund", "Strömstad", "Svenljunga", "Tanum", "Tibro", "Tidaholm", "Tjörn", "Tranemo", "Trollhättan", "Töreboda", "Uddevalla", "Ulricehamn", "Vara", "Vänersborg", "Vårgårda", "Åmål", "Öckerö"],
    "Örebro": ["Askersund", "Degerfors", "Hallsberg", "Hällefors", "Karlskoga", "Kumla", "Laxå", "Lekeberg", "Lindesberg", "Ljusnarsberg", "Nora", "Örebro"],
    "Östergötland": ["Boxholm", "Finspång", "Kinda", "Linköping", "Mjölby", "Motala", "Norrköping", "Söderköping", "Vadstena", "Valdemarsvik", "Ydre", "Åtvidaberg", "Ödeshög"],
}

_COUNTY_CODE_LOWER = {name.lower(): code for name, code in COUNTY_CODE.items()}


def resolve_county_code(location: str) -> Optional[str]:
    """Maps a free-text location (as typed into a search's "location" field)
    to Blocket's own county facet code, for the server-side `location` query
    param - e.g. "Norrbotten" -> "0.300025". Only matches a county name
    (case-insensitively, trimming a trailing "län" and the Swedish genitive
    "s" it takes in that form, e.g. "Norrbottens län"); anything else (a city
    name, a typo, a non-Swedish location) returns None so the caller falls
    back to no server-side filter rather than guessing wrong."""
    cleaned = location.strip().lower()
    if cleaned.endswith(" län"):
        cleaned = cleaned[: -len(" län")].strip()
        if cleaned.endswith("s") and cleaned[:-1] in _COUNTY_CODE_LOWER:
            cleaned = cleaned[:-1]
    return _COUNTY_CODE_LOWER.get(cleaned)
