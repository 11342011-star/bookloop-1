import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.cookies import SimpleCookie
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
ATOM_NS = "http://www.w3.org/2005/Atom"
DC_NS = "http://purl.org/dc/terms"
ISBN_PATTERN = re.compile(r"^(?:\d{13}|\d{9}[\dX])$")
WIKIDATA_CACHE_SECONDS = 30 * 86400
WIKIDATA_CACHE_FILE = ROOT / "university_locations.json"
MRT_CACHE_FILE = ROOT / "mrt_station_locations.json"
_universities = []
_universities_fetched_at = 0
_universities_lock = threading.Lock()
_mrt_stations = []
_mrt_stations_fetched_at = 0
_mrt_stations_lock = threading.Lock()
_admin_sessions = {}
_admin_sessions_lock = threading.Lock()
_admin_login_attempts = {}
_admin_login_attempts_lock = threading.Lock()
ADMIN_SESSION_SECONDS = 8 * 60 * 60
ADMIN_LOGIN_WINDOW_SECONDS = 15 * 60
ADMIN_LOGIN_MAX_ATTEMPTS = 5
ESG_ESTIMATED_CO2_KG_PER_CIRCULATION = 1.02
ANALYTICS_EVENT_TYPES = {"customer_seen", "book_listed", "trade_created", "trade_settled"}
ANALYTICS_REGIONS = {"north", "central", "south", "east", "islands"}
ANALYTICS_LANGUAGES = {"zh", "en", "ja", "other"}
ANALYTICS_CONDITIONS = {"excellent", "good", "fair", "damaged"}


def analytics_database_path():
    return Path(os.environ.get("BOOKLOOP_ANALYTICS_DB", ROOT / "bookloop_analytics.sqlite3"))


