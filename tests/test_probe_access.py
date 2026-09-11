import unittest

from scripts.probe_access import CHALLENGE_MARKERS, classify

MARKER = "product-main-price-display"
REAL_PAGE = f'<html><body><div class="{MARKER}">248,90 €</div></body></html>'


class ClassifyTests(unittest.TestCase):

    def test_200_with_the_price_element_is_served(self):
        self.assertEqual(classify(200, REAL_PAGE, MARKER), "served")

    def test_403_is_blocked(self):
        self.assertEqual(classify(403, "<html>Forbidden</html>", MARKER), "blocked")

    def test_429_is_blocked(self):
        self.assertEqual(classify(429, "", MARKER), "blocked")

    def test_200_cloudflare_challenge_is_a_block_not_a_success(self):
        """The trap this probe exists to catch: a 200 that is not the page."""
        body = "<html><title>Just a moment...</title><div id='cf-challenge'></div></html>"
        self.assertEqual(classify(200, body, MARKER), "challenge")

    def test_200_without_the_price_element_is_not_the_product_page(self):
        body = "<html><body>Welcome to our shop</body></html>"
        self.assertEqual(classify(200, body, MARKER), "not_product_page")

    def test_5xx_is_error_not_blocked(self):
        self.assertEqual(classify(503, "", MARKER), "error")

    def test_challenge_detection_is_case_insensitive(self):
        body = "<html>CHECKING YOUR BROWSER before accessing</html>"
        self.assertEqual(classify(200, body, MARKER), "challenge")

    def test_real_page_that_mentions_captcha_in_a_script_is_served(self):
        """Observed 2026-09-11: the real tennis-zone.hr page ships Shopware's
        feature flags ('"captcha":true') and a captcha.js for its forms.
        The price element proves it is the real page, not an interstitial."""
        body = ('<html><script>window.features={"captcha":true};</script>'
                '<script src="/js/captcha/captcha.js"></script>'
                f'<div class="{MARKER}">248,90 €</div></html>')
        self.assertEqual(classify(200, body, MARKER), "served")

    def test_challenge_markers_include_the_known_cloudflare_strings(self):
        for m in ("cf-challenge", "just a moment", "checking your browser", "captcha"):
            self.assertIn(m, [x.lower() for x in CHALLENGE_MARKERS])


if __name__ == "__main__":
    unittest.main()
