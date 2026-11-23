import json
import os
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

import server


class SharedBooksApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("BOOKLOOP_ANALYTICS_DB")
        os.environ["BOOKLOOP_ANALYTICS_DB"] = os.path.join(self.temp_dir.name, "bookloop.sqlite3")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.BookLoopHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        if self.old_db is None:
            os.environ.pop("BOOKLOOP_ANALYTICS_DB", None)
        else:
            os.environ["BOOKLOOP_ANALYTICS_DB"] = self.old_db
        self.temp_dir.cleanup()

    def request(self, path, method="GET", body=None):
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_published_book_is_saved_and_visible_to_catalog_readers(self):
        book = {
            "isbn": "978-957-1383125",
            "title": "共享書籍測試",
            "author": "測試作者",
            "publisher": "測試出版社",
            "points": 98,
            "cycles": 1,
            "condition": "good",
            "location": "台北市測試站",
            "region": "north",
            "language": "zh",
            "sellerId": "BL-demo-account-1",
            "sellerName": "測試書友",
        }
        status, created = self.request("/api/books", "POST", book)
        self.assertEqual(status, 201)
        self.assertNotEqual(created["book"]["isbn"], book["isbn"])
        self.assertEqual(created["book"]["media"], [])

        status, catalog = self.request("/api/books")
        self.assertEqual(status, 200)
        self.assertEqual(len(catalog["books"]), 1)
        self.assertEqual(catalog["books"][0]["id"], created["book"]["id"])
        self.assertEqual(catalog["books"][0]["isbn"], "9789571383125")

    def test_invalid_listings_are_rejected(self):
        status, response = self.request("/api/books", "POST", {
            "title": "缺少作者",
            "points": 80,
            "cycles": 0,
            "location": "測試站",
            "sellerId": "BL-demo-account-1",
        })
        self.assertEqual(status, 400)
        self.assertEqual(response["error"], "invalid_author")
        self.assertEqual(self.request("/api/books")[1]["books"], [])


if __name__ == "__main__":
    unittest.main()
