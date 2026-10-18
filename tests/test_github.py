import json
import sys
import unittest

from auto_pulse.errors import AuthError, GhError, TransientError
from auto_pulse.github import GhClient
from auto_pulse.redact import redact
from auto_pulse.runner import Result, SubprocessRunner, classify


class FakeRunner:
    def __init__(self, *results: Result):
        self.results = list(results)
        self.calls = []

    def run(self, args, input=None):
        self.calls.append((args, input))
        return self.results.pop(0)


def ok(data) -> Result:
    return Result(0, data if isinstance(data, str) else json.dumps(data), "")


ISSUE_NODE = {
    "id": "I_1", "number": 7, "title": "t", "body": "b", "url": "https://github.com/me/repo/issues/7",
    "state": "OPEN", "updatedAt": "2026-10-18T00:00:00Z", "labels": {"nodes": [{"name": "ai-managed"}]},
    "projectItems": {"nodes": [
        {"id": "OTHER", "project": {"id": "P2", "number": 9, "owner": {"login": "me"}}, "fieldValues": {"nodes": []}},
        {"id": "PVTI_7", "project": {"id": "P1", "number": 1, "owner": {"login": "Me"}}, "fieldValues": {"nodes": [
            {"__typename": "ProjectV2ItemFieldSingleSelectValue", "name": "Review", "field": {"name": "Status"}},
            {"__typename": "ProjectV2ItemFieldTextValue", "text": "次", "field": {"name": "Next Action"}},
            {"__typename": "ProjectV2ItemFieldDateValue", "date": "2026-10-17", "field": {"name": "Last Activity"}},
            {"__typename": "ProjectV2ItemFieldNumberValue", "number": 3.0, "field": {"name": "Effort"}},
            {"__typename": "ProjectV2ItemFieldRepositoryValue"},
        ]}},
    ]},
}


class GhClientTest(unittest.TestCase):
    def test_get_issue_picks_configured_project_only(self):
        runner = FakeRunner(ok({"data": {"repository": {"issue": ISSUE_NODE}}}))
        snap = GhClient(runner, mutation_interval=0).get_issue("me/repo", 7, "me", 1)
        self.assertEqual(snap.item_id, "PVTI_7")
        self.assertEqual(snap.fields, {"Status": "Review", "Next Action": "次", "Last Activity": "2026-10-17", "Effort": 3.0})
        args, payload = runner.calls[0]
        self.assertEqual(args, ["api", "graphql", "--input", "-"])
        self.assertEqual(json.loads(payload)["variables"], {"owner": "me", "name": "repo", "number": 7})

    def test_graphql_errors_raise(self):
        runner = FakeRunner(ok({"data": None, "errors": [{"message": "Could not resolve"}]}))
        with self.assertRaises(GhError):
            GhClient(runner, mutation_interval=0).get_issue("me/repo", 7, "me", 1)

    def test_set_field_uses_name_based_item_edit(self):
        runner = FakeRunner(ok(""), ok(""))
        client = GhClient(runner, mutation_interval=0)
        client.set_field("me", 1, "https://github.com/me/repo/issues/7", "Status", "In Progress")
        client.set_field("me", 1, "https://github.com/me/repo/issues/7", "Next Action", "")
        self.assertEqual(
            runner.calls[0][0],
            ["project", "item-edit", "1", "--owner", "me", "--url", "https://github.com/me/repo/issues/7", "--field", "Status", "--value", "In Progress"],
        )
        self.assertEqual(runner.calls[1][0][-1], "--clear")

    def test_comment_body_goes_through_stdin(self):
        runner = FakeRunner(ok(""))
        GhClient(runner, mutation_interval=0).add_comment("me/repo", 7, "本文 `--danger`")
        args, stdin = runner.calls[0]
        self.assertEqual(args, ["issue", "comment", "7", "--repo", "me/repo", "--body-file", "-"])
        self.assertEqual(stdin, "本文 `--danger`")

    def test_comment_event_ids(self):
        bodies = ["<!-- autopulse:event=0b1c2d3e-0000-4000-8000-000000000001 -->\nx", "人のコメント"]
        runner = FakeRunner(ok("\n".join(json.dumps(b) for b in bodies)))
        self.assertEqual(GhClient(runner, mutation_interval=0).comment_event_ids("me/repo", 7), {"0b1c2d3e-0000-4000-8000-000000000001"})

    def test_auth_status_parsing(self):
        text = "github.com\n  ✓ Logged in to github.com account octo (keyring)\n  - Token scopes: 'gist', 'project', 'repo'\n"
        status = GhClient(mutation_interval=0, runner=FakeRunner(Result(0, text, ""))).auth_status()
        self.assertEqual(status, {"logged_in": True, "account": "octo", "scopes": ["gist", "project", "repo"]})

    def test_bad_repo_name(self):
        with self.assertRaises(GhError):
            GhClient(mutation_interval=0, runner=FakeRunner()).get_issue("noslash", 1, None, None)


