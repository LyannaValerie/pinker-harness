import unittest

import p0_auth


class ProviderRouteTest(unittest.TestCase):
    def test_authorized_users_have_scoped_wrappers(self):
        for user in ("amara", "velina"):
            with self.subTest(user=user):
                route = p0_auth.provider_route(user)
                self.assertEqual(route["user_scoped_codex"], f"codex-{user}")
                self.assertEqual(route["user_scoped_claude"], f"claude-{user}")

    def test_other_users_are_blocked(self):
        with self.assertRaisesRegex(RuntimeError, "BLOCK"):
            p0_auth.provider_route("athenna")


if __name__ == "__main__":
    unittest.main()
