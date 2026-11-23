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
    if identifiers and not any(normalize_isbn(value) == isbn for value in identifiers):
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
