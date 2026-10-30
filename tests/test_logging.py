import logging
from logging.handlers import RotatingFileHandler
import tempfile
import unittest
import bot


class LoggingSafetyTests(unittest.TestCase):
    def test_http_client_logs_do_not_expose_bot_token(self):
        self.assertEqual(bot.log.name, 'financeai')  # importar bot configura o logging
        for name in ('httpx', 'httpcore'):
            self.assertGreaterEqual(logging.getLogger(name).getEffectiveLevel(), logging.WARNING)

    def test_rotating_file_handler_is_configured_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_file = bot.setup_file_logging(tmp)
            bot.setup_file_logging(tmp)
            root = logging.getLogger()
            matching = [h for h in root.handlers
                        if isinstance(h, RotatingFileHandler) and h.baseFilename == str(log_file)]
            try:
                self.assertEqual(len(matching), 1)
                self.assertEqual(matching[0].maxBytes, 1_000_000)
                self.assertEqual(matching[0].backupCount, 3)
            finally:
                for h in matching:
                    root.removeHandler(h)
                    h.close()


if __name__ == '__main__':
    unittest.main()
