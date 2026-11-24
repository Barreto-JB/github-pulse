import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import github_pulse


REPO = {
    "full_name": "example/project",
    "private": False,
    "stargazers_count": 18,
    "forks_count": 3,
    "open_issues_count": 7,
    "pushed_at": "2026-01-02T00:00:00Z",
    "updated_at": "2026-01-03T00:00:00Z",
    "default_branch": "main",
    "archived": False,
}
RELEASE = {
    "tag_name": "v1.0\x1b[2J",
    "published_at": "2026-01-01T00:00:00Z",
    "prerelease": False,
    "draft": False,
    "body": "Untrusted release notes",
}


class PulseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(directory.cleanup)
        self.cache = Path(directory.name)

    def invoke(self, *args, repo="Example/Project"):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = github_pulse.main([repo, "--cache-dir", str(self.cache), *args])
        return status, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def serve(*payloads):
        responses = [io.BytesIO(json.dumps(data).encode()) for data in payloads]
        return patch.object(github_pulse.OPENER, "open", side_effect=responses)

    def test_fetch_reports_activity_and_published_releases(self):
        draft = dict(RELEASE, tag_name="draft", draft=True)
        preview = dict(RELEASE, tag_name="v2-beta", prerelease=True,
                       published_at="2026-01-04T00:00:00Z")
        with self.serve(REPO, [draft, RELEASE, preview]) as transport:
            status, output, error = self.invoke("--json")
        self.assertEqual((status, error), (0, ""))
        snapshot = json.loads(output)
        self.assertEqual(snapshot["source"], "live")
        self.assertEqual(snapshot["repository"]["open_issues_count"], 7)
        self.assertEqual(snapshot["repository"]["pushed_at"], "2026-01-02T00:00:00Z")
        self.assertEqual([r["tag_name"] for r in snapshot["releases"]],
                         ["v2-beta", "v1.0\x1b[2J"])
        self.assertNotIn("body", snapshot["releases"][0])
        requests = [call.args[0] for call in transport.call_args_list]
        self.assertEqual([r.full_url for r in requests], [
            "https://api.github.com/repos/example/project",
            "https://api.github.com/repos/example/project/releases?per_page=10",
        ])
        self.assertTrue(all("Authorization" not in r.headers for r in requests))

    def test_fresh_cache_avoids_http_and_escapes_terminal_controls(self):
        with self.serve(REPO, [RELEASE]):
            self.assertEqual(self.invoke()[0], 0)
        with self.serve() as transport:
            status, output, error = self.invoke(repo="example/project")
        transport.assert_not_called()
        self.assertEqual((status, error), (0, ""))
        self.assertIn("cache", output)
        self.assertIn(r"\u001b", output)
        self.assertNotIn("\x1b", output)

    def test_offline_reads_stale_snapshot_while_online_refreshes_it(self):
        with self.serve(REPO, []), patch.object(github_pulse.time, "time", return_value=1000):
            self.assertEqual(self.invoke()[0], 0)
        with patch.object(github_pulse.time, "time", return_value=2000):
            with self.serve() as transport:
                status, output, _ = self.invoke("--offline", "--json")
            transport.assert_not_called()
            self.assertEqual(status, 0)
            self.assertTrue(json.loads(output)["stale"])
            with self.serve(dict(REPO, stargazers_count=19), []):
                status, output, _ = self.invoke("--json")
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)["repository"]["stargazers_count"], 19)

    def test_offline_missing_or_corrupt_cache_fails_without_http(self):
        with self.serve() as transport:
            status, output, error = self.invoke("--offline")
            self.assertEqual((status, output), (1, ""))
            self.assertIn("cached snapshot", error)
            (self.cache / "example+project.json").write_text("{}", encoding="utf-8")
            self.assertEqual(self.invoke("--offline")[0], 1)
        transport.assert_not_called()

    def test_failed_refresh_preserves_previous_snapshot(self):
        with self.serve(REPO, []):
            self.assertEqual(self.invoke()[0], 0)
        with patch.object(github_pulse.OPENER, "open", side_effect=[
            io.BytesIO(json.dumps(REPO).encode()),
            HTTPError("https://api.github.com", 429, "rate limit", {}, None),
        ]):
            status, output, error = self.invoke("--refresh")
        self.assertEqual((status, output), (1, ""))
        self.assertIn("rate limit", error)
        self.assertEqual(self.invoke("--offline")[0], 0)

    def test_invalid_repository_is_rejected_before_http(self):
        with self.serve() as transport:
            with self.assertRaises(SystemExit) as raised:
                self.invoke(repo="example/../project")
        transport.assert_not_called()
        self.assertEqual(raised.exception.code, 2)

    def test_malformed_payload_is_not_cached(self):
        with self.serve(dict(REPO, stargazers_count="lots"), []):
            self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(list(self.cache.iterdir()), [])


    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO required")
    def test_fifo_cache_rejected(self):
        os.mkfifo(self.cache / "example+project.json")
        self.assertEqual(self.invoke("--offline")[0], 1)

    def test_symlink_cache_rejected(self):
        with self.serve(REPO, []):
            self.assertEqual(self.invoke()[0], 0)
        link = self.cache / "example+project.json"
        target = self.cache / "target"
        link.rename(target)
        self.assertIsNotNone(github_pulse.read_cache(target, "example/project"))
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("Symlink permissions unavailable")
        self.assertIsNone(github_pulse.read_cache(link, "example/project"))

    def test_oversize_refresh_preserves_cache(self):
        with self.serve(REPO, []):
            self.assertEqual(self.invoke()[0], 0)
        old = (self.cache / "example+project.json").read_bytes()
        with patch.object(github_pulse, "MAX_RESPONSE_BYTES", 1000):
            with self.serve(REPO, [dict(RELEASE, tag_name="é" * 130)]):
                self.assertEqual(self.invoke("--refresh")[0], 1)
        self.assertEqual((self.cache / "example+project.json").read_bytes(), old)
        self.assertEqual(self.invoke("--offline")[0], 0)

    def test_invalid_calendar_dates(self):
        for repo, releases in [
            (dict(REPO, updated_at="2026-02-30T00:00:00Z"), []),
            (dict(REPO, pushed_at="2026-01-01T25:00:00Z"), []),
            (REPO, [dict(RELEASE, published_at="2026-02-30T00:00:00Z")]),
        ]:
            with self.serve(repo, releases):
                self.assertEqual(self.invoke("--refresh")[0], 1)
        self.assertEqual(list(self.cache.iterdir()), [])


    def test_secondary_rate_limit_retry_after(self):
        for headers, body, expect in [
            ({"X-RateLimit-Remaining":"10", "Retry-After":"60"}, b"{}", "Retry after 60 seconds"),
            ({"X-RateLimit-Remaining":"10"}, b'{"message":"You have exceeded a secondary rate limit."}', "rate limit"),
        ]:
            err = HTTPError("https://api.github.com", 403, "Forbidden", headers, io.BytesIO(body))
            with patch.object(github_pulse.OPENER, "open", side_effect=err):
                status, output, error = self.invoke("--refresh")
            self.assertEqual((status, output), (1, ""))
            self.assertIn(expect, error)
            self.assertIn("--offline", error)


    def test_default_cache_is_not_working_directory(self):
        with patch.dict(os.environ, {"XDG_CACHE_HOME":"relative-cache", "LOCALAPPDATA":"relative-cache"}):
            cache = github_pulse.default_cache_dir()
            self.assertTrue(cache.is_absolute())
            self.assertNotEqual(cache, Path.cwd() / ".github-pulse-cache")

    def test_serialized_limit_independently(self):
        path = self.cache / "snapshot.json"
        original = b"original"
        path.write_bytes(original)
        with patch.object(github_pulse, "MAX_RESPONSE_BYTES", 80):
            with self.assertRaises(github_pulse.PulseError):
                github_pulse.write_cache(path, 1000, {"repository":{"full_name":"x"*100},"releases":[]})
        self.assertEqual(path.read_bytes(), original)

    def test_explicit_cache_and_help_without_home(self):
        with patch.object(Path, "home", side_effect=RuntimeError("No home")):
            with self.serve(REPO, []):
                self.assertEqual(self.invoke()[0], 0)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
                github_pulse.main(["--help"])
            self.assertEqual(e.exception.code, 0)

    def test_absolute_override_without_home(self):
        with patch.object(Path, "home", side_effect=RuntimeError("No home")):
            with patch.dict(os.environ, {"XDG_CACHE_HOME":str(self.cache), "LOCALAPPDATA":str(self.cache)}):
                if github_pulse.sys.platform != "darwin":
                    self.assertTrue(github_pulse.default_cache_dir().is_relative_to(self.cache))

    def test_no_home_without_override_reports_usage(self):
        with patch.object(Path, "home", side_effect=RuntimeError("No home")):
            with patch.dict(os.environ, {"XDG_CACHE_HOME":"relative", "LOCALAPPDATA":"relative"}):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
                    github_pulse.main(["example/project", "--offline"])
        self.assertEqual(e.exception.code, 2)
