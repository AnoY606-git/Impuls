"""Check our adapter against actual upstream models, without network access."""
import inspect
import unittest
from unittest.mock import MagicMock, patch

try:
    from yandex_music import Account, Album, Artist, Client, Playlist as ApiPlaylist, Status, Track as ApiTrack, TrackShort, TracksList
except ImportError:
    Client = None

from impuls_player.service import MusicService, Playlist, ServiceError, Track, _track


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

    def test_real_likes_and_playlist_models_load_in_order(self):
        client = MagicMock()
        client.me.account.login = 'listener'
        client.me.account.uid = 42
        client.users_likes_tracks.return_value = TracksList(
            uid=42, revision=1, tracks=[TrackShort(id='123', album_id='17', timestamp='now'),
                                        TrackShort(id='456', timestamp='now')])
        client.users_playlists.return_value = ApiPlaylist(cover=None, tracks=[
            TrackShort(id='456', timestamp='now'), TrackShort(id='123', album_id='17', timestamp='now')])
        client.tracks.return_value = [ApiTrack(id='123', title='One', albums=[Album(id=17)]),
                                      ApiTrack(id='456', title='Two')]
        with patch('impuls_player.service._new_client', return_value=client):
            service = MusicService('test-token')
            service.connect()
            liked, more = service.liked_tracks_page()
            self.assertEqual(([item.id for item in liked], more), (['123:17', '456'], False))
            playlist, more = service.playlist_tracks_page(Playlist('1001', 'List', '42'))
            self.assertEqual(([item.id for item in playlist], more), (['456', '123:17'], False))

    def test_real_account_without_uid_is_rejected(self):
        client = MagicMock()
        client.me = Status(account=Account(now='now', service_available=True, uid=None), permissions=None)
        with patch('impuls_player.service._new_client', return_value=client):
            with self.assertRaisesRegex(ServiceError, 'аккаунт'):
                MusicService('test-token').connect()

    def test_actual_empty_tracks_list_is_a_valid_empty_library(self):
        client = MagicMock()
        client.me.account.uid = 42
        client.users_likes_tracks.return_value = TracksList(uid=42, revision=1, tracks=[])
        with patch('impuls_player.service._new_client', return_value=client):
            service = MusicService('test-token')
            service.connect()
            self.assertEqual(service.liked_tracks_page(), ([], False))
        client.tracks.assert_not_called()

    def test_playback_client_uses_independent_upstream_request_without_init(self):
        service = MusicService('test-token')
        service._client = Client('test-token')
        service._uid = '42'
        playback = service.playback_service()
        self.assertIsNot(service._client._request, playback._client._request)
        self.assertIsNot(service._client._request.headers, playback._client._request.headers)
        with patch.object(playback._client._request, 'get', return_value={
                'downloadInfo': {'trackId': '12', 'quality': 'hq', 'codec': 'mp3',
                                 'bitrate': 192, 'transport': 'raw',
                                 'url': 'https://s50vla.storage.yandex.net/audio', 'urls': []}}):
            self.assertEqual(playback.stream_url(Track('12', 'Song', ())),
                             'https://s50vla.storage.yandex.net/audio')

    def test_device_and_radio_entry_points_have_expected_arguments(self):
        for method, required in (
            (Client.request_device_code, ('device_name',)),
            (Client.poll_device_token, ('device_code',)),
            (Client.tracks_file_info, ('track_id', 'quality', 'codecs', 'transport')),
            (Client.rotor_session_tracks, ('radio_session_id', 'queue')),
        ):
            self.assertTrue(set(required).issubset(inspect.signature(method).parameters))


if __name__ == '__main__': unittest.main()
