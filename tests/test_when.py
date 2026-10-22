import unittest
from datetime import datetime, timedelta
import when
from config import TZ

NOW = datetime(2026, 10, 22, 7, 58, tzinfo=TZ)  # quinta-feira


def r(text, now=NOW):
    return when.resolve(text, now)


def at(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


class WhenTests(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(r('me lembra daqui a 30 minutos de tirar o bolo'), at(2026, 10, 22, 8, 28))
        self.assertEqual(r('daqui a 2 horas'), at(2026, 10, 22, 9, 58))
        self.assertEqual(r('em 1h30 me avisa'), at(2026, 10, 22, 9, 28))
        self.assertEqual(r('daqui a uma hora e meia'), at(2026, 10, 22, 9, 28))
        self.assertEqual(r('daqui a meia hora'), at(2026, 10, 22, 8, 28))
        self.assertEqual(r('daqui a cinco min'), at(2026, 10, 22, 8, 3))
        self.assertEqual(r('daqui a 2 dias às 9h'), at(2026, 10, 24, 9))

    def test_clock_formats(self):
        self.assertEqual(r('hoje às 15h30'), at(2026, 10, 22, 15, 30))
        self.assertEqual(r('hoje 15:30'), at(2026, 10, 22, 15, 30))
        self.assertEqual(r('hoje às 3 da tarde'), at(2026, 10, 22, 15))
        self.assertEqual(r('amanhã às 9 da noite'), at(2026, 10, 23, 21))
        self.assertEqual(r('amanhã às 9 da manhã'), at(2026, 10, 23, 9))
        self.assertEqual(r('amanhã 9 horas'), at(2026, 10, 23, 9))
        self.assertEqual(r('hoje ao meio dia'), at(2026, 10, 22, 12))
        self.assertEqual(r('à meia-noite'), at(2026, 10, 23, 0))
        self.assertEqual(r('amanhã 12 da noite'), at(2026, 10, 23, 0))

    def test_time_without_date_picks_next_occurrence(self):
        self.assertEqual(r('às 15h'), at(2026, 10, 22, 15))
        self.assertEqual(r('às 7h'), at(2026, 10, 23, 7))

    def test_dates(self):
        self.assertEqual(r('pagar aluguel dia 28 às 9h'), at(2026, 10, 28, 9))
        self.assertEqual(r('dia 5 às 10h'), at(2026, 11, 5, 10))
        self.assertEqual(r('dia 22 às 8h'), at(2026, 10, 22, 8))
        self.assertEqual(r('dia 22 às 7h'), at(2026, 11, 22, 7))
        self.assertEqual(r('dia 31 às 8h', at(2026, 11, 2, 8)), at(2026, 12, 31, 8))
        self.assertEqual(r('28/10 às 14h'), at(2026, 10, 28, 14))
        self.assertEqual(r('05/01 às 9h'), at(2027, 1, 5, 9))
        self.assertEqual(r('01/12/2026 às 8h'), at(2026, 12, 1, 8))
        self.assertEqual(r('15 de novembro às 10:30'), at(2026, 11, 15, 10, 30))
        self.assertEqual(r('depois de amanhã às 8h'), at(2026, 10, 24, 8))

    def test_weekdays(self):
        self.assertEqual(r('sexta às 8'), at(2026, 10, 23, 8))
        self.assertEqual(r('quinta às 9'), at(2026, 10, 22, 9))
        self.assertEqual(r('quinta-feira às 7'), at(2026, 10, 29, 7))
        self.assertEqual(r('domingo às 18h'), at(2026, 10, 25, 18))

    def test_no_time_returns_none(self):
        self.assertIsNone(r('me lembra de pagar aluguel dia 28'))
        self.assertIsNone(r('gastei 45,90 no almoço'))
        self.assertIsNone(r('daqui a 2 dias'))

    def test_last_mention_wins_for_corrections(self):
        self.assertEqual(r('me lembra de pagar aluguel amanhã às 9 não, às 10'), at(2026, 10, 23, 10))
        self.assertEqual(r('me lembra de pagar aluguel amanhã 9h'), at(2026, 10, 23, 9))

    def test_amounts_do_not_become_times(self):
        self.assertEqual(r('pagar R$ 1.200,50 de luz dia 28 às 9h'), at(2026, 10, 28, 9))
        self.assertIsNone(r('pagar 15 reais de luz dia 28'))

    def test_invalid_values(self):
        self.assertIsNone(r('dia 30/02 às 9h'))
        self.assertIsNone(r('32/13 às 9h'))

    def test_humanize(self):
        self.assertEqual(when.humanize(timedelta(minutes=30)), 'daqui a 30 min')
        self.assertEqual(when.humanize(timedelta(hours=2, minutes=5)), 'daqui a 2 h 5 min')
        self.assertEqual(when.humanize(timedelta(days=1, hours=1)), 'daqui a 25 h')
        self.assertEqual(when.humanize(timedelta(days=6)), 'daqui a 6 dias')


if __name__ == '__main__':
    unittest.main()