def open_analytics_database():
    path = analytics_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("""
        CREATE TABLE IF NOT EXISTS analytics_events (
            event_id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS analytics_events_created_at ON analytics_events(created_at)")
    connection.execute("CREATE INDEX IF NOT EXISTS analytics_events_type_created_at ON analytics_events(event_type, created_at)")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS shared_books (
            book_id INTEGER PRIMARY KEY,
            created_at INTEGER NOT NULL,
            book_json TEXT NOT NULL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS shared_books_created_at ON shared_books(created_at)")
    return connection


def clean_shared_book(payload):
    if not isinstance(payload, dict):
        raise ValueError("invalid_book")

    def text_field(name, maximum, required=False):
        value = payload.get(name, "")
        if not isinstance(value, str):
            raise ValueError(f"invalid_{name}")
        value = value.strip()
        if len(value) > maximum or (required and not value):
            raise ValueError(f"invalid_{name}")
        return value

    title = text_field("title", 300, required=True)
    author = text_field("author", 200, required=True)
    publisher = text_field("publisher", 200)
    isbn = re.sub(r"[^0-9X]", "", text_field("isbn", 32).upper())
    if isbn and not ISBN_PATTERN.fullmatch(isbn):
        isbn = ""

    try:
        points = payload.get("points")
        cycles = payload.get("cycles", 0)
        if isinstance(points, bool) or isinstance(cycles, bool):
            raise ValueError
        points = int(points)
        cycles = int(cycles)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid_points_or_cycles") from error
    if not 40 <= points <= 180 or not 0 <= cycles < 8:
        raise ValueError("invalid_points_or_cycles")

    region = text_field("region", 16)
    language = text_field("language", 16)
    condition = text_field("condition", 16)
    if region not in {"", "north", "central", "south", "east", "islands"}:
        region = ""
    if language not in {"", "zh", "en", "ja", "other"}:
        language = ""
    if condition not in {"", "excellent", "good", "fair", "damaged"}:
        condition = ""

    seller_id = text_field("sellerId", 80, required=True)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", seller_id):
        raise ValueError("invalid_sellerId")
    seller_name = text_field("sellerName", 80) or "書友"
    location = text_field("location", 160, required=True)
    book_location = text_field("bookLocation", 200)
    try:
        listing_bonus_points = payload.get("listingBonusPoints", 0)
        if isinstance(listing_bonus_points, bool):
            raise ValueError
        listing_bonus_points = int(listing_bonus_points)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid_listing_bonus") from error
    listing_bonus_points = max(0, min(listing_bonus_points, 50))

    risk_flags = payload.get("riskFlags", [])
    if not isinstance(risk_flags, list):
        risk_flags = []
    risk_flags = [flag.strip()[:120] for flag in risk_flags if isinstance(flag, str)][:5]
    return {
        "isbn": isbn,
        "title": title,
        "author": author,
        "publisher": publisher,
        "points": points,
        "cycles": cycles,
        "condition": condition,
        "conditionNotes": text_field("conditionNotes", 500),
        "conditionChecks": payload.get("conditionChecks") if isinstance(payload.get("conditionChecks"), dict) else {},
        "location": location,
        "bookLocation": book_location,
        "region": region,
        "language": language,
        "sellerId": seller_id,
        "sellerName": seller_name,
        "listingBonusPoints": listing_bonus_points,
        "riskFlags": risk_flags,
        "media": [],
        "tone": "green",
    }


def clean_analytics_event(event):
    if not isinstance(event, dict):
        raise ValueError("invalid_event")
    event_id = event.get("eventId")
    client_id = event.get("clientId")
    event_type = event.get("type")
    if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", event_id):
        raise ValueError("invalid_event_id")
    if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", client_id):
        raise ValueError("invalid_client_id")
    if event_type not in ANALYTICS_EVENT_TYPES:
        raise ValueError("invalid_event_type")

    raw_payload = event.get("payload") or {}
    if not isinstance(raw_payload, dict):
        raise ValueError("invalid_payload")
    payload = {}
    if event_type == "book_listed":
        isbn = normalize_isbn(str(raw_payload.get("isbn", "")))[:13]
        payload = {
            "isbn": isbn if re.fullmatch(r"(?:\d{13}|\d{9}[\dX])", isbn) else "",
            "region": raw_payload.get("region") if raw_payload.get("region") in ANALYTICS_REGIONS else "",
            "language": raw_payload.get("language") if raw_payload.get("language") in ANALYTICS_LANGUAGES else "",
            "condition": raw_payload.get("condition") if raw_payload.get("condition") in ANALYTICS_CONDITIONS else "",
            "points": min(180, max(1, int(raw_payload.get("points", 1)))),
        }
    elif event_type in {"trade_created", "trade_settled"}:
        mode = raw_payload.get("mode")
        if mode not in {"swap", "points", "money"}:
            raise ValueError("invalid_trade_mode")
        isbn = normalize_isbn(str(raw_payload.get("isbn", "")))[:13]
        payload = {
            "mode": mode,
            "isbn": isbn if re.fullmatch(r"(?:\d{13}|\d{9}[\dX])", isbn) else "",
        }

    return {
        "event_id": event_id,
        "actor_id": hashlib.sha256(client_id.encode("utf-8")).hexdigest(),
        "event_type": event_type,
        "payload_json": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    }


def summarize_analytics(events):
    customers = set()
    books = []
    trades_created = 0
    settled = 0
    regions = {}
    languages = {}
    conditions = {}
    daily = {}
    for event in events:
        day = time.strftime("%Y-%m-%d", time.gmtime(event["created_at"]))
        daily.setdefault(day, {"date": day, "booksListed": 0, "tradesCreated": 0, "tradesSettled": 0})
        payload = json.loads(event["payload_json"])
        if event["event_type"] == "customer_seen":
            customers.add(event["actor_id"])
        elif event["event_type"] == "book_listed":
            books.append(payload)
            daily[day]["booksListed"] += 1
            for key, target in (("region", regions), ("language", languages), ("condition", conditions)):
                value = payload.get(key)
                if value:
                    target[value] = target.get(value, 0) + 1
        elif event["event_type"] == "trade_created":
            trades_created += 1
            daily[day]["tradesCreated"] += 1
        elif event["event_type"] == "trade_settled":
            settled += 1
            daily[day]["tradesSettled"] += 1
    return {
        "customers": len(customers),
        "booksListed": len(books),
        "uniqueBooks": len({book["isbn"] for book in books if book["isbn"]}),
        "tradesCreated": trades_created,
        "tradesSettled": settled,
        "circulations": settled,
        "estimatedCo2Kg": round(settled * ESG_ESTIMATED_CO2_KG_PER_CIRCULATION, 2),
        "regions": regions,
        "languages": languages,
        "conditions": conditions,
        "daily": sorted(daily.values(), key=lambda item: item["date"]),
    }


class GoogleBooksDetailsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.metadata = {}
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "tr" and "metadata_row" in attributes.get("class", "").split():
            self.row = []
        elif tag == "td" and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if len(self.row) > 1:
                self.metadata[self.row[0].lower()] = self.row[1]
            self.row = None


def normalize_isbn(value):
    return re.sub(r"[-\s]", "", value).upper()


def fetch_url(url):
    headers = {"User-Agent": "BookLoop/1.0 (ISBN and campus lookup)"}
    if "wikidata.org/sparql" in url or "qlever" in url:
        headers["Accept"] = "application/sparql-results+json"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=8) as response:
        return response.read()


def lookup_google_books(isbn):
    try:
        volume_data = json.loads(fetch_url(
            f"https://www.googleapis.com/books/v1/volumes?q={quote('isbn:' + isbn)}&maxResults=5"
        ))
        for volume in volume_data.get("items") or []:
            volume_info = volume.get("volumeInfo") or {}
            industry_ids = volume_info.get("industryIdentifiers") or []
            matched = any(
                normalize_isbn(item.get("identifier", "")) == isbn
                for item in industry_ids
                if item.get("identifier")
            )
            if industry_ids and not matched:
                continue
            title = (volume_info.get("title") or "").strip()
            if not title:
                continue
            return {
                "title": title,
                "author": ", ".join(volume_info.get("authors") or []),
                "publisher": (volume_info.get("publisher") or "").strip(),
                "points": 80,
            }
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        print(f"Google Books JSON lookup failed ({isbn}): {error}", file=sys.stderr)

    details = GoogleBooksDetailsParser()
    try:
        details.feed(fetch_url(
            f"https://books.google.com/books?vid=ISBN{quote(isbn)}&hl=zh-TW"
        ).decode("utf-8", "replace"))
    except (URLError, TimeoutError, OSError) as error:
        print(f"Google Books ISBN page lookup failed ({isbn}): {error}", file=sys.stderr)
    page_isbns = details.metadata.get("isbn", "")
    if any(normalize_isbn(value) == isbn for value in re.split(r"[,;]", page_isbns)):
        title = details.metadata.get("書名") or details.metadata.get("title", "")
        if title:
            return {
                "title": title,
                "author": details.metadata.get("作者") or details.metadata.get("author", ""),
                "publisher": details.metadata.get("出版者") or details.metadata.get("publisher", ""),
                "points": 80,
            }

    query = quote(f"isbn:{isbn}")
    feed = ET.fromstring(fetch_url(f"https://books.google.com/books/feeds/volumes?q={query}"))
    entry = feed.find(f"{{{ATOM_NS}}}entry")
    if entry is None:
        return None

    identifiers = [
        item.text.split(":", 1)[1]
        for item in entry.findall(f"{{{DC_NS}}}identifier")
        if item.text and item.text.startswith("ISBN:")
    ]
    if not any(normalize_isbn(value) == isbn for value in identifiers):
        return None

    title = entry.findtext(f"{{{ATOM_NS}}}title", "").strip()
    if not title:
        return None
    author = entry.findtext(f"{{{DC_NS}}}creator", "").strip()
    publisher = entry.findtext(f"{{{DC_NS}}}publisher", "").strip()
    if not author or not publisher:
        book_id = entry.findtext(f"{{{ATOM_NS}}}id", "").rsplit("/", 1)[-1]
        if book_id:
            details = GoogleBooksDetailsParser()
            try:
                details.feed(fetch_url(
                    f"https://books.google.com/books?id={quote(book_id)}&hl=zh-TW"
                ).decode("utf-8", "replace"))
            except (URLError, TimeoutError, OSError) as error:
                print(f"Google Books details lookup failed ({isbn}): {error}", file=sys.stderr)
            author = author or details.metadata.get("作者") or details.metadata.get("author", "")
            publisher = publisher or details.metadata.get("出版者") or details.metadata.get("publisher", "")
    return {"title": title, "author": author, "publisher": publisher, "points": 80}


def lookup_open_library(isbn):
    fields = quote("title,author_name,publisher")
    book = None
    try:
        data = json.loads(fetch_url(
            f"https://openlibrary.org/search.json?isbn={quote(isbn)}&limit=1&fields={fields}"
        ))
        book = (data.get("docs") or [None])[0]
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        print(f"Open Library search lookup failed ({isbn}): {error}", file=sys.stderr)

    if not book:
        try:
            edition = json.loads(fetch_url(
                f"https://openlibrary.org/isbn/{quote(isbn)}.json"
            ))
            authors = []
            for reference in edition.get("authors") or []:
                key = reference.get("key")
                if not key:
                    continue
                try:
                    author = json.loads(fetch_url(
                        f"https://openlibrary.org{quote(key)}.json"
                    ))
                    name = author.get("name") or author.get("personal_name")
                    if name:
                        authors.append(name)
                except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
                    print(f"Open Library author lookup failed ({isbn}): {error}", file=sys.stderr)
            book = {
                "title": edition.get("title"),
                "author_name": authors,
                "publisher": edition.get("publishers") or [],
            }
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            print(f"Open Library edition lookup failed ({isbn}): {error}", file=sys.stderr)

    if not book or not book.get("title"):
        return None

    authors = book.get("author_name") or []
    publishers = book.get("publisher") or []
    return {
        "title": book["title"],
        "author": ", ".join(authors),
        "publisher": publishers[0] if publishers else "",
        "points": 80,
    }


def load_universities():
    global _universities, _universities_fetched_at
    with _universities_lock:
        if not _universities and WIKIDATA_CACHE_FILE.exists():
            try:
                cached = json.loads(WIKIDATA_CACHE_FILE.read_text(encoding="utf-8"))
                _universities = cached["schools"]
                _universities_fetched_at = cached["fetchedAt"]
            except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
                print(f"University cache could not be read: {error}", file=sys.stderr)
        if _universities and time.time() - _universities_fetched_at < WIKIDATA_CACHE_SECONDS:
            return _universities

        query = """
        PREFIX wdt: <http://www.wikidata.org/prop/direct/>
        PREFIX wd: <http://www.wikidata.org/entity/>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT ?school ?coord (COALESCE(SAMPLE(?zh), SAMPLE(?en)) AS ?schoolLabel) WHERE {
          ?school wdt:P31/wdt:P279* wd:Q3918;
                  wdt:P17 wd:Q865;
                  wdt:P625 ?coord.
          FILTER NOT EXISTS { ?school wdt:P361 ?parent. }
          OPTIONAL { ?school rdfs:label ?zh. FILTER(LANG(?zh) IN ("zh-tw", "zh")) }
          OPTIONAL { ?school rdfs:label ?en. FILTER(LANG(?en) = "en") }
        }
        GROUP BY ?school ?coord
        """
        sources = (
            ("Wikidata", "https://query.wikidata.org/sparql?" + urlencode({"query": query, "format": "json"})),
            ("Wikidata mirror", "https://qlever.cs.uni-freiburg.de/api/wikidata?" + urlencode({"query": query})),
        )
        schools = []
        errors = []
        for source, url in sources:
            try:
                data = json.loads(fetch_url(url))
                for row in data["results"]["bindings"]:
                    match = re.fullmatch(r"POINT\(([-0-9.]+) ([-0-9.]+)\)", row["coord"]["value"], re.IGNORECASE)
                    if not match:
                        continue
                    longitude, latitude = map(float, match.groups())
                    schools.append({
                        "name": row["schoolLabel"]["value"],
                        "lat": latitude,
                        "lng": longitude,
                    })
                if schools:
                    break
                errors.append(f"{source}: returned no university locations")
            except (URLError, TimeoutError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
                errors.append(f"{source}: {error}")
                print(f"University lookup failed ({source}): {error}", file=sys.stderr)

        if not schools:
            if _universities:
                print(f"University refresh failed; using cached locations: {'; '.join(errors)}", file=sys.stderr)
                return _universities
            raise ValueError("; ".join(errors))
        _universities = schools
        _universities_fetched_at = time.time()
        try:
            WIKIDATA_CACHE_FILE.write_text(
                json.dumps({"fetchedAt": _universities_fetched_at, "schools": schools}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as error:
            print(f"University cache could not be saved: {error}", file=sys.stderr)
        return _universities


def load_mrt_stations():
    global _mrt_stations, _mrt_stations_fetched_at
    with _mrt_stations_lock:
        if not _mrt_stations and MRT_CACHE_FILE.exists():
            try:
                cached = json.loads(MRT_CACHE_FILE.read_text(encoding="utf-8"))
                _mrt_stations = cached["stations"]
                _mrt_stations_fetched_at = cached["fetchedAt"]
            except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
                print(f"MRT cache could not be read: {error}", file=sys.stderr)
        if _mrt_stations and time.time() - _mrt_stations_fetched_at < WIKIDATA_CACHE_SECONDS:
            return _mrt_stations

        query = """
        PREFIX wdt: <http://www.wikidata.org/prop/direct/>
        PREFIX wd: <http://www.wikidata.org/entity/>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT ?station ?coord (COALESCE(SAMPLE(?zh), SAMPLE(?en)) AS ?stationLabel) WHERE {
          ?station wdt:P31/wdt:P279* wd:Q928830;
                   wdt:P17 wd:Q865;
                   wdt:P625 ?coord.
          OPTIONAL { ?station rdfs:label ?zh. FILTER(LANG(?zh) IN ("zh-tw", "zh")) }
          OPTIONAL { ?station rdfs:label ?en. FILTER(LANG(?en) = "en") }
        }
        GROUP BY ?station ?coord
        """
        sources = (
            ("Wikidata", "https://query.wikidata.org/sparql?" + urlencode({"query": query, "format": "json"})),
            ("Wikidata mirror", "https://qlever.cs.uni-freiburg.de/api/wikidata?" + urlencode({"query": query})),
        )
        stations = []
        errors = []
        for source, url in sources:
            try:
                data = json.loads(fetch_url(url))
                for row in data["results"]["bindings"]:
                    match = re.fullmatch(r"POINT\(([-0-9.]+) ([-0-9.]+)\)", row["coord"]["value"], re.IGNORECASE)
                    if not match:
                        continue
                    longitude, latitude = map(float, match.groups())
                    stations.append({
                        "name": row["stationLabel"]["value"],
                        "lat": latitude,
                        "lng": longitude,
                    })
                if stations:
                    break
                errors.append(f"{source}: returned no MRT stations")
            except (URLError, TimeoutError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
                errors.append(f"{source}: {error}")
                print(f"MRT lookup failed ({source}): {error}", file=sys.stderr)

        if not stations:
            if _mrt_stations:
                print(f"MRT refresh failed; using cached locations: {'; '.join(errors)}", file=sys.stderr)
                return _mrt_stations
            raise ValueError("; ".join(errors))
        _mrt_stations = stations
        _mrt_stations_fetched_at = time.time()
        try:
            MRT_CACHE_FILE.write_text(
                json.dumps({"fetchedAt": _mrt_stations_fetched_at, "stations": stations}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as error:
            print(f"MRT cache could not be saved: {error}", file=sys.stderr)
        return _mrt_stations


def distance_km(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = map(math.radians, (lat1, lng1, lat2, lng2))
    delta_lat = lat2 - lat1
    delta_lng = lng2 - lng1
    arc = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    return 6371 * 2 * math.atan2(math.sqrt(arc), math.sqrt(1 - arc))


def estimate_book_points(
    isbn, title, author, publisher, cycles, condition="", media_count=0,
    video_count=0, condition_notes="", missing_page="unknown",
    handwriting="unknown", damage="unknown", stains="unknown",
):
    valid_isbn = bool(ISBN_PATTERN.fullmatch(normalize_isbn(isbn)))
    cycle_count = max(0, min(cycles, 50))
    points = 55

    if valid_isbn:
        points += 15
    if title.strip():
        points += 5
    if author.strip():
        points += 10
    if publisher.strip():
        points += 5

    textbook_keywords = ("教科", "課本", "教材", "習題", "參考書", "學習", "練習")
    if any(keyword in title for keyword in textbook_keywords):
        points += 20

    media_count = max(0, min(media_count, 5))
    video_count = max(0, min(video_count, media_count))
    if media_count == 0:
        points -= 8
    elif media_count == 1:
        points += 3
    elif media_count >= 2:
        points += 8
    if video_count:
        points += min(video_count * 3, 6)

    condition = condition.strip()
    condition_adjustments = {
        "excellent": 10,
        "good": 5,
        "fair": 0,
        "damaged": -15,
    }
    points += condition_adjustments.get(condition, 0)

    notes = condition_notes.lower()
    damage_keywords = ("破損", "撕裂", "缺頁", "水漬", "受潮", "霉", "髒污", "折角", "塗寫", "嚴重")
    evidence_keywords = ("全新", "幾乎全新", "封面完整", "無破損")
    def has_damage_evidence(text):
        for keyword in damage_keywords:
            start = 0
            while True:
                position = text.find(keyword, start)
                if position < 0:
                    break
                prefix = text[max(0, position - 2):position]
                if not any(negation in prefix for negation in ("無", "沒", "不")):
                    return True
                start = position + len(keyword)
        return False

    if has_damage_evidence(notes):
        points -= 10
        condition_note = "受損描述會降低環點"
    elif any(keyword in notes for keyword in evidence_keywords):
        points += 5
        condition_note = "完整度描述會提高可信度"
    else:
        condition_note = "未提供額外受損或完整度描述"

    check_labels = {
        "missing_page": ("缺頁", missing_page),
        "handwriting": ("筆記", handwriting),
        "damage": ("破損", damage),
        "stains": ("污漬", stains),
    }
    check_reasons = []
    for key, (label, value) in check_labels.items():
        if value not in ("yes", "no", "unknown"):
            value = "unknown"
        if value == "yes":
            penalty = 12 if key in ("missing_page", "damage") else 8
            points -= penalty
            check_reasons.append(f"{label}：有，環點 -{penalty}")
        elif value == "no":
            points += 2
            check_reasons.append(f"{label}：無，環點 +2")
        else:
            check_reasons.append(f"{label}：未確認")

    condition_labels = {
        "excellent": "近全新",
        "good": "良好",
        "fair": "普通",
        "damaged": "明顯受損",
    }
    condition_label = condition_labels.get(condition, "未選擇")

    points -= min(cycle_count * 4, 30)
    points = max(40, min(points, 180))

    reasons = [
        "ISBN 已驗證" if valid_isbn else "ISBN 未驗證",
        "書目資料完整" if title and author and publisher else "部分書目需手動補齊",
        "課外／教材類別加權" if any(keyword in title for keyword in textbook_keywords) else "一般圖書",
        f"已流轉 {cycle_count} 次，依次數調整",
        f"已附 {media_count} 個媒體證據" if media_count else "未附照片或影片，可信度較低",
        f"書況：{condition_label}",
        condition_note,
        *check_reasons,
    ]
    return {
        "points": points,
        "cycles": cycle_count,
        "reasons": reasons,
        "method": "BookLoop rules-based estimate",
    }


class BookLoopHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def allowed_cors_origin(self):
        origin = self.headers.get("Origin", "")
        allowed = {
            "http://localhost:4173",
            "http://127.0.0.1:4173",
            "https://11342011-star.github.io",
        }
        configured = os.environ.get("BOOKLOOP_CORS_ORIGINS", "")
        allowed.update(item.strip() for item in configured.split(",") if item.strip())
        return origin if origin in allowed else ""

    def send_cors_headers(self):
        origin = self.allowed_cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self, max_bytes=16_384):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("invalid_content_type")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid_content_length") from error
        if length < 1 or length > max_bytes:
            raise ValueError("invalid_request_size")
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("invalid_json") from error

    def admin_session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get("bookloop_admin_session")
        if not morsel:
            return None
        token = morsel.value
        now = time.time()
        with _admin_sessions_lock:
            for existing, expires_at in list(_admin_sessions.items()):
                if expires_at <= now:
                    del _admin_sessions[existing]
            if _admin_sessions.get(token, 0) <= now:
                return None
        return token

    def require_admin(self):
        if self.admin_session_token():
            return True
        self.send_json(401, {"error": "admin_auth_required"})
        return False

    def analytics_range(self):
        requested = parse_qs(urlparse(self.path).query).get("days", ["30"])[0]
        if requested == "all":
            return 0
        try:
            days = int(requested)
        except ValueError as error:
            raise ValueError("invalid_range") from error
        if days not in {7, 30, 90, 365, 0}:
            raise ValueError("invalid_range")
        return days

    def analytics_events(self, days):
        connection = open_analytics_database()
        try:
            if days:
                cutoff = int(time.time()) - days * 24 * 60 * 60
                rows = connection.execute(
                    "SELECT actor_id, event_type, created_at, payload_json FROM analytics_events WHERE created_at >= ? ORDER BY created_at",
                    (cutoff,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT actor_id, event_type, created_at, payload_json FROM analytics_events ORDER BY created_at"
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def do_OPTIONS(self):
        if not self.allowed_cors_origin():
            self.send_json(403, {"error": "cors_origin_not_allowed"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.allowed_cors_origin())
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        request = urlparse(self.path)
        if request.path == "/api/books":
            try:
                book = clean_shared_book(self.read_json_body())
            except (ValueError, TypeError) as error:
                return self.send_json(400, {"error": str(error) or "invalid_book"})
            created_at = int(time.time())
            connection = open_analytics_database()
            try:
                for _ in range(3):
                    book["id"] = secrets.randbelow(8_000_000_000_000_000) + 1
                    book["createdAt"] = created_at
                    try:
                        connection.execute(
                            "INSERT INTO shared_books(book_id, created_at, book_json) VALUES (?, ?, ?)",
                            (book["id"], created_at, json.dumps(book, ensure_ascii=False)),
                        )
                        break
                    except sqlite3.IntegrityError:
                        continue
                else:
                    return self.send_json(503, {"error": "shared_book_id_unavailable"})
                connection.commit()
                return self.send_json(201, {"book": book})
            except sqlite3.Error as error:
                connection.rollback()
                print(f"Shared book could not be saved: {error}", file=sys.stderr)
                return self.send_json(500, {"error": "shared_book_unavailable"})
            finally:
                connection.close()

        if request.path == "/api/analytics/events":
            try:
                event = clean_analytics_event(self.read_json_body())
            except (ValueError, TypeError) as error:
                return self.send_json(400, {"error": str(error) or "invalid_event"})
            connection = open_analytics_database()
            try:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO analytics_events(event_id, actor_id, event_type, created_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                    (event["event_id"], event["actor_id"], event["event_type"], int(time.time()), event["payload_json"]),
                )
                connection.commit()
                return self.send_json(202, {"accepted": cursor.rowcount == 1})
            finally:
                connection.close()

        if request.path == "/api/admin/login":
            password = os.environ.get("BOOKLOOP_ADMIN_PASSWORD", "")
            demo_mode = os.environ.get("BOOKLOOP_ADMIN_DEMO_MODE", "0") == "1"
            if len(password) < 16 and not demo_mode:
                return self.send_json(503, {"error": "admin_password_not_configured"})
            client_ip = self.client_address[0]
            now = time.time()
            with _admin_login_attempts_lock:
                attempts = [at for at in _admin_login_attempts.get(client_ip, []) if now - at < ADMIN_LOGIN_WINDOW_SECONDS]
                if len(attempts) >= ADMIN_LOGIN_MAX_ATTEMPTS:
                    _admin_login_attempts[client_ip] = attempts
                    return self.send_json(429, {"error": "admin_login_rate_limited"})
                attempts.append(now)
                _admin_login_attempts[client_ip] = attempts
            try:
                body = self.read_json_body(4_096)
            except ValueError as error:
                return self.send_json(400, {"error": str(error)})
            submitted = body.get("password", "") if isinstance(body, dict) else ""
            password_matches = isinstance(submitted, str) and len(password) >= 16 and hmac.compare_digest(
                submitted.encode("utf-8"), password.encode("utf-8")
            )
            demo_matches = demo_mode and isinstance(submitted, str) and hmac.compare_digest(submitted, "0000")
            if not password_matches and not demo_matches:
                return self.send_json(401, {"error": "invalid_admin_credentials"})
            token = secrets.token_urlsafe(32)
            with _admin_sessions_lock:
                _admin_sessions[token] = now + ADMIN_SESSION_SECONDS
            with _admin_login_attempts_lock:
                _admin_login_attempts.pop(client_ip, None)
            cross_origin = bool(self.allowed_cors_origin())
            cookie = (
                f"bookloop_admin_session={token}; HttpOnly; "
                f"SameSite={'None' if cross_origin else 'Strict'}; "
                f"Path=/; Max-Age={ADMIN_SESSION_SECONDS}"
            )
            forwarded_proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
            if forwarded_proto == "https" or cross_origin:
                cookie += "; Secure"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_cors_headers()
            self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(json.dumps({"authenticated": True}).encode("utf-8"))
            return

        if request.path == "/api/admin/logout":
            token = self.admin_session_token()
            if token:
                with _admin_sessions_lock:
                    _admin_sessions.pop(token, None)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_cors_headers()
            logout_same_site = "None" if self.allowed_cors_origin() else "Strict"
            self.send_header("Set-Cookie", f"bookloop_admin_session=; HttpOnly; SameSite={logout_same_site}; Path=/; Max-Age=0; Secure")
            self.end_headers()
            self.wfile.write(b'{"authenticated":false}')
            return

        return self.send_json(404, {"error": "not_found"})

    def do_GET(self):
        request = urlparse(self.path)
        if request.path == "/api/admin/session":
            if not self.require_admin():
                return
            return self.send_json(200, {"authenticated": True})
        if request.path == "/api/admin/analytics":
            if not self.require_admin():
                return
            try:
                days = self.analytics_range()
                events = self.analytics_events(days)
            except ValueError as error:
                return self.send_json(400, {"error": str(error)})
            summary = summarize_analytics(events)
            summary["rangeDays"] = days
            summary["dataNote"] = "統計來自前端原型回報；登入、訂單履約與 ESG 效益未經平台或物流方驗證。減碳數值為每次流轉 1.02 kg CO₂e 的原型估算。"
            return self.send_json(200, summary)
        if request.path == "/api/admin/analytics.csv":
            if not self.require_admin():
                return
            try:
                days = self.analytics_range()
                events = self.analytics_events(days)
            except ValueError as error:
                return self.send_json(400, {"error": str(error)})
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(("timestamp_utc", "event_type", "anonymous_customer_ref", "isbn", "region", "language", "condition", "points", "trade_mode", "estimated_co2_kg"))
            for event in events:
                payload = json.loads(event["payload_json"])
                writer.writerow((
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(event["created_at"])),
                    event["event_type"],
                    event["actor_id"],
                    payload.get("isbn", ""),
                    payload.get("region", ""),
                    payload.get("language", ""),
                    payload.get("condition", ""),
                    payload.get("points", ""),
                    payload.get("mode", ""),
                    ESG_ESTIMATED_CO2_KG_PER_CIRCULATION if event["event_type"] == "trade_settled" else "",
                ))
            body = ("\ufeff" + output.getvalue()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", 'attachment; filename="bookloop-platform-analytics.csv"')
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(body)
            return
        if request.path == "/api/points":
            query = parse_qs(request.query)
            isbn = normalize_isbn(query.get("isbn", [""])[0])
            title = query.get("title", [""])[0].strip()
            author = query.get("author", [""])[0].strip()
            publisher = query.get("publisher", [""])[0].strip()
            try:
                cycles = int(query.get("cycles", ["0"])[0])
            except (TypeError, ValueError):
                cycles = 0
            try:
                media_count = int(query.get("mediaCount", ["0"])[0])
                video_count = int(query.get("videoCount", ["0"])[0])
            except (TypeError, ValueError):
                media_count = 0
                video_count = 0
            condition = query.get("condition", [""])[0]
            condition_notes = query.get("conditionNotes", [""])[0]
            missing_page = query.get("missingPage", ["unknown"])[0]
            handwriting = query.get("handwriting", ["unknown"])[0]
            damage = query.get("damage", ["unknown"])[0]
            stains = query.get("stains", ["unknown"])[0]
            return self.send_json(200, estimate_book_points(
                isbn, title, author, publisher, cycles,
                condition, media_count, video_count, condition_notes,
                missing_page, handwriting, damage, stains,
            ))
        if request.path == "/api/stations":
            return self.get_stations(parse_qs(request.query))
        if request.path != "/api/books":
            return super().do_GET()

        query = parse_qs(request.query)
        isbn = normalize_isbn(query.get("isbn", [""])[0])
        if not isbn:
            connection = open_analytics_database()
            try:
                rows = connection.execute(
                    "SELECT book_json FROM shared_books ORDER BY created_at DESC, book_id DESC"
                ).fetchall()
                return self.send_json(200, {"books": [json.loads(row["book_json"]) for row in rows]})
            except (sqlite3.Error, json.JSONDecodeError) as error:
                print(f"Shared books could not be loaded: {error}", file=sys.stderr)
                return self.send_json(500, {"error": "shared_books_unavailable"})
            finally:
                connection.close()

        if not ISBN_PATTERN.fullmatch(isbn):
            return self.send_json(400, {"error": "invalid_isbn"})

        errors = []
        result = None
        for source, lookup in (
            ("Google Books", lookup_google_books),
            ("Open Library", lookup_open_library),
        ):
            try:
                book = lookup(isbn)
            except (URLError, TimeoutError, OSError, ET.ParseError, json.JSONDecodeError) as error:
                errors.append(f"{source}: {error}")
                print(f"ISBN lookup failed ({source}, {isbn}): {error}", file=sys.stderr)
                continue
            if book:
                if result is None:
                    result = book
                else:
                    for field in ("title", "author", "publisher"):
                        if not result.get(field) and book.get(field):
                            result[field] = book[field]
                if result.get("author") and result.get("publisher"):
                    break

        if result:
            return self.send_json(200, result)

        if errors:
            return self.send_json(502, {"error": "metadata_sources_unavailable"})
        return self.send_json(404, {"error": "book_not_found"})

    def get_stations(self, query):
        try:
            latitude = float(query.get("lat", [""])[0])
            longitude = float(query.get("lng", [""])[0])
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError("Coordinates are out of range")
            schools = load_universities()
            mrt_stations = load_mrt_stations()
        except (ValueError, KeyError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            print(f"Station lookup failed: {error}", file=sys.stderr)
            return self.send_json(502, {"error": "station_locations_unavailable"})

        all_stations = [
            {"name": school["name"], "lat": school["lat"], "lng": school["lng"], "type": "school"}
            for school in schools
        ] + [
            {"name": station["name"], "lat": station["lat"], "lng": station["lng"], "type": "mrt"}
            for station in mrt_stations
        ]
        nearest = sorted(
            (
                {
                    "name": station["name"],
                    "type": station["type"],
                    "distanceKm": round(distance_km(latitude, longitude, station["lat"], station["lng"]), 1),
                }
                for station in all_stations
            ),
            key=lambda school: school["distanceKm"],
        )
        return self.send_json(200, {
            "universityCount": len(schools),
            "mrtCount": len(mrt_stations),
            "stations": nearest[:10],
        })


def main():
    port = int(os.environ.get("PORT", "4173"))
    server = ThreadingHTTPServer(("0.0.0.0", port), BookLoopHandler)
    print(f"BookLoop is serving on http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
