import logging
import unittest
import bot  # noqa: F401  (configura o logging)


class LoggingSafetyTests(unittest.TestCase):
    def test_http_client_logs_do_not_expose_bot_token(self):
        for name in ('httpx', 'httpcore'):
            self.assertGreaterEqual(logging.getLogger(name).getEffectiveLevel(), logging.WARNING)


if __name__ == '__main__':
    unittest.main()
