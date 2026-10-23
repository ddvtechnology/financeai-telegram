import logging
import unittest
import bot


class LoggingSafetyTests(unittest.TestCase):
    def test_http_client_logs_do_not_expose_bot_token(self):
        self.assertEqual(bot.log.name, 'financeai')  # importar bot configura o logging
        for name in ('httpx', 'httpcore'):
            self.assertGreaterEqual(logging.getLogger(name).getEffectiveLevel(), logging.WARNING)


if __name__ == '__main__':
    unittest.main()
