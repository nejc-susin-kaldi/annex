"""The gateway token (D80): HS256, expires, signature checks out. Stdlib unittest, runs on the Mac."""
import base64
import hashlib
import hmac
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import annex_vm as vm  # noqa: E402


def decode(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


class Jwt(unittest.TestCase):
    def test_claims_carry_an_expiry_one_year_out(self):
        h, p, _ = vm.jwt("app_x", "x", key="k", now=1_000_000).split(".")
        self.assertEqual(decode(h), {"alg": "HS256", "typ": "JWT"})
        self.assertEqual(decode(p), {"role": "app_x", "app": "x", "iat": 1_000_000, "exp": 1_000_000 + 365 * 24 * 3600})

    def test_signature_is_hmac_sha256_of_header_and_payload(self):
        h, p, s = vm.jwt("app_x", "x", key="k", now=5).split(".")
        want = base64.urlsafe_b64encode(hmac.new(b"k", f"{h}.{p}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        self.assertEqual(s, want)

    def test_a_later_token_differs(self):
        self.assertNotEqual(vm.jwt("app_x", "x", key="k", now=1), vm.jwt("app_x", "x", key="k", now=2))


if __name__ == "__main__":
    unittest.main()
