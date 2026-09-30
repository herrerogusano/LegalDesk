from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.identity import create_cognito_logout_url


class LogoutUrlSecurityTests(unittest.TestCase):
    def test_provider_url_uses_same_origin_fixed_path_and_exact_return_uri(self) -> None:
        value = create_cognito_logout_url(
            "https://pool.auth.eu-west-1.amazoncognito.com/oauth2/authorize",
            client_id="public-client",
            logout_uri="https://public.example/logout",
        )
        parsed = urlsplit(value)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ("https", "pool.auth.eu-west-1.amazoncognito.com", "/logout"))
        self.assertEqual(parse_qs(parsed.query), {"client_id": ["public-client"], "logout_uri": ["https://public.example/logout"]})
        self.assertNotIn("token", value.lower())
        self.assertNotIn("code", value.lower())
        self.assertNotIn("state", value.lower())

    def test_provider_url_rejects_untrusted_endpoint_or_return_uri(self) -> None:
        invalid_values = (
            ("http://pool.example/authorize", "https://public.example/logout"),
            ("https://pool.example/authorize?next=https://evil.example", "https://public.example/logout"),
            ("https://pool.example/authorize", "https://evil.example/other"),
            ("https://pool.example/authorize", "https://public.example/logout?next=https://evil.example"),
            ("https://user:password@pool.example/authorize", "https://public.example/logout"),
        )
        for endpoint, logout_uri in invalid_values:
            with self.subTest(endpoint=endpoint, logout_uri=logout_uri), self.assertRaises(ValueError):
                create_cognito_logout_url(endpoint, client_id="public-client", logout_uri=logout_uri)


if __name__ == "__main__":
    unittest.main()
