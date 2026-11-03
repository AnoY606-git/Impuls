import unittest
from dataclasses import dataclass
import random

from impuls_player.queue import PlaybackQueue


@dataclass(frozen=True)
class Track:
    id: str


class ReverseRandom:
    def shuffle(self, items):
        items.reverse()


class PlaybackQueueTests(unittest.TestCase):
    def setUp(self):
        self.tracks = [Track('one'), Track('two'), Track('three')]
        self.queue = PlaybackQueue()

    def test_selecting_a_track_starts_from_the_displayed_list(self):
        self.queue.replace(self.tracks, self.tracks[1])
        self.assertEqual(self.queue.current, self.tracks[1])
        self.assertEqual(self.queue.previous(), self.tracks[0])
        self.assertEqual(self.queue.next(), self.tracks[1])
        self.assertEqual(self.queue.next(), self.tracks[2])

    def test_appending_a_wave_batch_preserves_previous_tracks(self):
        first, second, third = Track('first'), Track('second'), Track('third')
        self.queue.replace([first, second], first)
        self.assertEqual(self.queue.next(), second)
        self.queue.append_wave([third])
        self.assertEqual(self.queue.next(), third)
        self.assertEqual(self.queue.previous(), second)

    def test_selecting_second_duplicate_keeps_its_list_position(self):
        duplicate, following = Track('same'), Track('following')
        self.queue.replace([duplicate, duplicate, following], duplicate, index=1)
        self.assertEqual(self.queue.next(), following)

    def test_end_does_not_repeat_or_advance_past_last_track(self):
        self.queue.replace(self.tracks, self.tracks[-1])
        self.assertIsNone(self.queue.next())
        self.assertEqual(self.queue.current, self.tracks[-1])

    def test_previous_at_beginning_does_not_wrap(self):
        self.queue.replace(self.tracks, self.tracks[0])
        self.assertIsNone(self.queue.previous())
        self.assertEqual(self.queue.current, self.tracks[0])

    def test_selecting_track_outside_list_makes_single_track_queue(self):
        other = Track('other')
        self.queue.replace(self.tracks, other)
        self.assertEqual(self.queue.current, other)
        self.assertIsNone(self.queue.next())

    def test_clear_forgets_the_queue(self):
        self.queue.replace(self.tracks, self.tracks[0])
        self.queue.clear()
        self.assertIsNone(self.queue.current)
        self.assertIsNone(self.queue.next())

    def test_shuffle_plays_selected_first_and_each_other_occurrence_once(self):
        queue = PlaybackQueue(randomizer=ReverseRandom())
        queue.set_shuffle(True)
        queue.replace(self.tracks, self.tracks[1])
        self.assertEqual(queue.current, self.tracks[1])
        self.assertEqual(queue.next(), self.tracks[2])
        self.assertEqual(queue.next(), self.tracks[0])
        self.assertIsNone(queue.next())

    def test_shuffle_previous_and_next_follow_playback_history(self):
        queue = PlaybackQueue(randomizer=ReverseRandom())
        queue.set_shuffle(True)
        queue.replace(self.tracks, self.tracks[1])
        queue.next()
        queue.next()
        self.assertEqual(queue.previous(), self.tracks[2])
        self.assertEqual(queue.previous(), self.tracks[1])
        self.assertIsNone(queue.previous())
        self.assertEqual(queue.next(), self.tracks[2])
        self.assertEqual(queue.next(), self.tracks[0])
        self.assertIsNone(queue.next())

    def test_repeat_wraps_whole_list_and_previous_returns_to_last_played(self):
        self.queue.set_repeat(True)
        self.queue.replace(self.tracks, self.tracks[1])
        self.assertEqual(self.queue.next(), self.tracks[2])
        self.assertEqual(self.queue.next(), self.tracks[0])
        self.assertEqual(self.queue.previous(), self.tracks[2])
        self.assertEqual(self.queue.next(), self.tracks[0])
        self.assertEqual(self.queue.next(), self.tracks[1])

    def test_shuffle_repeat_starts_new_cycle_without_immediate_repetition(self):
        queue = PlaybackQueue(randomizer=ReverseRandom())
        queue.set_shuffle(True)
        queue.set_repeat(True)
        queue.replace(self.tracks, self.tracks[1])
        first_cycle = [queue.current, queue.next(), queue.next()]
        second_cycle = [queue.next(), queue.next(), queue.next()]
        self.assertEqual(set(first_cycle), set(self.tracks))
        self.assertEqual(set(second_cycle), set(self.tracks))
        self.assertNotEqual(first_cycle[-1], second_cycle[0])
        self.assertEqual(queue.previous(), second_cycle[1])

    def test_changing_modes_keeps_current_and_can_resume_natural_order(self):
        queue = PlaybackQueue(randomizer=ReverseRandom())
        queue.replace(self.tracks, self.tracks[1])
        queue.set_shuffle(True)
        queue.set_repeat(True)
        self.assertEqual(queue.current, self.tracks[1])
        self.assertEqual(queue.next(), self.tracks[2])
        queue.set_shuffle(False)
        self.assertEqual(queue.current, self.tracks[2])
        self.assertEqual(queue.next(), self.tracks[0])

    def test_duplicate_tracks_remain_separate_queue_entries(self):
        duplicate = Track('duplicate')
        queue = PlaybackQueue(randomizer=ReverseRandom())
        queue.set_shuffle(True)
        queue.replace([duplicate, duplicate, Track('other')], duplicate)
        heard = [queue.current, queue.next(), queue.next()]
        self.assertEqual(heard.count(duplicate), 2)
        self.assertIsNone(queue.next())

    def test_mode_settings_survive_clear_and_apply_to_new_list(self):
        queue = PlaybackQueue(randomizer=ReverseRandom())
        queue.set_shuffle(True)
        queue.set_repeat(True)
        queue.replace(self.tracks, self.tracks[0])
        queue.clear()
        queue.replace(self.tracks, self.tracks[1])
        self.assertEqual(queue.current, self.tracks[1])
        self.assertEqual(queue.next(), self.tracks[2])
        self.assertEqual(queue.next(), self.tracks[0])
        self.assertIsNotNone(queue.next())

    def test_shuffle_visits_each_entry_for_multiple_seeds_and_initial_selections(self):
        tracks = [Track(str(i)) for i in range(8)]
        for seed in range(10):
            for selected in tracks:
                with self.subTest(seed=seed, selected=selected):
                    queue = PlaybackQueue(randomizer=random.Random(seed))
                    queue.set_shuffle(True)
                    queue.replace(tracks, selected)
                    played = [queue.current]
                    while (track := queue.next()) is not None:
                        played.append(track)
                    self.assertEqual(played[0], selected)
                    self.assertEqual(len(played), len(tracks))
                    self.assertEqual(set(played), set(tracks))


if __name__ == '__main__':
    unittest.main()
