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

    def test_appending_a_page_keeps_current_and_playback_history(self):
        first, second, third, fourth = [Track(str(i)) for i in range(4)]
        self.queue.replace([first, second], first)
        self.assertEqual(self.queue.next(), second)
        self.queue.append_tracks(iter([third, fourth]))
        self.assertEqual(self.queue.current, second)
        self.assertEqual(self.queue.previous(), first)
        self.assertEqual(self.queue.next(), second)
        self.assertEqual(self.queue.next(), third)
        self.assertEqual(self.queue.next(), fourth)
        self.assertIsNone(self.queue.next())

    def test_appending_to_shuffle_preserves_forward_history_and_visits_each_entry(self):
        tracks = [Track(str(i)) for i in range(5)]
        queue = PlaybackQueue(randomizer=ReverseRandom())
        queue.set_shuffle(True)
        queue.replace(tracks[:3], tracks[1])
        self.assertEqual(queue.next(), tracks[2])
        self.assertEqual(queue.previous(), tracks[1])
        queue.append_tracks(tracks[3:])
        self.assertEqual(queue.current, tracks[1])
        self.assertEqual(queue.next(), tracks[2])
        rest = [queue.next(), queue.next(), queue.next()]
        self.assertEqual(set(rest), {tracks[0], tracks[3], tracks[4]})
        self.assertIsNone(queue.next())

    def test_empty_page_keeps_pending_shuffle_order(self):
        queue = PlaybackQueue(randomizer=ReverseRandom())
        queue.set_shuffle(True)
        queue.replace(self.tracks, self.tracks[0])
        queue.append_tracks([])
        self.assertEqual(queue.next(), self.tracks[2])
        self.assertEqual(queue.next(), self.tracks[1])

    def test_appending_in_shuffle_repeat_keeps_the_remaining_cycle_complete(self):
        tracks = [Track(str(i)) for i in range(5)]
        queue = PlaybackQueue(randomizer=ReverseRandom())
        queue.set_shuffle(True)
        queue.set_repeat(True)
        queue.replace(tracks[:3], tracks[0])
        first, second = queue.current, queue.next()
        queue.append_tracks(tracks[3:])
        cycle = [first, second, queue.next(), queue.next(), queue.next()]
        self.assertEqual(set(cycle), set(tracks))
        self.assertNotEqual(cycle[-1], queue.next())

    def test_appended_page_joins_current_repeat_cycle_and_next_cycle(self):
        first, second, third = [Track(str(i)) for i in range(3)]
        self.queue.set_repeat(True)
        self.queue.replace([first, second], first)
        self.assertEqual(self.queue.next(), second)
        self.assertEqual(self.queue.next(), first)
        self.queue.append_tracks([third])
        self.assertEqual([self.queue.next(), self.queue.next(), self.queue.next()],
                         [second, third, first])

    def test_appending_duplicate_entries_does_not_collapse_them(self):
        first, duplicate = Track('first'), Track('duplicate')
        self.queue.replace([first], first)
        self.queue.append_tracks([duplicate, duplicate])
        self.assertEqual(self.queue.next(), duplicate)
        self.assertEqual(self.queue.next(), duplicate)
        self.assertIsNone(self.queue.next())

    def test_appending_a_page_requires_an_active_queue(self):
        with self.assertRaises(ValueError):
            self.queue.append_tracks([Track('orphan')])

    def test_selecting_second_duplicate_keeps_its_list_position(self):
        duplicate, following = Track('same'), Track('following')
        self.queue.replace([duplicate, duplicate, following], duplicate, index=1)
        self.assertEqual(self.queue.next(), following)

    def test_end_does_not_repeat_or_advance_past_last_track(self):
        self.queue.replace(self.tracks, self.tracks[-1])
        self.assertIsNone(self.queue.next())
        self.assertEqual(self.queue.current, self.tracks[-1])

    def test_has_next_excludes_repeat_but_includes_forward_history(self):
        first, second = self.tracks[:2]
        self.queue.set_repeat(True)
        self.queue.replace([first, second], first)
        self.assertTrue(getattr(self.queue, 'has_next', False))
        self.assertEqual(self.queue.next(), second)
        self.assertFalse(self.queue.has_next)
        self.assertEqual(self.queue.previous(), first)
        self.assertTrue(self.queue.has_next)
        self.assertEqual(self.queue.next(), second)
        self.assertEqual(self.queue.next(), first)

    def test_peek_next_reads_pending_without_moving_or_changing_history(self):
        first, second, third = self.tracks
        self.queue.replace(self.tracks, first)
        self.assertEqual(self.queue.peek_next(), second)
        self.assertEqual(self.queue.peek_next(), second)
        self.assertEqual(self.queue.current, first)
        self.assertIsNone(self.queue.previous())
        self.assertEqual(self.queue.next(), second)
        self.assertEqual(self.queue.next(), third)

    def test_peek_next_prioritizes_forward_playback_history(self):
        queue = PlaybackQueue(randomizer=ReverseRandom())
        queue.set_shuffle(True)
        queue.replace(self.tracks, self.tracks[0])
        self.assertEqual(queue.next(), self.tracks[2])
        self.assertEqual(queue.previous(), self.tracks[0])
        self.assertEqual(queue.peek_next(), self.tracks[2])
        self.assertEqual(queue.next(), self.tracks[2])
        self.assertEqual(queue.next(), self.tracks[1])

    def test_peek_next_does_not_advance_random_generator(self):
        randomizer = random.Random(42)
        queue = PlaybackQueue(randomizer=randomizer)
        queue.set_shuffle(True)
        queue.replace(self.tracks, self.tracks[0])
        state = randomizer.getstate()
        first_peek = queue.peek_next()
        self.assertEqual(queue.peek_next(), first_peek)
        self.assertEqual(randomizer.getstate(), state)
        self.assertEqual(queue.next(), first_peek)

    def test_peek_next_can_predict_ordered_repeat_without_advancing(self):
        self.queue.set_repeat(True)
        self.queue.replace(self.tracks, self.tracks[-1])
        self.assertEqual(self.queue.peek_next(), self.tracks[0])
        self.assertEqual(self.queue.current, self.tracks[-1])
        self.assertEqual(self.queue.next(), self.tracks[0])
        self.assertEqual(self.queue.previous(), self.tracks[-1])

    def test_peek_next_reserves_random_repeat_cycle_without_moving_history(self):
        randomizer = random.Random(21)
        queue = PlaybackQueue(randomizer=randomizer)
        queue.set_shuffle(True)
        queue.set_repeat(True)
        queue.replace(self.tracks, self.tracks[0])
        while queue.has_next:
            queue.next()
        before = queue.current
        reserved = queue.peek_next()
        self.assertIsNotNone(reserved)
        self.assertNotEqual(reserved, before)
        self.assertEqual(queue.peek_next(), reserved)
        self.assertEqual(queue.current, before)
        self.assertIsNotNone(queue.previous())
        self.assertEqual(queue.next(), before)
        self.assertEqual(queue.peek_next(), reserved)
        self.assertEqual(queue.next(), reserved)

    def test_appending_tracks_invalidates_reserved_repeat_cycle(self):
        queue = PlaybackQueue(randomizer=ReverseRandom())
        queue.set_shuffle(True)
        queue.set_repeat(True)
        queue.replace(self.tracks, self.tracks[0])
        while queue.has_next:
            queue.next()
        self.assertIsNotNone(queue.peek_next())
        new_track = Track('new')
        queue.append_tracks([new_track])
        self.assertEqual(queue.peek_next(), new_track)
        self.assertEqual(queue.next(), new_track)
        cycle = [queue.next() for _ in range(4)]
        self.assertEqual(set(cycle), set(self.tracks + [new_track]))
        self.assertNotEqual(cycle[0], new_track)

    def test_mode_change_discards_reserved_shuffle_cycle(self):
        queue = PlaybackQueue(randomizer=ReverseRandom())
        queue.set_shuffle(True)
        queue.set_repeat(True)
        queue.replace(self.tracks, self.tracks[0])
        while queue.has_next:
            queue.next()
        self.assertIsNotNone(queue.peek_next())
        queue.set_repeat(False)
        self.assertIsNone(queue.peek_next())
        queue.set_repeat(True)
        queue.set_shuffle(False)
        self.assertEqual(queue.peek_next(), self.tracks[2])
        self.assertEqual(queue.next(), self.tracks[2])

    def test_peek_next_on_empty_or_exhausted_queue_is_none(self):
        self.assertIsNone(self.queue.peek_next())
        self.queue.replace(self.tracks, self.tracks[-1])
        self.assertIsNone(self.queue.peek_next())

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
