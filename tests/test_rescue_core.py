import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rescue_core

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rescue_core import (
    CredentialCommitError,
    CredentialError,
    CredentialRecoveryError,
    import_cpa_bundle,
    parse_api_credential,
    parse_codex_credential,
    install_cpa_credential,
)


class ApiCredentialTests(unittest.TestCase):
    def test_parses_valid_gemini_payload_without_echoing_secret(self):
        raw = b'{"provider":"gemini","api_key":"secret-value","label":"telegram-rescue"}'
        parsed = parse_api_credential(raw)
        self.assertEqual(parsed.provider, "gemini")
        self.assertEqual(parsed.api_key, "secret-value")
        self.assertEqual(parsed.label, "telegram-rescue")
        self.assertNotIn("secret-value", repr(parsed))

    def test_rejects_unsupported_provider_and_control_characters(self):
        with self.assertRaisesRegex(CredentialError, "不支持"):
            parse_api_credential(b'{"provider":"evil","api_key":"abc"}')
        with self.assertRaisesRegex(CredentialError, "换行"):
            parse_api_credential(b'{"provider":"gemini","api_key":"abc\\ndef"}')


class CodexCredentialTests(unittest.TestCase):
    def test_accepts_codex_cli_shape_and_keeps_tokens_out_of_repr(self):
        raw = (
            b'{"tokens":{"access_token":"access-secret",'
            b'"refresh_token":"refresh-secret","account_id":"acct"},'
            b'"last_refresh":"2026-07-15T00:00:00Z"}'
        )
        parsed = parse_codex_credential(raw)
        self.assertEqual(parsed.tokens["access_token"], "access-secret")
        self.assertEqual(parsed.tokens["refresh_token"], "refresh-secret")
        self.assertEqual(parsed.last_refresh, "2026-07-15T00:00:00Z")
        self.assertNotIn("access-secret", repr(parsed))
        self.assertNotIn("refresh-secret", repr(parsed))

    def test_accepts_cpa_c2api_access_only_shape_as_non_refreshable(self):
        parsed = parse_codex_credential(
            b'{"type":"codex","access_token":"access-only",'
            b'"session_token":"ignored-secret","account_id":"acct",'
            b'"expired":"2026-10-13T10:50:14Z"}'
        )
        self.assertEqual(parsed.tokens["access_token"], "access-only")
        self.assertNotIn("session_token", parsed.tokens)
        self.assertFalse(parsed.refreshable)
        self.assertEqual(parsed.expires_at, "2026-10-13T10:50:14Z")
        self.assertNotIn("ignored-secret", repr(parsed))

    def test_accepts_empty_optional_fields_and_omits_them(self):
        parsed = parse_codex_credential(
            b'{"type":"codex","access_token":"access-only",'
            b'"refresh_token":"","account_id":"","id_token":""}'
        )
        self.assertEqual(parsed.tokens, {"access_token": "access-only"})
        self.assertFalse(parsed.refreshable)

    def test_installs_minimal_cpa_auth_file_atomically_with_mode_600(self):
        raw = (
            b'{"type":"codex","access_token":"access-secret",'
            b'"session_token":"must-not-be-copied","account_id":"acct",'
            b'"email":"person@example.com","expired":"2026-10-13T10:50:14Z"}'
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = install_cpa_credential(raw, Path(tmp))
            self.assertTrue(result.path.is_file())
            self.assertEqual(result.path.parent, Path(tmp))
            self.assertEqual(os.stat(result.path).st_mode & 0o777, 0o600)
            stored = json.loads(result.path.read_text())
            self.assertEqual(stored["type"], "codex")
            self.assertEqual(stored["access_token"], "access-secret")
            self.assertNotIn("session_token", stored)
            self.assertNotIn("email", stored)
            self.assertFalse(result.refreshable)

    def test_standalone_bundle_imports_sub2_accounts_without_pii(self):
        raw = json.dumps(
            {
                "exported_at": "2026-07-17T00:00:00Z",
                "accounts": [
                    {
                        "platform": "openai",
                        "type": "oauth",
                        "name": "private-name",
                        "extra": {"email": "private@example.com"},
                        "credentials": {
                            "access_token": "access-one",
                            "id_token": "id-one",
                            "refresh_token": "refresh-one",
                            "account_id": "acct-one",
                            "session_token": "must-not-persist",
                        },
                    },
                    {
                        "platform": "openai",
                        "type": "oauth",
                        "credentials": {
                            "access_token": "access-two",
                            "id_token": "id-two",
                            "account_id": "acct-two",
                        },
                    },
                ],
            }
        ).encode()
        with tempfile.TemporaryDirectory() as tmp:
            result = import_cpa_bundle(raw, Path(tmp))
            self.assertEqual(result["format"], "sub2")
            self.assertEqual(result["imported"], 2)
            files = sorted(Path(tmp).glob("*.json"))
            self.assertEqual(len(files), 2)
            for path in files:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                stored = json.loads(path.read_text())
                self.assertNotIn("email", stored)
                self.assertNotIn("name", stored)
                self.assertNotIn("session_token", stored)

    def test_repeated_bundle_reports_created_updated_and_unchanged(self):
        def payload(account_id):
            return json.dumps(
                {
                    "type": "codex",
                    "access_token": "stable-access",
                    "account_id": account_id,
                }
            ).encode()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = import_cpa_bundle(payload("one"), root)
            repeated = import_cpa_bundle(payload("one"), root)
            updated = import_cpa_bundle(payload("two"), root)

            self.assertEqual(
                (first["created"], first["updated"], first["unchanged"]),
                (1, 0, 0),
            )
            self.assertEqual(
                (repeated["created"], repeated["updated"], repeated["unchanged"]),
                (0, 0, 1),
            )
            self.assertEqual(
                (updated["created"], updated["updated"], updated["unchanged"]),
                (0, 1, 0),
            )
            self.assertEqual(len(list(root.glob("*.json"))), 1)

    def test_commit_and_recovery_failures_have_distinct_exceptions(self):
        def payload(account_id):
            return json.dumps(
                {
                    "type": "codex",
                    "access_token": "stable-access",
                    "account_id": account_id,
                }
            ).encode()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            import_cpa_bundle(payload("original"), root)
            target = next(root.glob("*.json"))
            original_bytes = target.read_bytes()
            real_atomic_write = rescue_core._atomic_bytes_write
            calls = 0

            def fail_commit_then_restore(path, data, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("simulated commit failure")
                return real_atomic_write(path, data, **kwargs)

            with mock.patch.object(
                rescue_core,
                "_atomic_bytes_write",
                side_effect=fail_commit_then_restore,
            ):
                with self.assertRaises(CredentialCommitError):
                    import_cpa_bundle(payload("updated"), root)
            self.assertEqual(target.read_bytes(), original_bytes)

            with mock.patch.object(
                rescue_core,
                "_atomic_bytes_write",
                side_effect=OSError("simulated commit and rollback failure"),
            ):
                with self.assertRaises(CredentialRecoveryError):
                    import_cpa_bundle(payload("updated-again"), root)

    def test_invalid_bundle_does_not_write_any_credentials(self):
        raw = b'{"accounts":[{"platform":"openai","type":"oauth","credentials":{"access_token":"first","id_token":"id"}},{"platform":"evil","type":"oauth","credentials":{"access_token":"second","id_token":"id"}}]}'
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CredentialError):
                import_cpa_bundle(raw, Path(tmp))
            self.assertEqual(list(Path(tmp).glob("*.json")), [])

    def test_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(CredentialError, "重复字段"):
                import_cpa_bundle(
                    b'{"type":"codex","access_token":"first","access_token":"second"}',
                    Path(tmp),
                )
            self.assertEqual(list(Path(tmp).glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
