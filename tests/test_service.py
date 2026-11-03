"""The API adapter is tested without contacting Yandex or using credentials."""

import gc
import unittest
import weakref
from threading import Event, Thread
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from impuls_player.service import AudioStream, CollectionSnapshot, MusicService, Playlist, ServiceError, Track


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
        self.client.me.account.uid = 42
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

    def test_invalidate_cache_refreshes_likes_but_keeps_older_view_snapshot(self):
        self.service.connect()
        old_view, new_view = CollectionSnapshot(), CollectionSnapshot()
        self.client.users_likes_tracks.return_value = NS(tracks_ids=['12', '13'])
        self.client.tracks.side_effect = lambda ids: [api_track(identifier) for identifier in ids]
        self.assertEqual(self.service.liked_tracks_page(limit=1, snapshot=old_view)[0][0].id, '12:34')
        self.client.users_likes_tracks.return_value = NS(tracks_ids=['20'])
        self.service.invalidate_cache()
        self.assertEqual(self.service.liked_tracks_page(limit=1, snapshot=new_view)[0][0].id, '20:34')
        self.assertEqual(self.service.liked_tracks_page(offset=1, limit=1, snapshot=old_view)[0][0].id, '13:34')

    def test_invalidate_cache_refreshes_playlist_source_and_track_metadata(self):
        self.service.connect()
        playlist = Playlist('1', 'Playlist', '42')
        old_view, new_view = CollectionSnapshot(), CollectionSnapshot()
        self.client.users_playlists.return_value = NS(tracks=[NS(track_id='12', track=None), NS(track_id='13', track=None)])
        self.client.tracks.side_effect = lambda ids: [api_track(identifier, 'Old') for identifier in ids]
        self.assertEqual(self.service.playlist_tracks_page(playlist, limit=1, snapshot=old_view)[0][0].title, 'Old')
        self.client.users_playlists.return_value = NS(tracks=[NS(track_id='12', track=None)])
        self.client.tracks.side_effect = lambda ids: [api_track(identifier, 'Fresh') for identifier in ids]
        self.service.invalidate_cache()
        tracks, more = self.service.playlist_tracks_page(playlist, limit=1, snapshot=new_view)
        self.assertEqual(tracks[0].title, 'Fresh')
        self.assertFalse(more)
        self.assertEqual(self.service.playlist_tracks_page(playlist, offset=1, limit=1, snapshot=old_view)[0][0].id, '13:34')

    def test_connect_rejects_missing_or_invalid_uid(self):
        for uid in (None, '', '1:2', 'bad', True):
            self.client.me.account.uid = uid
            with self.subTest(uid=uid), self.assertRaises(ServiceError):
                self.service.connect()

    def test_playlist_response_started_before_refresh_cannot_cache_stale_entries(self):
        self.service.connect()
        playlist = Playlist('1', 'Playlist', '42')

        def old_response(*args, **kwargs):
            self.service.invalidate_cache()
            return NS(tracks=[NS(track=api_track('12', 'Old'))])

        self.client.users_playlists.side_effect = old_response
        old_tracks, _ = self.service.playlist_tracks_page(playlist, snapshot=CollectionSnapshot())
        self.assertEqual(old_tracks[0].title, 'Old')
        self.client.users_playlists.side_effect = None
        self.client.users_playlists.return_value = NS(tracks=[NS(track=api_track('20', 'Fresh'))])
        fresh_tracks, _ = self.service.playlist_tracks_page(playlist, snapshot=CollectionSnapshot())
        self.assertEqual(fresh_tracks[0].title, 'Fresh')

    def test_metadata_response_started_before_refresh_cannot_cache_stale_track(self):
        self.service.connect()
        self.client.users_likes_tracks.return_value = NS(tracks_ids=['12'])

        def old_response(identifiers):
            self.service.invalidate_cache()
            return [api_track('12', 'Old')]

        self.client.tracks.side_effect = old_response
        old_tracks, _ = self.service.liked_tracks_page(snapshot=CollectionSnapshot())
        self.assertEqual(old_tracks[0].title, 'Old')
        self.client.tracks.side_effect = None
        self.client.tracks.return_value = [api_track('12', 'Fresh')]
        fresh_tracks, _ = self.service.liked_tracks_page(snapshot=CollectionSnapshot())
        self.assertEqual(fresh_tracks[0].title, 'Fresh')

    def test_playback_service_creates_separate_client_without_initializing_account(self):
        playback_client = MagicMock()
        playback_client.tracks_file_info.return_value = NS(
            download_info=NS(transport="raw", codec="mp3", url="https://s50vla.storage.yandex.net/audio"))
        with patch("impuls_player.service._new_client", side_effect=[self.client, playback_client]):
            self.service.connect()
            playback = self.service.playback_service()
            self.assertIsNot(playback, self.service)
            self.assertEqual(playback.stream_url(Track("12", "Song", ())),
                             "https://s50vla.storage.yandex.net/audio")
        self.client.init.assert_called_once()
        playback_client.init.assert_not_called()
        self.client.tracks_file_info.assert_not_called()

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
        self.client.users_playlists_list.assert_called_once_with(user_id="42")

    def test_invalid_playlist_listing_is_not_a_silent_empty_collection(self):
        self.service.connect()
        for response in (None, "invalid", [NS(kind=None, owner=None)]):
            self.client.users_playlists_list.return_value = response
            with self.subTest(response=response), self.assertRaises(ServiceError):
                self.service.playlists()
        self.client.users_playlists_list.return_value = []
        self.assertEqual(self.service.playlists(), [])

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

    def test_liked_first_page_fetches_only_its_metadata_and_preserves_duplicates(self):
        self.service.connect()
        ids = ["1:34", "2:34", "1:34"] + [f"{n}:34" for n in range(3, 206)]
        self.client.users_likes_tracks.return_value = NS(tracks_ids=ids)
        self.client.tracks.side_effect = lambda ids: [api_track(value.split(":")[0]) for value in reversed(ids)]
        tracks, more = self.service.liked_tracks_page(limit=100)
        self.assertEqual([track.id for track in tracks[:3]], ["1:34", "2:34", "1:34"])
        self.assertEqual(len(tracks), 100)
        self.assertTrue(more)
        self.assertEqual(self.client.users_likes_tracks.call_count, 1)
        self.client.users_likes_tracks.assert_called_once_with(user_id="42")
        self.assertEqual(self.client.tracks.call_count, 1)
        self.assertLessEqual(len(self.client.tracks.call_args.args[0]), 100)
        self.assertNotIn("205:34", self.client.tracks.call_args.args[0])

    def test_playlist_pages_fetch_only_requested_metadata_and_cache_playlist(self):
        self.service.connect()
        self.client.users_playlists.return_value = NS(tracks=[
            NS(track=None, track_id=f"{number}:34") for number in range(1, 206)
        ])
        self.client.tracks.side_effect = lambda ids: [api_track(value.split(":")[0]) for value in ids]
        playlist = Playlist("1001", "List", "42")
        first, more = self.service.playlist_tracks_page(playlist)
        self.assertEqual((len(first), more, first[0].id, first[-1].id), (100, True, "1:34", "100:34"))
        self.assertEqual(self.client.tracks.call_count, 1)
        second, more = self.service.playlist_tracks_page(playlist, offset=100)
        self.assertEqual((len(second), more, second[0].id), (100, True, "101:34"))
        self.assertEqual(self.client.users_playlists.call_count, 1)
        self.assertEqual(self.client.tracks.call_count, 2)
        last, more = self.service.playlist_tracks_page(playlist, offset=200)
        self.assertEqual((len(last), more), (5, False))

    def test_missing_collection_responses_are_errors_but_valid_empty_collections_are_not(self):
        self.service.connect()
        playlist = Playlist("1001", "List", "42")
        for response in (None, NS(), NS(tracks_ids=None)):
            self.client.users_likes_tracks.return_value = response
            with self.subTest(collection="likes", response=response), self.assertRaises(ServiceError):
                self.service.liked_tracks_page()
        for response in (None, NS(), NS(tracks=None)):
            self.client.users_playlists.return_value = response
            with self.subTest(collection="playlist", response=response), self.assertRaises(ServiceError):
                self.service.playlist_tracks_page(playlist)
        self.client.users_likes_tracks.return_value = NS(tracks_ids=[])
        self.client.users_playlists.return_value = NS(tracks=[])
        self.assertEqual(self.service.liked_tracks_page(), ([], False))
        self.assertEqual(self.service.playlist_tracks_page(playlist), ([], False))
        self.client.tracks.assert_not_called()

    def test_malformed_identifiers_and_metadata_do_not_look_like_empty_collections(self):
        self.service.connect()
        self.client.users_likes_tracks.return_value = NS(tracks_ids=["not-a-track"])
        with self.assertRaises(ServiceError):
            self.service.liked_tracks_page()
        self.client.users_likes_tracks.return_value = NS(tracks_ids=["12:34"])
        self.client.tracks.return_value = None
        with self.assertRaises(ServiceError):
            self.service.liked_tracks_page()

    def test_cache_expires_and_liking_invalidates_liked_ids(self):
        self.service.connect()
        self.client.users_likes_tracks.side_effect = [
            NS(tracks_ids=["1:34"]), NS(tracks_ids=["2:34"]), NS(tracks_ids=["3:34"])
        ]
        self.client.tracks.side_effect = lambda ids: [api_track(value.split(":")[0]) for value in ids]
        self.client.users_likes_tracks_add.return_value = True
        with patch("impuls_player.service.time.monotonic") as now:
            now.return_value = 0
            first, _ = self.service.liked_tracks_page()
            cached, _ = self.service.liked_tracks_page()
            self.assertEqual(first, cached)
            self.assertEqual(self.client.users_likes_tracks.call_count, 1)
            self.assertEqual(self.client.tracks.call_count, 1)
            self.service.set_liked(first[0], True)
            changed, _ = self.service.liked_tracks_page()
            self.assertEqual(changed[0].id, "2:34")
            now.return_value = 61
            refreshed, _ = self.service.liked_tracks_page()
            self.assertEqual(refreshed[0].id, "3:34")
            self.assertEqual(self.client.users_likes_tracks.call_count, 3)

    def test_likes_pagination_keeps_snapshot_after_liking_until_new_first_page(self):
        self.service.connect()
        self.client.users_likes_tracks.side_effect = [
            NS(tracks_ids=["1:34", "2:34", "3:34"]),
            NS(tracks_ids=["9:34", "1:34", "2:34", "3:34"]),
        ]
        self.client.tracks.side_effect = lambda ids: [api_track(value.split(":")[0]) for value in ids]
        self.client.users_likes_tracks_add.return_value = True
        first, more = self.service.liked_tracks_page(limit=2)
        self.assertEqual(([item.id for item in first], more), (["1:34", "2:34"], True))
        self.service.set_liked(Track("9", "Song", ()), True)
        second, more = self.service.liked_tracks_page(offset=2, limit=2)
        self.assertEqual(([item.id for item in second], more), (["3:34"], False))
        refreshed, more = self.service.liked_tracks_page(limit=2)
        self.assertEqual(([item.id for item in refreshed], more), (["9:34", "1:34"], True))

    def test_playlist_pagination_keeps_snapshot_after_cache_expiry(self):
        self.service.connect()
        playlist = Playlist("1001", "List", "42")
        def entries(*ids):
            return NS(tracks=[NS(track=None, track_id=f"{item}:34") for item in ids])
        self.client.users_playlists.side_effect = [entries(1, 2, 3), entries(9, 1, 2, 3)]
        self.client.tracks.side_effect = lambda ids: [api_track(value.split(":")[0]) for value in ids]
        with patch("impuls_player.service.time.monotonic") as now:
            now.return_value = 0
            first, more = self.service.playlist_tracks_page(playlist, limit=2)
            self.assertEqual(([item.id for item in first], more), (["1:34", "2:34"], True))
            now.return_value = 61
            second, more = self.service.playlist_tracks_page(playlist, offset=2, limit=2)
            self.assertEqual(([item.id for item in second], more), (["3:34"], False))
            refreshed, more = self.service.playlist_tracks_page(playlist, limit=2)
            self.assertEqual(([item.id for item in refreshed], more), (["9:34", "1:34"], True))

    def test_independent_liked_views_keep_their_own_snapshot(self):
        self.service.connect()
        self.client.users_likes_tracks.side_effect = [
            NS(tracks_ids=["1:34", "2:34", "3:34"]),
            NS(tracks_ids=["9:34", "1:34", "2:34", "3:34"]),
        ]
        self.client.tracks.side_effect = lambda ids: [api_track(value.split(":")[0]) for value in ids]
        self.client.users_likes_tracks_add.return_value = True
        old, new = CollectionSnapshot(), CollectionSnapshot()
        first, _ = self.service.liked_tracks_page(limit=2, snapshot=old)
        self.assertEqual([track.id for track in first], ["1:34", "2:34"])
        self.service.set_liked(Track("9", "Song", ()), True)
        refreshed, _ = self.service.liked_tracks_page(limit=2, snapshot=new)
        self.assertEqual([track.id for track in refreshed], ["9:34", "1:34"])
        continuation, more = self.service.liked_tracks_page(offset=2, limit=2, snapshot=old)
        self.assertEqual(([track.id for track in continuation], more), (["3:34"], False))

    def test_independent_playlist_views_keep_snapshot_and_release_it_with_view(self):
        self.service.connect()
        playlist = Playlist("1001", "List", "42")
        def entries(*ids):
            return NS(tracks=[NS(track=None, track_id=f"{item}:34") for item in ids])
        self.client.users_playlists.side_effect = [entries(1, 2, 3), entries(9, 1, 2, 3)]
        self.client.tracks.side_effect = lambda ids: [api_track(value.split(":")[0]) for value in ids]
        old, new = CollectionSnapshot(), CollectionSnapshot()
        with patch("impuls_player.service.time.monotonic") as now:
            now.return_value = 0
            self.service.playlist_tracks_page(playlist, limit=2, snapshot=old)
            now.return_value = 61
            self.service.playlist_tracks_page(playlist, limit=2, snapshot=new)
            continuation, more = self.service.playlist_tracks_page(playlist, offset=2, limit=2, snapshot=old)
        self.assertEqual(([track.id for track in continuation], more), (["3:34"], False))
        reference = weakref.ref(old)
        del old
        gc.collect()
        self.assertIsNone(reference(), 'service must not retain completed views')

    def test_playlist_and_metadata_caches_expire(self):
        self.service.connect()
        playlist = Playlist("1001", "List", "42")
        self.client.users_playlists.return_value = NS(tracks=[NS(track=None, track_id="1:34")])
        self.client.tracks.side_effect = [[api_track("1", "Old")], [api_track("1", "New")]]
        with patch("impuls_player.service.time.monotonic") as now:
            now.return_value = 5
            self.assertEqual(self.service.playlist_tracks_page(playlist)[0][0].title, "Old")
            now.return_value = 64
            self.assertEqual(self.service.playlist_tracks_page(playlist)[0][0].title, "Old")
            self.assertEqual(self.client.users_playlists.call_count, 1)
            self.assertEqual(self.client.tracks.call_count, 1)
            now.return_value = 66
            self.assertEqual(self.service.playlist_tracks_page(playlist)[0][0].title, "New")
            self.assertEqual(self.client.users_playlists.call_count, 2)
            self.assertEqual(self.client.tracks.call_count, 2)

    def test_liking_during_inflight_likes_request_does_not_restore_stale_cache(self):
        self.service.connect()
        started, release = Event(), Event()
        def first_response(**kwargs):
            started.set()
            release.wait(2)
            return NS(tracks_ids=["1:34"])
        responses = iter((first_response, lambda **kwargs: NS(tracks_ids=["2:34"])))
        self.client.users_likes_tracks.side_effect = lambda **kwargs: next(responses)(**kwargs)
        self.client.tracks.side_effect = lambda ids: [api_track(value.split(":")[0]) for value in ids]
        self.client.users_likes_tracks_add.return_value = True
        results = []
        thread = Thread(target=lambda: results.append(self.service.liked_tracks_page()), daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(1))
            self.service.set_liked(Track("2", "Song", ()), True)
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0][0][0].id, "1:34")
        self.assertEqual(self.service.liked_tracks_page()[0][0].id, "2:34")
        self.assertEqual(self.client.users_likes_tracks.call_count, 2)

    def test_cancelled_likes_does_not_fetch_metadata_after_id_response(self):
        self.service.connect()
        cancelled = Event()
        def fetch_ids(**kwargs):
            cancelled.set()
            return NS(tracks_ids=["1:34"])
        self.client.users_likes_tracks.side_effect = fetch_ids
        with self.assertRaises(ServiceError):
            self.service.liked_tracks_page(should_cancel=cancelled.is_set)
        self.client.tracks.assert_not_called()

    def test_likes_network_errors_do_not_expose_tokens_or_urls(self):
        self.service.connect()
        self.client.users_likes_tracks.side_effect = RuntimeError('test-token https://example.test/private')
        with self.assertRaises(ServiceError) as error:
            self.service.liked_tracks_page()
        self.assertNotIn('test-token', str(error.exception))
        self.assertNotIn('https://', str(error.exception))

    def test_page_rejects_invalid_bounds_and_checks_cancellation_before_network(self):
        self.service.connect()
        playlist = Playlist("1001", "List", "42")
        for offset, limit in ((-1, 100), (0, 0), (0, 101), (0.5, 10), (True, 10), (0, False)):
            with self.subTest(offset=offset, limit=limit):
                with self.assertRaises((ValueError, ServiceError)):
                    self.service.liked_tracks_page(offset, limit)
                with self.assertRaises((ValueError, ServiceError)):
                    self.service.playlist_tracks_page(playlist, offset, limit)
        with self.assertRaises(ServiceError):
            self.service.liked_tracks_page(should_cancel=lambda: True)
        with self.assertRaises(ServiceError):
            self.service.playlist_tracks_page(playlist, should_cancel=lambda: True)
        self.client.users_likes_tracks.assert_not_called()
        self.client.users_playlists.assert_not_called()

    def test_track_stream_uses_unencrypted_https_file_url(self):
        self.service.connect()
        self.client.tracks_file_info.return_value = NS(
            download_info=NS(transport="raw", codec="mp3", url="https://s50vla.storage.yandex.net/audio")
        )
        track = Track("12:34", "Песня", ("Исполнитель",))
        self.assertEqual(self.service.stream_url(track), "https://s50vla.storage.yandex.net/audio")

    def test_stream_info_uses_raw_default_codecs_and_hides_url_in_repr(self):
        self.service.connect()
        self.client.tracks_file_info.return_value = NS(download_info=NS(
            transport='raw', codec='flac-mp4', bitrate=0, quality='lossless',
            url='https://s50vla.storage.yandex.net/audio?signature=secret'))
        result = self.service.stream_info(Track('12:34', 'Song', ()), quality='lossless')
        self.assertEqual((result.codec, result.bitrate, result.quality), ('flac-mp4', 0, 'lossless'))
        self.assertEqual(result.url, 'https://s50vla.storage.yandex.net/audio?signature=secret')
        self.assertNotIn('signature=secret', repr(result))
        self.client.tracks_file_info.assert_called_once_with('12:34', quality='lossless', transport='raw')

    def test_lossless_request_does_not_label_downgraded_aac_as_lossless(self):
        self.service.connect()
        self.client.tracks_file_info.return_value = NS(download_info=NS(
            transport='raw', codec='aac-mp4', bitrate=256, quality='lossless',
            url='https://s50vla.storage.yandex.net/audio'))
        stream = self.service.stream_info(Track('12', 'Song', ()), quality='lossless')
        self.assertEqual((stream.codec, stream.bitrate, stream.quality), ('aac-mp4', 256, 'hq'))

    def test_stream_info_accepts_only_known_unencrypted_codecs(self):
        self.service.connect()
        for codec in ('flac', 'flac-mp4', 'aac', 'aac-mp4', 'he-aac', 'he-aac-mp4', 'mp3'):
            self.client.tracks_file_info.return_value = NS(download_info=NS(
                transport='raw', codec=codec, bitrate=192, quality='nq',
                url='https://s50vla.storage.yandex.net/audio'))
            with self.subTest(codec=codec):
                self.assertEqual(self.service.stream_info(Track('12', 'Song', ())).codec, codec)

    def test_stream_info_rejects_invalid_quality_without_network(self):
        self.service.connect()
        for quality in ('high', '', None, True, ['hq']):
            with self.subTest(quality=quality), self.assertRaises((ValueError, ServiceError)):
                self.service.stream_info(Track('12', 'Song', ()), quality=quality)
        self.client.tracks_file_info.assert_not_called()

    def test_legacy_mp3_fallback_selects_quality_and_reports_actual_bitrate(self):
        self.service.connect()
        self.client.tracks_file_info.side_effect = RuntimeError('unavailable')
        def candidate(bitrate):
            return NS(codec='mp3', bitrate_in_kbps=bitrate, preview=False,
                      download_info_url='https://s50vla.storage.yandex.net/info',
                      get_direct_link=lambda **kwargs: f'https://s50vla.storage.yandex.net/{bitrate}')
        self.client.tracks_download_info.return_value = [candidate(128), candidate(320), candidate(192)]
        track = Track('12', 'Song', ())
        normal = self.service.stream_info(track, quality='nq')
        high = self.service.stream_info(track, quality='hq')
        fallback = self.service.stream_info(track, quality='lossless')
        self.assertEqual((normal.codec, normal.bitrate, normal.quality), ('mp3', 192, 'nq'))
        self.assertEqual((high.codec, high.bitrate, high.quality), ('mp3', 320, 'hq'))
        self.assertEqual((fallback.codec, fallback.bitrate, fallback.quality), ('mp3', 320, 'hq'))

    def test_legacy_fallback_continues_after_unavailable_variant(self):
        self.service.connect()
        self.client.tracks_file_info.side_effect = RuntimeError('unavailable')
        broken = NS(codec='mp3', bitrate_in_kbps=320, preview=False,
                    download_info_url='https://s50vla.storage.yandex.net/info',
                    get_direct_link=MagicMock(side_effect=RuntimeError('private URL')))
        working = NS(codec='mp3', bitrate_in_kbps=192, preview=False,
                     download_info_url='https://s50vla.storage.yandex.net/info',
                     get_direct_link=lambda **kwargs: 'https://s50vla.storage.yandex.net/audio')
        self.client.tracks_download_info.return_value = [broken, working]
        stream = self.service.stream_info(Track('12', 'Song', ()), quality='hq')
        self.assertEqual((stream.bitrate, stream.quality), (192, 'nq'))

    def test_audio_stream_repr_never_includes_signed_url(self):
        result = AudioStream('https://yandex.net/?token=secret', 'mp3', 320, 'hq')
        self.assertNotIn('secret', repr(result))

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
            download_info=NS(transport="raw", codec="mp3", bitrate=320, quality='hq',
                             url="http://s50vla.storage.yandex.net/audio")
        )
        self.client.tracks_download_info.return_value = []
        with self.assertRaises(ServiceError):
            self.service.stream_url(Track("12", "Песня", ()))

    def test_track_stream_does_not_play_encrypted_data_as_audio(self):
        self.service.connect()
        self.client.tracks_file_info.return_value = NS(
            download_info=NS(transport="encraw", codec="flac-mp4", bitrate=0, quality='lossless',
                             url="https://s50vla.storage.yandex.net/audio")
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
