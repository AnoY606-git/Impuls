"""Check our adapter against actual upstream models, without network access."""
import inspect
import unittest

try:
    from yandex_music import Album, Artist, Client, Track as ApiTrack
except ImportError:
    Client = None

from impuls_player.service import Track, _track


@unittest.skipUnless(Client is not None, 'yandex-music is not installed')
class LibraryContractTests(unittest.TestCase):
    def test_real_track_model_is_converted(self):
        raw = ApiTrack(id='123', title='Song', artists=[Artist(name='Artist')],
                       albums=[Album(title='Album')], duration_ms=60000,
                       cover_uri='avatars.yandex.net/cover/%%')
        self.assertEqual(_track(raw), Track('123', 'Song', ('Artist',), 'Album', 60000,
                                             'https://avatars.yandex.net/cover/200x200'))

    def test_real_track_with_album_id_preserves_composite_identifier(self):
        raw = ApiTrack(id='123', title='Song', albums=[Album(id=17, title='Album')])
        self.assertEqual(_track(raw).id, '123:17')

    def test_device_and_radio_entry_points_have_expected_arguments(self):
        for method, required in (
            (Client.request_device_code, ('device_name',)),
            (Client.poll_device_token, ('device_code',)),
            (Client.tracks_file_info, ('track_id', 'quality', 'codecs', 'transport')),
            (Client.rotor_session_tracks, ('radio_session_id', 'queue')),
        ):
            self.assertTrue(set(required).issubset(inspect.signature(method).parameters))


if __name__ == '__main__': unittest.main()
