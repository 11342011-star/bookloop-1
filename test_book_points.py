import unittest

import server


class BookPointEstimateTests(unittest.TestCase):
    def estimate(self, **overrides):
        values = {
            "isbn": "9789571383125",
            "title": "一般圖書",
            "author": "測試作者",
            "publisher": "測試出版社",
            "cycles": 0,
            "condition": "good",
            "media_count": 1,
            "video_count": 0,
        }
        values.update(overrides)
        return server.estimate_book_points(**values)["points"]

    def test_isbn_condition_and_circulation_change_estimate(self):
        base = self.estimate()
        self.assertEqual(self.estimate(isbn=""), base - 15)
        self.assertEqual(self.estimate(condition="excellent"), base + 5)
        self.assertEqual(self.estimate(condition="damaged"), base - 20)
        self.assertEqual(self.estimate(cycles=1), base - 4)
        self.assertEqual(self.estimate(cycles=7), base - 28)

    def test_listing_quality_and_cycle_cap_bound_estimate(self):
        without_evidence = self.estimate(media_count=0)
        with_photos = self.estimate(media_count=2)
        self.assertGreater(with_photos, without_evidence)
        self.assertEqual(self.estimate(cycles=8), self.estimate(cycles=50))
        self.assertEqual(self.estimate(
            condition="damaged",
            cycles=50,
            missing_page="yes",
            handwriting="yes",
            damage="yes",
            stains="yes",
        ), 40)


if __name__ == "__main__":
    unittest.main()