class SubprocessRunnerTest(unittest.TestCase):
    def test_stdin_is_passed_byte_exact(self):
        echo = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
        result = SubprocessRunner(sys.executable).run(["-c", echo], input="a\r\nb\n日本語")
        self.assertEqual(result.stdout, "a\r\nb\n日本語")


class MutationPacingTest(unittest.TestCase):
    def test_writes_are_spaced_but_reads_are_not(self):
        import time
        runner = FakeRunner(ok(""), ok(""), ok(""))
        client = GhClient(runner, mutation_interval=0.2)
        start = time.monotonic()
        client.gh("issue", "view", "1")
        client.add_comment("me/repo", 1, "a")
        client.add_comment("me/repo", 1, "b")
        self.assertGreaterEqual(time.monotonic() - start, 0.2)


class ClassifyTest(unittest.TestCase):
    def test_rate_limit_is_transient(self):
        self.assertIsInstance(classify(Result(1, "", "HTTP 403: API rate limit exceeded")), TransientError)
        self.assertIsInstance(classify(Result(1, "", "HTTP 502: Bad Gateway")), TransientError)
        self.assertIsInstance(classify(Result(1, "", "dial tcp: lookup api.github.com: no such host")), TransientError)

    def test_windows_connection_refused_is_transient(self):
        # Seen in the E2E run with an unreachable proxy; it used to be treated as permanent.
        text = (
            'Post "https://api.github.com/graphql": proxyconnect tcp: dial tcp 127.0.0.1:9: connectex: '
            "No connection could be made because the target machine actively refused it."
        )
        self.assertIsInstance(classify(Result(1, "", text)), TransientError)

    def test_unknown_output_is_retried(self):
        self.assertIsInstance(classify(Result(1, "", "something unexpected")), TransientError)

    def test_github_answers_are_permanent(self):
        self.assertIsInstance(classify(Result(1, "", "GraphQL: Could not resolve to an Issue with the number of 9.")), GhError)
        self.assertIsInstance(classify(Result(1, "", "HTTP 404: Not Found")), GhError)

    def test_auth_problems(self):
        self.assertIsInstance(classify(Result(1, "", "To get started with GitHub CLI, please run:  gh auth login")), AuthError)
        self.assertIsInstance(classify(Result(1, "", "your token has not been granted the required scopes")), AuthError)

    def test_other_errors_are_permanent_and_masked(self):
        err = classify(Result(1, "", "HTTP 422: invalid token ghp_abcdefghijklmnopqrstu"))
        self.assertIsInstance(err, GhError)
        self.assertFalse(err.retryable)
        self.assertNotIn("abcdefghijklmnopqrstu", str(err))


class RedactTest(unittest.TestCase):
    def test_masks_common_secrets(self):
        text = "ghp_abcdefghij1234567890 github_pat_11ABCDEFG_xyzxyzxyzxyz Bearer abcdefghijklmnop1234 password=hunter2"
        masked = redact(text)
        for secret in ("abcdefghij1234567890", "11ABCDEFG", "abcdefghijklmnop1234", "hunter2"):
            self.assertNotIn(secret, masked)

    def test_keeps_normal_text(self):
        self.assertEqual(redact("配線図を確認する token 数を減らす"), "配線図を確認する token 数を減らす")


if __name__ == "__main__":
    unittest.main()
