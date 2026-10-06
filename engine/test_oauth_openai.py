#!/usr/bin/env python3
"""Tests for the Sign in with ChatGPT path (oauth_openai.py + the OAuth bits of llm_author.py).

Stdlib unittest only, no network, no browser. Covers:
  - PKCE: code_challenge == base64url(sha256(verifier)) with padding stripped.
  - JWT decode: a sample unsigned id_token yields the right account_id + plan.
  - Request-body builder: text-only vs image shape (store:false, include, input_image only w/ image).
  - SSE aggregation: a sample event-stream returns the concatenated assistant text.
  - Settings round-trip: save/load the oauth bundle; public_settings hides secrets; file mode 600.

The LIVE browser sign-in and a real inference call to the ChatGPT backend CANNOT be exercised
here (no browser / no hardware) — that is the outstanding on-hardware verification.
"""
import base64
import hashlib
import json
import os
import stat
import tempfile
import unittest

import oauth_openai
import llm_author


def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_id_token(auth_claim: dict) -> str:
    """Build an UNSIGNED sample JWT (header.payload.signature) carrying the OpenAI auth claim."""
    header = _b64url_nopad(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url_nopad(json.dumps({
        "sub": "user-123",
        "https://api.openai.com/auth": auth_claim,
    }).encode())
    return f"{header}.{payload}.dummy-signature"


class PkceTest(unittest.TestCase):
    def test_challenge_is_b64url_sha256_of_verifier(self):
        url, verifier, state = oauth_openai.build_authorize_url()
        # recompute the challenge from the verifier and confirm it appears in the URL
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        self.assertNotIn("=", expected)  # padding stripped
        self.assertIn(f"code_challenge={expected}", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn(f"state={state}", url)
        # all mandatory extra params present
        for frag in ("response_type=code", "id_token_add_organizations=true",
                     "codex_cli_simplified_flow=true", "originator=codex_cli_rs",
                     "client_id=app_EMoamEEZ73f0CkXaXp7hrann"):
            self.assertIn(frag, url)

    def test_verifier_is_url_safe_and_unpadded(self):
        _, verifier, _ = oauth_openai.build_authorize_url()
        self.assertNotIn("=", verifier)
        self.assertNotIn("+", verifier)
        self.assertNotIn("/", verifier)


class JwtDecodeTest(unittest.TestCase):
    def test_account_id_and_plan_extracted(self):
        tok = _make_id_token({"chatgpt_account_id": "acct_ABC123", "chatgpt_plan_type": "pro"})
        account_id, plan, residency = oauth_openai._account_from_id_token(tok)
        self.assertEqual(account_id, "acct_ABC123")
        self.assertEqual(plan, "pro")
        self.assertIsNone(residency)

    def test_residency_captured_when_present(self):
        tok = _make_id_token({"chatgpt_account_id": "acct_X", "chatgpt_plan_type": "plus",
                              "chatgpt_data_residency": "eu"})
        account_id, plan, residency = oauth_openai._account_from_id_token(tok)
        self.assertEqual(residency, "eu")

    def test_padding_fix_on_payload(self):
        # exercise a payload whose base64 length needs padding restored
        tok = _make_id_token({"chatgpt_account_id": "a", "chatgpt_plan_type": "free"})
        claims = oauth_openai._jwt_claims(tok)
        self.assertEqual(claims["https://api.openai.com/auth"]["chatgpt_account_id"], "a")


class RequestBodyTest(unittest.TestCase):
    def test_text_only_shape(self):
        body = llm_author._oauth_responses_body("gpt-5.1", "SYS", "run 5k", None)
        self.assertEqual(body["model"], "gpt-5.1")
        self.assertEqual(body["instructions"], "SYS")
        self.assertIs(body["store"], False)                       # store:false REQUIRED
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])  # REQUIRED w/ store:false
        self.assertIs(body["stream"], True)
        content = body["input"][0]["content"]
        self.assertEqual(body["input"][0]["type"], "message")
        self.assertEqual(body["input"][0]["role"], "user")
        self.assertEqual(content[0], {"type": "input_text", "text": "run 5k"})
        # no image block when no image
        self.assertFalse(any(c.get("type") == "input_image" for c in content))

    def test_image_shape(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\nFAKE")
            path = f.name
        try:
            body = llm_author._oauth_responses_body("gpt-5.1", "SYS", "this plan", path)
            content = body["input"][0]["content"]
            img = [c for c in content if c.get("type") == "input_image"]
            self.assertEqual(len(img), 1)
            block = img[0]
            self.assertEqual(block["detail"], "high")
            # image_url is a data-URL STRING, not a nested {url:...} object
            self.assertIsInstance(block["image_url"], str)
            self.assertTrue(block["image_url"].startswith("data:image/png;base64,"))
        finally:
            os.unlink(path)


class SseAggregationTest(unittest.TestCase):
    def test_deltas_concatenated(self):
        stream = (
            "event: response.output_text.delta\n"
            'data: {"type":"response.output_text.delta","delta":"Hello "}\n\n'
            'data: {"type":"response.output_text.delta","delta":"world"}\n\n'
            'data: {"type":"response.completed","response":{"output_text":"IGNORED"}}\n\n'
            "data: [DONE]\n\n"
        )
        self.assertEqual(llm_author._aggregate_sse_text(stream), "Hello world")

    def test_fallback_to_completed_output_text(self):
        stream = (
            'data: {"type":"response.created","response":{}}\n\n'
            'data: {"type":"response.completed","response":{"output_text":"final text"}}\n\n'
            "data: [DONE]\n\n"
        )
        self.assertEqual(llm_author._aggregate_sse_text(stream), "final text")

    def test_fallback_to_nested_output_content(self):
        completed = {
            "type": "response.completed",
            "response": {"output": [
                {"content": [{"type": "output_text", "text": "A"}, {"type": "output_text", "text": "B"}]}
            ]},
        }
        stream = f'data: {json.dumps(completed)}\n\n'
        self.assertEqual(llm_author._aggregate_sse_text(stream), "AB")


class SettingsRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings_path = os.path.join(self.tmp, ".app_settings.json")
        # point BOTH modules' settings file at the temp path
        self._old_oauth = oauth_openai.SETTINGS_FILE
        self._old_llm = llm_author.SETTINGS_FILE
        oauth_openai.SETTINGS_FILE = self.settings_path
        llm_author.SETTINGS_FILE = self.settings_path

    def tearDown(self):
        oauth_openai.SETTINGS_FILE = self._old_oauth
        llm_author.SETTINGS_FILE = self._old_llm

    def test_bundle_store_load_and_status(self):
        bundle = {
            "access_token": "secret-access", "refresh_token": "secret-refresh",
            "id_token": "jwt", "account_id": "acct_9", "plan": "plus",
            "residency": None, "expires_at": 9999999999,
        }
        oauth_openai._store_bundle(bundle)
        loaded = oauth_openai._load_bundle()
        self.assertEqual(loaded["access_token"], "secret-access")
        st = oauth_openai.status()
        self.assertTrue(st["connected"])
        self.assertEqual(st["plan"], "plus")
        self.assertEqual(st["account_id"], "acct_9")
        # status() must NOT leak any token
        self.assertNotIn("access_token", st)
        self.assertNotIn("refresh_token", st)

    def test_file_mode_600(self):
        oauth_openai._store_bundle({"access_token": "x", "expires_at": 1})
        mode = stat.S_IMODE(os.stat(self.settings_path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_public_settings_hides_secrets_and_exposes_oauth_state(self):
        # a BYO key + an oauth bundle both present
        llm_author.save_settings(provider="openai", key="sk-secret", openai_auth="oauth")
        oauth_openai._store_bundle({
            "access_token": "tok", "refresh_token": "rt", "id_token": "j",
            "account_id": "acct_1", "plan": "pro", "residency": None, "expires_at": 9999999999,
        })
        pub = llm_author.public_settings()
        # secrets never surface
        self.assertNotIn("key", pub)
        self.assertNotIn("access_token", json.dumps(pub))
        self.assertNotIn("sk-secret", json.dumps(pub))
        # connected-state + plan do surface
        self.assertTrue(pub["has_key"])
        self.assertEqual(pub["openai_auth"], "oauth")
        self.assertTrue(pub["openai_oauth_connected"])
        self.assertEqual(pub["openai_oauth_plan"], "pro")

    def test_logout_clears_bundle_but_keeps_key(self):
        llm_author.save_settings(provider="openai", key="sk-keepme")
        oauth_openai._store_bundle({"access_token": "tok", "account_id": "a", "expires_at": 9e9})
        oauth_openai.logout()
        self.assertIsNone(oauth_openai._load_bundle())
        # the BYO key survives an oauth logout (fallback stays intact)
        self.assertEqual(llm_author.load_settings()["key"], "sk-keepme")

    def test_valid_access_token_refreshes_near_expiry(self):
        import time
        called = {}

        def fake_refresh(url, form):
            called["form"] = form
            return {"access_token": "fresh-tok", "refresh_token": "fresh-rt", "expires_in": 3600}

        old_post = oauth_openai._post_form
        oauth_openai._post_form = fake_refresh
        try:
            # a token already expired -> must refresh
            oauth_openai._store_bundle({
                "access_token": "stale", "refresh_token": "rt-old",
                "id_token": "", "account_id": "acct", "plan": "plus",
                "residency": None, "expires_at": int(time.time()) - 10,
            })
            tok = oauth_openai.valid_access_token()
            self.assertEqual(tok, "fresh-tok")
            self.assertEqual(called["form"]["grant_type"], "refresh_token")
            self.assertEqual(called["form"]["refresh_token"], "rt-old")
            # the fresh bundle was persisted, account_id preserved from the previous bundle
            self.assertEqual(oauth_openai._load_bundle()["account_id"], "acct")
        finally:
            oauth_openai._post_form = old_post

    def test_valid_access_token_none_when_signed_out(self):
        self.assertIsNone(oauth_openai.valid_access_token())


class AuthorRoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.settings_path = os.path.join(self.tmp, ".app_settings.json")
        self._old_oauth_file = oauth_openai.SETTINGS_FILE
        self._old_llm_file = llm_author.SETTINGS_FILE
        self._old_status = oauth_openai.status
        self._old_oauth_call = llm_author._call_openai_oauth
        self._old_openai_call = llm_author._call_openai
        self._old_model_listers = dict(llm_author._MODEL_LISTERS)
        oauth_openai.SETTINGS_FILE = self.settings_path
        llm_author.SETTINGS_FILE = self.settings_path

    def tearDown(self):
        oauth_openai.SETTINGS_FILE = self._old_oauth_file
        llm_author.SETTINGS_FILE = self._old_llm_file
        oauth_openai.status = self._old_status
        llm_author._call_openai_oauth = self._old_oauth_call
        llm_author._call_openai = self._old_openai_call
        llm_author._MODEL_LISTERS.clear()
        llm_author._MODEL_LISTERS.update(self._old_model_listers)

    def test_keys_are_stored_per_provider(self):
        llm_author.save_settings(provider="gemini", key="AIza-gemini")
        llm_author.save_settings(provider="openai", key="sk-openai")

        gemini = llm_author.load_settings_for_provider("gemini")
        openai = llm_author.load_settings_for_provider("openai")
        pub = llm_author.public_settings(openai)

        self.assertEqual(gemini["key"], "AIza-gemini")
        self.assertEqual(openai["key"], "sk-openai")
        self.assertTrue(pub["has_key_by_provider"]["gemini"])
        self.assertTrue(pub["has_key_by_provider"]["openai"])
        self.assertFalse(pub["has_key_by_provider"]["anthropic"])

    def test_legacy_key_migrates_to_current_provider_only(self):
        with open(self.settings_path, "w") as f:
            json.dump({"provider": "gemini", "model": "gemini-2.5-flash", "key": "AIza-legacy"}, f)

        gemini = llm_author.load_settings()
        openai = llm_author.load_settings_for_provider("openai")

        self.assertEqual(gemini["key"], "AIza-legacy")
        self.assertEqual(openai["key"], "")

    def test_byo_route_never_uses_another_provider_key(self):
        llm_author.save_settings(provider="gemini", key="AIza-gemini")
        llm_author.save_settings(provider="openai", openai_auth="key")

        called = []

        def fake_openai(*args):
            called.append(args)
            return '{"name":"bad","sport":"running","steps":[]}'

        llm_author._call_openai = fake_openai
        with self.assertRaisesRegex(RuntimeError, "no API key set"):
            llm_author.author("run easy")
        self.assertEqual(called, [])

    def test_chatgpt_subscription_model_options_are_hardcoded(self):
        opts = llm_author.model_options(provider="openai", auth="oauth")
        self.assertEqual(opts["models"], ["gpt-5.5", "gpt-5.4"])
        self.assertEqual(opts["default_model"], "gpt-5.5")

    def test_model_options_use_selected_provider_key(self):
        llm_author.save_settings(provider="gemini", key="AIza-gemini")
        llm_author.save_settings(provider="openai", key="sk-openai")
        calls = []

        def openai_lister(key):
            calls.append(("openai", key))
            return ["gpt-dynamic"]

        def gemini_lister(key):
            calls.append(("gemini", key))
            return ["gemini-dynamic"]

        llm_author._MODEL_LISTERS["openai"] = openai_lister
        llm_author._MODEL_LISTERS["gemini"] = gemini_lister

        opts = llm_author.model_options(provider="openai", auth="key")

        self.assertEqual(opts["models"], ["gpt-dynamic"])
        self.assertEqual(opts["source"], "provider")
        self.assertEqual(calls, [("openai", "sk-openai")])

    def test_oauth_not_supported_retries_gpt_54_once(self):
        oauth_openai.status = lambda: {"connected": True}
        calls = []

        def fake_oauth(model, prompt, user_text, image_path):
            calls.append(model)
            if model == "gpt-5.5":
                raise RuntimeError('400 from ChatGPT: "gpt-5.5" is not supported with this ChatGPT account')
            return '{"name":"fallback","sport":"running","steps":[]}'

        llm_author._call_openai_oauth = fake_oauth
        notes = []
        spec = llm_author.author(
            "run easy",
            settings={"provider": "openai", "openai_auth": "oauth",
                      "openai_oauth_model": "gpt-5.5", "model": "gpt-5.5", "key": ""},
            notes=notes,
        )

        self.assertEqual(calls, ["gpt-5.5", "gpt-5.4"])
        self.assertEqual(spec["name"], "fallback")
        self.assertIn("gpt-5.4", " ".join(notes))

    def test_oauth_failure_raises_chatgpt_error_without_cross_key_fallback(self):
        llm_author.save_settings(provider="gemini", key="AIza-gemini")
        llm_author.save_settings(provider="openai", openai_auth="oauth")
        oauth_openai.status = lambda: {"connected": True}
        llm_author._call_openai_oauth = lambda *a: (_ for _ in ()).throw(
            RuntimeError("401 from ChatGPT: token blocked"))

        def fail_if_used(*args):
            raise AssertionError("OpenAI API fallback should not run without an OpenAI key")

        llm_author._call_openai = fail_if_used
        with self.assertRaisesRegex(RuntimeError, "401 from ChatGPT: token blocked") as ctx:
            llm_author.author("run easy")
        self.assertNotIn("API key", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
