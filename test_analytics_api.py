import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, HTTPHandler, Request, build_opener
from http.server import ThreadingHTTPServer

import server


class AnalyticsApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("BOOKLOOP_ANALYTICS_DB")
        self.old_password = os.environ.get("BOOKLOOP_ADMIN_PASSWORD")
        os.environ["BOOKLOOP_ANALYTICS_DB"] = os.path.join(self.temp_dir.name, "analytics.sqlite3")
        os.environ["BOOKLOOP_ADMIN_PASSWORD"] = "test-admin-password-long-enough"
        with server._admin_sessions_lock:
            server._admin_sessions.clear()
        with server._admin_login_attempts_lock:
            server._admin_login_attempts.clear()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.BookLoopHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.opener = build_opener(HTTPHandler(),)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        if self.old_db is None:
            os.environ.pop("BOOKLOOP_ANALYTICS_DB", None)
        else:
            os.environ["BOOKLOOP_ANALYTICS_DB"] = self.old_db
        if self.old_password is None:
            os.environ.pop("BOOKLOOP_ADMIN_PASSWORD", None)
        else:
            os.environ["BOOKLOOP_ADMIN_PASSWORD"] = self.old_password
        self.temp_dir.cleanup()

    def request(self, path, method="GET", body=None, headers=None, opener=None):
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method,
        )
        try:
            response = (opener or self.opener).open(request)
            return response.status, response.read(), response.headers
        except HTTPError as error:
            result = error.code, error.read(), error.headers
            error.close()
            return result

    def post_event(self, event_id, client_id, event_type, payload=None):
        return self.request("/api/analytics/events", "POST", {
            "eventId": event_id,
            "clientId": client_id,
            "type": event_type,
            "payload": payload or {},
        })

    def test_analytics_requires_admin_and_export_uses_event_data(self):
        status, body, _ = self.request("/api/admin/analytics?days=all")
        self.assertEqual(status, 401)
        self.assertNotIn(b'"customers"', body)

        self.assertEqual(self.post_event("customer-event-000001", "client-browser-0001", "customer_seen")[0], 202)
        self.assertEqual(self.post_event("customer-event-000002", "client-browser-0001", "customer_seen")[0], 202)
        self.assertEqual(self.post_event(
            "book-event-00000001", "client-browser-0001", "book_listed",
            {"isbn": "978-957-13-8312-5", "region": "north", "language": "zh", "condition": "good", "points": 80},
        )[0], 202)
        self.assertEqual(self.post_event(
            "trade-event-000001", "client-browser-0001", "trade_created", {"isbn": "9789571383125", "mode": "points"},
        )[0], 202)
        self.assertEqual(self.post_event(
            "money-trade-event-0001", "client-browser-0001", "trade_created", {"isbn": "9789571383125", "mode": "money"},
        )[0], 202)
        self.assertEqual(self.post_event(
            "settled-event-000001", "client-browser-0001", "trade_settled", {"isbn": "9789571383125", "mode": "points"},
        )[0], 202)
        self.assertEqual(self.post_event(
            "money-settled-event-001", "client-browser-0001", "trade_settled", {"isbn": "9789571383125", "mode": "money"},
        )[0], 202)
        duplicate_status, duplicate_body, _ = self.post_event(
            "book-event-00000001", "client-browser-0001", "book_listed", {"points": 500},
        )
        self.assertEqual(duplicate_status, 202)
        self.assertFalse(json.loads(duplicate_body)["accepted"])

        cookie_jar = http.cookiejar.CookieJar()
        admin_opener = build_opener(HTTPHandler(),)
        admin_opener.add_handler(HTTPCookieProcessor(cookie_jar))
        status, _, headers = self.request(
            "/api/admin/login", "POST", {"password": "test-admin-password-long-enough"}, opener=admin_opener,
        )
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers.get("Set-Cookie", ""))
        self.assertIn("SameSite=Strict", headers.get("Set-Cookie", ""))

        status, body, _ = self.request("/api/admin/analytics?days=all", opener=admin_opener)
        self.assertEqual(status, 200)
        summary = json.loads(body)
        self.assertEqual(summary["customers"], 1)
        self.assertEqual(summary["booksListed"], 1)
        self.assertEqual(summary["uniqueBooks"], 1)
        self.assertEqual(summary["tradesCreated"], 2)
        self.assertEqual(summary["tradesSettled"], 2)
        self.assertEqual(summary["estimatedCo2Kg"], 2.04)
        self.assertEqual(summary["regions"], {"north": 1})
        self.assertIn("未經平台", summary["dataNote"])

        status, body, headers = self.request("/api/admin/analytics.csv?days=all", opener=admin_opener)
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers.get("Content-Type", ""))
        self.assertIn(b"9789571383125", body)
        self.assertIn(b"1.02", body)

        self.assertEqual(self.request("/api/admin/logout", "POST", {}, opener=admin_opener)[0], 200)
        self.assertEqual(self.request("/api/admin/analytics?days=all", opener=admin_opener)[0], 401)

    def test_invalid_events_and_ranges_are_rejected(self):
        status, _, _ = self.post_event("invalid-event-00001", "client-browser-0001", "trade_settled", {"mode": "not-a-mode"})
        self.assertEqual(status, 400)
        status, _, _ = self.request(
            "/api/admin/login", "POST", {"password": "test-admin-password-long-enough"},
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 400)

        cookie_jar = http.cookiejar.CookieJar()
        admin_opener = build_opener(HTTPHandler(), HTTPCookieProcessor(cookie_jar))
        self.assertEqual(self.request(
            "/api/admin/login", "POST", {"password": "test-admin-password-long-enough"}, opener=admin_opener,
        )[0], 200)
        self.assertEqual(self.request("/api/admin/analytics?days=13", opener=admin_opener)[0], 400)

    def test_bad_password_is_rejected_and_rate_limited(self):
        for _ in range(server.ADMIN_LOGIN_MAX_ATTEMPTS):
            status, body, _ = self.request("/api/admin/login", "POST", {"password": "wrong-password"})
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body)["error"], "invalid_admin_credentials")
        status, body, _ = self.request("/api/admin/login", "POST", {"password": "wrong-password"})
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"], "admin_login_rate_limited")


if __name__ == "__main__":
    unittest.main()
