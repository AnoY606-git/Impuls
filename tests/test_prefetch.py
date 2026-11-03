"""Prefetch audio without touching the network or the user's credentials."""

import io
import logging
import unittest
from unittest.mock import patch

from impuls_player.prefetch import PrefetchError, download_audio


class Raw:
    def __init__(self, chunks, after_read=None):
        self.chunks = iter(chunks)
        self.after_read = after_read
        self.reads = 0

    def read1(self, size, decode_content=False):
        assert size <= 65536 and not decode_content
        self.reads += 1
        result = next(self.chunks, b'')
        if self.after_read:
            self.after_read(self.reads)
        return result


class Response:
    def __init__(self, chunks=(b'audio',), length='5', status=200, encoding=None):
        self.status_code = status
        self.headers = {}
        if length is not None:
            self.headers['Content-Length'] = length
        if encoding is not None:
            self.headers['Content-Encoding'] = encoding
        self.raw = Raw(chunks)
        self.closed = False

    def close(self):
        self.closed = True


class Session:
    def __init__(self, response):
        self.response = response
        self.trust_env = True
        self.headers = {'Authorization': 'must-not-leak'}
        self.auth = 'must-not-leak'
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.response = Response()
        self.session = Session(self.response)
        self.patcher = patch('impuls_player.prefetch.requests.Session', return_value=self.session)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.url = 'https://s50vla.storage.yandex.net/audio?signature=private'

    def test_downloads_complete_bytes_without_credentials_or_redirects(self):
        self.assertEqual(download_audio(self.url), b'audio')
        self.assertFalse(self.session.trust_env)
        self.assertIsNone(self.session.auth)
        self.assertNotIn('Authorization', self.session.headers)
        _, options = self.session.calls[0]
        self.assertEqual(options['allow_redirects'], False)
        self.assertEqual(options['stream'], True)
        self.assertEqual(options['headers']['Accept-Encoding'], 'identity')
        self.assertNotIn('Authorization', options['headers'])
        self.assertTrue(self.response.closed)
        self.assertTrue(self.session.closed)

    def test_rejects_untrusted_urls_before_contacting_network(self):
        for url in ('http://yandex.net/audio', 'https://evil.example/audio',
                    'https://yandex.net.evil.example/audio', 'https://user@yandex.net/audio',
                    'https://127.0.0.1/audio'):
            with self.subTest(url=url), self.assertRaises(PrefetchError):
                download_audio(url)
        self.assertEqual(self.session.calls, [])

    def test_rejects_redirects_partial_responses_and_encoded_bodies(self):
        for status, encoding in ((301, None), (302, None), (206, None), (500, None), (200, 'gzip')):
            with self.subTest(status=status, encoding=encoding):
                self.session.response = Response(status=status, encoding=encoding)
                with self.assertRaises(PrefetchError):
                    download_audio(self.url)
                self.assertTrue(self.session.response.closed)

    def test_rejects_unknown_empty_oversize_and_invalid_content_length(self):
        for length in (None, '', '-1', '0', 'NaN', '67108865'):
            with self.subTest(length=length):
                self.session.response = Response(length=length)
                with self.assertRaises(PrefetchError):
                    download_audio(self.url)
                self.assertEqual(self.session.response.raw.reads, 0)

    def test_rejects_truncated_or_excess_response(self):
        for chunks, declared in (((b'abc',), '4'), ((b'ab', b'cde'), '4')):
            with self.subTest(chunks=chunks):
                self.session.response = Response(chunks=chunks, length=declared)
                with self.assertRaises(PrefetchError):
                    download_audio(self.url)
                self.assertTrue(self.session.response.closed)

    def test_cancel_before_network_or_between_chunks(self):
        with self.assertRaises(PrefetchError):
            download_audio(self.url, should_cancel=lambda: True)
        self.assertEqual(self.session.calls, [])
        cancelled = [False]
        self.response.raw = Raw((b'ab', b'cde'), after_read=lambda count: cancelled.__setitem__(0, True))
        with self.assertRaises(PrefetchError):
            download_audio(self.url, should_cancel=lambda: cancelled[0])
        self.assertEqual(self.response.raw.reads, 1)
        self.assertTrue(self.response.closed)

    def test_total_deadline_is_checked_while_data_arrives(self):
        self.response = Response(chunks=(b'a', b'b', b'c', b'd', b'e'))
        self.session.response = self.response
        clock = iter((0.0, 0.2, 0.5, 1.1, 1.2, 1.3))
        with patch('impuls_player.prefetch.time.monotonic', side_effect=lambda: next(clock)):
            with self.assertRaises(PrefetchError):
                download_audio(self.url, timeout=1)
        self.assertTrue(self.response.closed)

    def test_network_errors_do_not_expose_url_or_token(self):
        self.session.get = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError('secret-token https://example.invalid/private'))
        with self.assertRaises(PrefetchError) as error:
            download_audio(self.url)
        self.assertNotIn('secret-token', str(error.exception))
        self.assertNotIn('https://', str(error.exception))
        self.assertTrue(self.session.closed)

    def test_http_debug_logging_does_not_expose_signed_url(self):
        logger = logging.getLogger('urllib3.connectionpool')
        previous_level = logger.level
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        old_get = self.session.get
        def debug_get(url, **kwargs):
            logger.debug('GET %s', url)
            return old_get(url, **kwargs)
        self.session.get = debug_get
        try:
            self.assertEqual(download_audio(self.url), b'audio')
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
        self.assertNotIn('signature=private', output.getvalue())


if __name__ == '__main__':
    unittest.main()
