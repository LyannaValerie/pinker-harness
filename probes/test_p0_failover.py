import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import p0_auth
import p0_failover


class FailoverStateTest(unittest.TestCase):
    def test_valid_login_and_request_are_available(self):
        attempt = p0_failover.Attempt("Anthropic", "amara", "M", auth=p0_failover.AuthStatus.PASS)
        attempt.availability = p0_failover.Availability.AVAILABLE
        attempt.request = p0_failover.RequestStatus.PASS
        self.assertEqual(attempt.auth, p0_failover.AuthStatus.PASS)
        self.assertEqual(attempt.availability, p0_failover.Availability.AVAILABLE)
        self.assertEqual(attempt.request, p0_failover.RequestStatus.PASS)

    def test_real_observed_quota_message_preserves_auth(self):
        # Sanitized output observed from claude-velina in this P0 run.
        message = "api_error_status=429; terminal_reason=api_error; You've hit your weekly limit · resets Sep 17, 8am (UTC)"
        availability, request, _ = p0_failover.classify_failure(1, message)
        self.assertEqual(availability, p0_failover.Availability.QUOTA_EXHAUSTED)
        self.assertEqual(request, p0_failover.RequestStatus.BLOCKED_BY_QUOTA)
        source = p0_failover.Attempt("Anthropic", "velina", "M", auth=p0_failover.AuthStatus.PASS)
        source.availability, source.request = availability, request
        self.assertEqual(source.auth, p0_failover.AuthStatus.PASS)
        self.assertNotEqual(source.auth.value, "DISCARDED")

    def test_unknown_error_is_not_quota(self):
        availability, request, _ = p0_failover.classify_failure(1, "connection closed")
        self.assertEqual(availability, p0_failover.Availability.UNKNOWN)
        self.assertEqual(request, p0_failover.RequestStatus.FAILED)

    def test_same_provider_is_selected_before_cross_provider(self):
        target = p0_failover.choose_same_provider("Anthropic", "velina")
        self.assertEqual(target, ("Anthropic", "amara"))

    def test_target_executes_the_same_logical_operation_marker(self):
        seen = []

        def fake_attempt(account, marker):
            seen.append((account, marker))
            if account == "velina":
                return p0_failover.Attempt(
                    "Anthropic", account, marker, auth=p0_failover.AuthStatus.PASS,
                    availability=p0_failover.Availability.QUOTA_EXHAUSTED,
                    request=p0_failover.RequestStatus.BLOCKED_BY_QUOTA,
                )
            return p0_failover.Attempt(
                "Anthropic", account, marker, auth=p0_failover.AuthStatus.PASS,
                availability=p0_failover.Availability.AVAILABLE,
                request=p0_failover.RequestStatus.PASS,
            )

        with patch("p0_failover.attempt_anthropic", side_effect=fake_attempt):
            result = p0_failover.run_probe()
        self.assertEqual(result.status, "P0_FAILOVER_PASS")
        self.assertEqual(seen[0][1], seen[1][1])

    def test_missing_wrapper_is_explicit(self):
        with patch("p0_failover.shutil.which", return_value=None):
            attempt = p0_failover.attempt_anthropic("amara", "M")
        self.assertEqual(attempt.auth, p0_failover.AuthStatus.UNSUPPORTED)
        self.assertEqual(attempt.category, "USER_SCOPED_WRAPPER_MISSING")

    def test_unauthorized_route_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "BLOCK"):
            p0_auth.provider_route("athenna")

    def test_artifact_root_cannot_be_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            checkout.mkdir()
            with self.assertRaisesRegex(RuntimeError, "RUNTIME_EVIDENCE"):
                p0_auth.artifact_root(
                    environment={"PINKER_HARNESS_ARTIFACT_ROOT": str(checkout / "artifacts")},
                    checkout=checkout,
                )


if __name__ == "__main__":
    unittest.main()
