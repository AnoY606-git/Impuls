import unittest

try:
    from impuls_player.ui import _format_time, _is_safe_login_url
except ModuleNotFoundError as exc:
    if exc.name not in ('impuls_player', 'impuls_player.ui'):
        raise
    _format_time = None
    _is_safe_login_url = None

class FormatterTests(unittest.TestCase):
    def test_duration_is_mm_ss_and_never_negative(self):
        self.assertIsNotNone(_format_time, 'Duration formatter must exist')
        self.assertEqual(_format_time(0), '0:00')
        self.assertEqual(_format_time(61000), '1:01')
        self.assertEqual(_format_time(-1), '0:00')
        self.assertEqual(_format_time(3600000), '60:00')

class SecurityTests(unittest.TestCase):
    def test_track_links_cannot_escape_service_origin(self):
        import impuls_player.ui as ui
        builder = getattr(ui, '_track_url', None)
        self.assertTrue(callable(builder), 'Track link builder must exist')
        self.assertEqual(builder('123'), 'https://music.yandex.ru/track/123')
        self.assertEqual(builder('123:456'), 'https://music.yandex.ru/track/123')
        for track_id in ('', '../123', '//evil.test', '1?next=https://evil.test', None):
            self.assertIsNone(builder(track_id))

    def test_device_login_only_links_to_yandex_over_https(self):
        self.assertIsNotNone(_is_safe_login_url, 'Login URL validator must exist')
        self.assertTrue(_is_safe_login_url('https://ya.ru/device'))
        self.assertTrue(_is_safe_login_url('https://passport.yandex.ru/auth'))
        for url in ('http://ya.ru/device', 'javascript:alert(1)', 'https://ya.ru.evil.test/', 'https://ya.ru@evil.test/', 'https://evil.test/', 'https://ya.ru:444/device'):
            with self.subTest(url=url):
                self.assertFalse(_is_safe_login_url(url))

if __name__ == '__main__':
    unittest.main()
