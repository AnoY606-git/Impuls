"""The API adapter is tested without contacting Yandex or using credentials."""

import unittest
from threading import Event, Thread
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from impuls_player.service import MusicService, Playlist, ServiceError, Track


def api_track(identifier="12", title="Песня"):
    return NS(
        id=identifier,
        track_id=f"{identifier}:34",
        title=title,
        version=None,
        artists=[NS(name="Исполнитель")],
        albums=[NS(title="Альбом")],
        duration_ms=183000,
        cover_uri="avatars.yandex.net/get-music-content/cover/%%",
    )


class MusicServiceTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.me.account.login = "listener"
        self.client.init.return_value = self.client
        self.patcher = patch("impuls_player.service._new_client", return_value=self.client)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.service = MusicService(" test-token ")

    def test_blank_token_is_rejected_without_network(self):
        with self.assertRaises(ValueError):
            MusicService("   ")
        self.client.init.assert_not_called()

    def test_token_is_not_in_repr(self):
        self.assertNotIn("test-token", repr(self.service))

    def test_connect_returns_login(self):
        self.assertEqual(self.service.connect(), "listener")

    def test_library_error_does_not_expose_secret(self):
        self.client.init.side_effect = RuntimeError("bad token test-token")
        with self.assertRaises(ServiceError) as error:
            self.service.connect()
        self.assertNotIn("test-token", str(error.exception))

    def test_search_returns_domain_tracks(self):
        self.service.connect()
        self.client.search.return_value = NS(tracks=NS(results=[api_track()]))
        self.assertEqual(
            self.service.search("  песня  "),
            [Track("12:34", "Песня", ("Исполнитель",), "Альбом", 183000,
                   "https://avatars.yandex.net/get-music-content/cover/200x200")],
        )
        self.client.search.assert_called_once_with("песня", type_="track")

    def test_search_without_results(self):
        self.service.connect()
        self.client.search.return_value = NS(tracks=None)
        self.assertEqual(self.service.search("not found"), [])

    def test_blank_search_does_not_contact_service(self):
        self.service.connect()
        self.assertEqual(self.service.search("   "), [])
        self.client.search.assert_not_called()

    def test_liked_tracks_use_bulk_metadata(self):
        self.service.connect()
        self.client.users_likes_tracks.return_value = NS(tracks_ids=["12:34"])
        self.client.tracks.return_value = [api_track()]
        tracks = self.service.liked_tracks()
        self.assertEqual([track.title for track in tracks], ["Песня"])

    def test_liked_tracks_are_requested_in_bounded_batches(self):
        self.service.connect()
        self.client.users_likes_tracks.return_value = NS(
            tracks_ids=[f"{n}:34" for n in range(1, 206)]
        )

        def limited_request(ids):
            if len(ids) > 100:
                raise ValueError("too many track IDs")
            return [api_track(value.split(":")[0]) for value in ids]

        self.client.tracks.side_effect = limited_request
        tracks = self.service.liked_tracks()
        self.assertEqual(len(tracks), 205)
        self.assertEqual(tracks[-1].id, "205:34")

    def test_playlists_map_owner_and_kind(self):
        self.service.connect()
        self.client.users_playlists_list.return_value = [
            NS(kind=1001, title="Плейлист", owner=NS(uid=42), track_count=7, cover=None)
        ]
        self.assertEqual(self.service.playlists(), [Playlist("1001", "Плейлист", "42", 7)])

    def test_cancelled_library_stops_before_fetching_remaining_batches(self):
        self.service.connect()
        self.client.users_likes_tracks.return_value = NS(tracks_ids=[str(n) for n in range(1, 250)])
        cancelled = False
        def first_batch(ids):
            nonlocal cancelled
            cancelled = True
            return [api_track(value) for value in ids]
        self.client.tracks.side_effect = first_batch
        with self.assertRaises(ServiceError):
            self.service.liked_tracks(should_cancel=lambda: cancelled)
        self.assertEqual(self.client.tracks.call_count, 1)

    def test_playlist_uses_embedded_full_tracks_and_fetches_missing(self):
        self.service.connect()
        self.client.users_playlists.return_value = NS(
            tracks=[NS(track=api_track("12"), track_id="12:34"),
                    NS(track=None, track_id="56:34")]
        )
        self.client.tracks.return_value = [api_track("56", "Вторая")]
        tracks = self.service.playlist_tracks(Playlist("1001", "List", "42"))
        self.assertEqual([track.title for track in tracks], ["Песня", "Вторая"])

    def test_track_stream_uses_unencrypted_https_file_url(self):
        self.service.connect()
        self.client.tracks_file_info.return_value = NS(
            download_info=NS(transport="raw", codec="mp3", url="https://s50vla.storage.yandex.net/audio")
        )
        track = Track("12:34", "Песня", ("Исполнитель",))
        self.assertEqual(self.service.stream_url(track), "https://s50vla.storage.yandex.net/audio")

    def test_track_stream_accepts_supported_aac(self):
        self.service.connect()
        self.client.tracks_file_info.return_value = NS(
            download_info=NS(transport="raw", codec="aac", url="https://s50vla.storage.yandex.net/audio")
        )
        self.assertEqual(self.service.stream_url(Track("12", "Song", ())),
                         "https://s50vla.storage.yandex.net/audio")

    def test_track_stream_rejects_an_unrequested_codec(self):
        self.service.connect()
        self.client.tracks_file_info.return_value = NS(
            download_info=NS(transport="raw", codec="unknown", url="https://s50vla.storage.yandex.net/audio")
        )
        self.client.tracks_download_info.return_value = []
        with self.assertRaises(ServiceError):
            self.service.stream_url(Track("12", "Song", ()))

    def test_track_stream_falls_back_to_legacy_download_info(self):
        self.service.connect()
        self.client.tracks_file_info.side_effect = RuntimeError("endpoint unavailable")
        info = NS(codec="mp3", bitrate_in_kbps=192, preview=False,
                  download_info_url="https://s50vla.storage.yandex.net/info",
                  get_direct_link=lambda **kwargs: "https://s50vla.storage.yandex.net/audio")
        self.client.tracks_download_info.return_value = [info]
        self.assertEqual(self.service.stream_url(Track("12", "Песня", ())),
                         "https://s50vla.storage.yandex.net/audio")

    def test_backslash_in_download_host_cannot_reach_local_network(self):
        self.service.connect()
        self.client.tracks_file_info.side_effect = RuntimeError('unavailable')
        download = MagicMock(codec='mp3', bitrate_in_kbps=192, preview=False,
                             download_info_url=r'https://127.0.0.1\.yandex.net/info')
        self.client.tracks_download_info.return_value = [download]
        with self.assertRaises(ServiceError):
            self.service.stream_url(Track('12', 'Song', ()))
        download.get_direct_link.assert_not_called()

    def test_track_stream_does_not_play_insecure_urls(self):
        self.service.connect()
        self.client.tracks_file_info.return_value = NS(
            download_info=NS(transport="raw", url="http://s50vla.storage.yandex.net/audio")
        )
        self.client.tracks_download_info.return_value = []
        with self.assertRaises(ServiceError):
            self.service.stream_url(Track("12", "Песня", ()))

    def test_track_stream_does_not_play_encrypted_data_as_audio(self):
        self.service.connect()
        self.client.tracks_file_info.return_value = NS(
            download_info=NS(transport="encraw", url="https://s50vla.storage.yandex.net/audio")
        )
        self.client.tracks_download_info.return_value = []
        with self.assertRaises(ServiceError):
            self.service.stream_url(Track("12", "Песня", ()))

    def test_liking_failure_is_reported(self):
        self.service.connect()
        self.client.users_likes_tracks_add.return_value = False
        with self.assertRaises(ServiceError):
            self.service.set_liked(Track("12:34", "Песня", ()), True)

    def test_unlike_uses_track_id(self):
        self.service.connect()
        self.client.users_likes_tracks_remove.return_value = True
        self.service.set_liked(Track("12:34", "Песня", ()), False)
        self.client.users_likes_tracks_remove.assert_called_once_with("12")

    def test_device_flow_provides_code_and_returns_token(self):
        received = []
        self.client.request_device_code.return_value = NS(
            verification_url="https://oauth.yandex.ru/verification_code", user_code="ABCD",
            device_code="device-secret", interval=60, expires_in=300)
        self.client.poll_device_token.return_value = NS(access_token="secret")
        token = MusicService.device_login(lambda url, code: received.append((url, code)))
        self.assertEqual(received, [("https://oauth.yandex.ru/verification_code", "ABCD")])
        self.assertEqual(token, "secret")
        self.client.request_device_code.assert_called_once_with(device_name="Impuls")

    def test_device_flow_rejects_untrusted_verification_url(self):
        self.client.request_device_code.return_value = NS(
            verification_url="https://evil.test/", user_code="ABCD", device_code="device-secret",
            interval=60, expires_in=300)
        with self.assertRaises(ServiceError):
            MusicService.device_login(lambda url, code: None)
        self.client.poll_device_token.assert_not_called()

    def test_device_flow_cancellation_interrupts_long_poll_interval(self):
        cancelled, first_poll, finished = Event(), Event(), Event()
        self.client.request_device_code.return_value = NS(
            verification_url="https://oauth.yandex.ru/verification_code", user_code="ABCD",
            device_code="device-secret", interval=3600, expires_in=300)
        def poll(_code):
            first_poll.set()
            return None
        self.client.poll_device_token.side_effect = poll
        errors = []
        def run():
            try:
                MusicService.device_login(lambda url, code: None, should_cancel=cancelled.is_set)
            except ServiceError as error:
                errors.append(str(error))
            finally:
                finished.set()
        thread = Thread(target=run, daemon=True)
        thread.start()
        try:
            self.assertTrue(first_poll.wait(1))
            cancelled.set()
            self.assertTrue(finished.wait(0.6), 'device login did not react to cancellation')
            self.assertTrue(errors)
            self.assertNotIn('device-secret', errors[0])
        finally:
            cancelled.set()
            thread.join(1)

    def test_device_flow_honors_expiry_during_long_poll_interval(self):
        self.client.request_device_code.return_value = NS(
            verification_url="https://oauth.yandex.ru/verification_code", user_code="ABCD",
            device_code="device-secret", interval=3600, expires_in=0.02)
        self.client.poll_device_token.return_value = None
        with self.assertRaises(ServiceError):
            MusicService.device_login(lambda url, code: None)
        self.client.poll_device_token.assert_called_once()


if __name__ == "__main__":
    unittest.main()
