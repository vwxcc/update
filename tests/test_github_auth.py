import unittest
from urllib.request import Request
from unittest.mock import patch

import auto_deploy_daemon as updater


class GitHubTokenTests(unittest.TestCase):
    def test_auth_headers_use_bearer_token(self):
        with patch.object(updater, "GITHUB_TOKEN", "github_pat_test_token_123"):
            headers = updater.github_headers()
        self.assertEqual(headers["Authorization"], "Bearer github_pat_test_token_123")
        self.assertEqual(headers["Accept"], "application/vnd.github+json")
        self.assertNotIn("github_pat_test_token_123", updater.API_URL)
        self.assertNotIn("github_pat_test_token_123", updater.ARCHIVE_API_URL)

    def test_missing_token_fails_closed_without_echoing_secret(self):
        with patch.object(updater, "GITHUB_TOKEN", ""):
            with self.assertRaises(RuntimeError) as caught:
                updater.github_headers()
        self.assertIn("GITHUB_TOKEN", str(caught.exception))
        self.assertNotIn("github_pat_test_token_123", str(caught.exception))

    def test_invalid_token_characters_are_rejected(self):
        with patch.object(updater, "GITHUB_TOKEN", "token\r\nInjected: yes"):
            with self.assertRaises(RuntimeError):
                updater.github_headers()

    def test_cross_host_archive_redirect_drops_authorization(self):
        with patch.object(updater, "GITHUB_TOKEN", "github_pat_test_token_123"):
            request = Request(
                updater.ARCHIVE_API_URL + ("a" * 40),
                headers=updater.github_headers(),
            )
        handler = updater.SafeGitHubArchiveRedirectHandler()
        redirected = handler.redirect_request(
            request, None, 302, "Found", {}, "https://codeload.github.com/vwxcc/deepseekw/legacy.tar.gz/example"
        )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))
        self.assertIsNone(redirected.get_header("Cookie"))

    def test_untrusted_archive_redirect_is_rejected(self):
        handler = updater.SafeGitHubArchiveRedirectHandler()
        with self.assertRaises(Exception):
            handler.redirect_request(
                Request(updater.ARCHIVE_API_URL + ("a" * 40)),
                None,
                302,
                "Found",
                {},
                "https://attacker.example/archive.tar.gz",
            )


if __name__ == "__main__":
    unittest.main()
