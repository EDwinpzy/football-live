import json
import threading
import unittest
import urllib.error
import urllib.request

from tools.selfhosted.auth_service import (
    decode_access_token,
    hash_password,
    make_server,
)


class SelfHostedAuthTests(unittest.TestCase):
    def setUp(self):
        self.secret = b"test-only-signing-secret-with-more-than-32-bytes"
        salt = "7e1d96d872317d5fcab5e26ae5508871"
        self.users = {
            "svc_import": {
                "subject": "2094843486709620737",
                "salt": salt,
                "password_hash": hash_password("test-password", salt),
            }
        }
        self.server = make_server("127.0.0.1", 0, self.users, self.secret)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def post_signin(self, payload):
        request = urllib.request.Request(
            self.base_url + "/auth/v1/signin",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=3)

    def test_signin_issues_postgrest_compatible_service_token(self):
        with self.post_signin({"username": "svc_import", "password": "test-password"}) as response:
            body = json.loads(response.read())
        self.assertEqual(body["token_type"], "bearer")
        self.assertEqual(body["expires_in"], 7200)
        claims = decode_access_token(body["access_token"], self.secret)
        self.assertEqual(claims["sub"], "2094843486709620737")
        self.assertEqual(claims["role"], "authenticated")
        self.assertGreater(claims["exp"], claims["iat"])

    def test_signin_rejects_invalid_credentials_without_revealing_user_existence(self):
        responses = []
        for payload in (
            {"username": "svc_import", "password": "wrong"},
            {"username": "unknown", "password": "wrong"},
        ):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post_signin(payload)
            responses.append((error.exception.code, error.exception.read()))
        self.assertEqual(responses[0], responses[1])
        self.assertEqual(responses[0][0], 401)

    def test_token_decoder_rejects_tampering_and_expired_tokens(self):
        token = self.post_signin({"username": "svc_import", "password": "test-password"}).read()
        access_token = json.loads(token)["access_token"]
        first, middle, last = access_token.split(".")
        changed = last[:-1] + ("A" if last[-1] != "A" else "B")
        self.assertIsNone(decode_access_token(f"{first}.{middle}.{changed}", self.secret))
        self.assertIsNone(decode_access_token(access_token, self.secret, now=10**12))


if __name__ == "__main__":
    unittest.main()
