import json
import math
import re
import sys
import threading
import time
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

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        request = urlparse(self.path)
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

        isbn = normalize_isbn(parse_qs(request.query).get("isbn", [""])[0])
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
    port = 4173
    server = ThreadingHTTPServer(("0.0.0.0", port), BookLoopHandler)
    print(f"BookLoop is serving on http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
