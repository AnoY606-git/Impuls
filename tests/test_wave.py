"""Wave session API behavior without credentials or network requests."""

import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from impuls_player.service import ServiceError, Track
from impuls_player.wave import WaveSession


def raw_track(identifier, title):
    return NS(id=identifier, track_id=f"{identifier}:10", title=title, version=None,
              artists=[NS(name="Artist")], albums=[], duration_ms=120000, cover_uri=None)


def batch(session="session-123", number="batch-1", *tracks, unknown=False):
    return NS(radio_session_id=session, batch_id=number,
              sequence=[NS(track=track) for track in tracks], unknown_session=unknown)


class WaveSessionTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.service = MagicMock()
        self.service._connected.return_value = self.client
        self.client.rotor_session_new.return_value = batch(
            "session-123", "batch-1", raw_track("42", "Первая"), raw_track("43", "Вторая")
        )
        self.client.rotor_session_feedback_radio_started.return_value = True
        self.client.rotor_session_feedback_track_started.return_value = True
        self.client.rotor_session_feedback_track_finished.return_value = True
        self.client.rotor_session_feedback_skip.return_value = True
        self.wave = WaveSession(self.service)

    def test_starts_personal_session_and_returns_tracks(self):
        tracks = self.wave.start()
        self.assertEqual([track.title for track in tracks], ["Первая", "Вторая"])
        self.client.rotor_session_new.assert_called_once_with(["user:onyourwave"])
        self.client.rotor_session_feedback_radio_started.assert_called_once_with("session-123", "batch-1")

    def test_does_not_fetch_before_start(self):
        with self.assertRaises(ServiceError):
            self.wave.next_batch()
        self.client.rotor_session_tracks.assert_not_called()

    def test_reports_start_finish_and_skip_with_correct_batch(self):
        first, second = self.wave.start()
        self.wave.track_started(first)
        self.wave.track_finished(first, 112.5)
        self.wave.track_started(second)
        self.wave.track_skipped(second, 3.0)
        self.client.rotor_session_feedback_track_started.assert_any_call("session-123", "42:10", "batch-1")
        self.client.rotor_session_feedback_track_finished.assert_called_once_with("session-123", "42:10", 112.5, "batch-1")
        self.client.rotor_session_feedback_skip.assert_called_once_with("session-123", "43:10", 3.0, "batch-1")

    def test_next_batch_sends_recent_tracks_and_uses_new_batch_id(self):
        first, _ = self.wave.start()
        self.wave.track_started(first)
        self.client.rotor_session_tracks.return_value = batch(
            "session-123", "batch-2", raw_track("99", "Третья")
        )
        tracks = self.wave.next_batch()
        self.assertEqual([track.title for track in tracks], ["Третья"])
        self.client.rotor_session_tracks.assert_called_once_with("session-123", queue=["42:10"])
        self.wave.track_started(tracks[0])
        self.client.rotor_session_feedback_track_started.assert_called_with("session-123", "99:10", "batch-2")

    def test_feedback_remains_available_for_recent_history_across_many_batches(self):
        first, _ = self.wave.start()
        for offset in range(3):
            values = [raw_track(str(1000 + offset * 200 + i), 'Track') for i in range(200)]
            self.client.rotor_session_tracks.return_value = batch('session-123', f'batch-{offset + 2}', *values)
            self.wave.next_batch()
        self.assertTrue(self.wave.track_started(first))
        self.client.rotor_session_feedback_track_started.assert_called_with('session-123', '42:10', 'batch-1')

    def test_repeated_track_keeps_feedback_batch_for_each_occurrence(self):
        first, _ = self.wave.start()
        self.client.rotor_session_tracks.return_value = batch(
            'session-123', 'batch-2', raw_track('42', 'Первая'))
        second, = self.wave.next_batch()
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.wave.track_started(first)
        self.wave.track_started(second)
        calls = self.client.rotor_session_feedback_track_started.call_args_list
        self.assertEqual(calls[-2].args, ('session-123', '42:10', 'batch-1'))
        self.assertEqual(calls[-1].args, ('session-123', '42:10', 'batch-2'))

    def test_no_tracks_does_not_cause_automatic_retries(self):
        self.wave.start()
        self.client.rotor_session_tracks.return_value = batch("session-123", "batch-2")
        with self.assertRaises(ServiceError):
            self.wave.next_batch()
        self.assertEqual(self.client.rotor_session_tracks.call_count, 1)

    def test_unknown_session_is_reported(self):
        self.wave.start()
        self.client.rotor_session_tracks.return_value = batch("session-123", "batch-2", unknown=True)
        with self.assertRaises(ServiceError):
            self.wave.next_batch()

    def test_rejects_tracks_from_outside_session(self):
        self.wave.start()
        with self.assertRaises(ServiceError):
            self.wave.track_started(Track("100", "other", ()))
        self.client.rotor_session_feedback_track_started.assert_not_called()

    def test_does_not_send_invalid_elapsed_time(self):
        first, _ = self.wave.start()
        for invalid in (-1, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.wave.track_skipped(first, invalid)
        self.client.rotor_session_feedback_skip.assert_not_called()

    def test_broken_session_identifier_is_rejected(self):
        self.client.rotor_session_new.return_value = batch("../other", "batch-1", raw_track("42", "One"))
        with self.assertRaises(ServiceError):
            self.wave.start()
        self.client.rotor_session_feedback_radio_started.assert_not_called()

    def test_feedback_failure_does_not_expose_backend_error(self):
        first, _ = self.wave.start()
        self.client.rotor_session_feedback_track_started.side_effect = RuntimeError("private-token")
        with self.assertRaises(ServiceError) as error:
            self.wave.track_started(first)
        self.assertNotIn("private-token", str(error.exception))

    def test_missing_playable_track_is_reported(self):
        self.client.rotor_session_new.return_value = batch("session-123", "batch-1", None)
        with self.assertRaises(ServiceError):
            self.wave.start()


if __name__ == "__main__":
    unittest.main()
