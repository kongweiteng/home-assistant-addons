from datetime import datetime, timedelta, timezone
import unittest
from weixin_gateway.m8_reminders import parse_command, is_reminder_command

NOW = datetime(2026, 9, 28, 20, tzinfo=timezone(timedelta(hours=8)))


class M8ReplyParsingTests(unittest.TestCase):
    def test_exact_ignore_only(self):
        self.assertEqual(parse_command('忽略本次充电')['command'], 'ignore_current_charge')
        for text in ('忽略', '别提醒天气', '明天再说', '请转发22点再提醒这句话'):
            self.assertFalse(is_reminder_command(text))

    def test_20_to_22_and_half_hour(self):
        for text in ('22点再提醒', '今晚22点再提醒', '晚上10点再提醒我', '22:00再提醒', '延迟到22点再提醒'):
            result = parse_command(text, NOW)
            self.assertEqual(result['command'], 'defer_current_charge')
            self.assertEqual(datetime.fromisoformat(result['remind_at']).hour, 22)
        self.assertEqual(datetime.fromisoformat(parse_command('22点半再提醒', NOW)['remind_at']).minute, 30)

    def test_past_and_after_cutoff_are_not_silently_reinterpreted(self):
        for text in ('19点再提醒', '23点再提醒', '24点再提醒', '22:70再提醒'):
            with self.assertRaises(ValueError):
                parse_command(text, NOW)
        target = datetime.fromisoformat(parse_command('明天08点再提醒', NOW)['remind_at'])
        self.assertEqual(target.day, 29)
        self.assertEqual(target.hour, 8)


if __name__ == '__main__':
    unittest.main()
