from __future__ import annotations

import unittest

from security_target import redact_persisted_text


class SecretRedactionTests(unittest.TestCase):
    def test_redacts_openai_style_credentials(self) -> None:
        result = redact_persisted_text("worker failed with sk-examplecredential123 in stderr")

        self.assertNotIn("sk-examplecredential123", result)
        self.assertIn("worker failed with", result)


if __name__ == "__main__":
    unittest.main()
