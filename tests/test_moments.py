import unittest

from impuls_player.moments import MomentBook


class MomentBookTests(unittest.TestCase):
    def setUp(self):
        self.book = MomentBook()

    def test_moments_are_sorted_and_keep_millisecond_precision(self):
        self.assertEqual(self.book.positions('song'), ())
        for position in (4321, 0, 1999):
            self.assertEqual(self.book.add('song', position, 10000), 'added')
        self.assertEqual(self.book.positions('song'), (0, 1999, 4321))

    def test_duplicate_seconds_keep_the_first_position(self):
        self.assertEqual(self.book.add('song', 1999, 10000), 'added')
        self.assertEqual(self.book.add('song', 1000, 10000), 'duplicate')
        self.assertEqual(self.book.add('song', 2000, 10000), 'added')
        self.assertEqual(self.book.positions('song'), (1999, 2000))

    def test_limit_rejects_sixth_moment_but_still_recognizes_duplicates(self):
        for position in (0, 1000, 2000, 3000, 4000):
            self.assertEqual(self.book.add('song', position, 10000), 'added')
        self.assertEqual(self.book.add('song', 5000, 10000), 'full')
        self.assertEqual(self.book.add('song', 4999, 10000), 'duplicate')
        self.assertEqual(self.book.positions('song'), (0, 1000, 2000, 3000, 4000))

    def test_tracks_and_instances_have_independent_moments(self):
        self.book.add('first', 1250, 10000)
        self.book.add('second', 2500, 10000)
        self.assertEqual(self.book.positions('first'), (1250,))
        self.assertEqual(self.book.positions('second'), (2500,))
        self.assertEqual(MomentBook().positions('first'), ())

    def test_clear_is_idempotent_and_only_removes_the_requested_track(self):
        self.book.add('first', 1000, 10000)
        self.book.add('second', 2000, 10000)
        self.book.clear('first')
        self.book.clear('first')
        self.book.clear('missing')
        self.assertEqual(self.book.positions('first'), ())
        self.assertEqual(self.book.positions('second'), (2000,))
        self.assertEqual(self.book.add('first', 1001, 10000), 'added')

    def test_invalid_values_are_unavailable_and_do_not_add_moments(self):
        invalid = (
            ('', 0, 10000), (None, 0, 10000), (True, 0, 10000),
            (1, 0, 10000), ([], 0, 10000),
            ('song', -1, 10000), ('song', 10000, 10000),
            ('song', 10001, 10000), ('song', 0, 0), ('song', 0, -1),
            ('song', True, 10000), ('song', 1.0, 10000),
            ('song', '1', 10000), ('song', None, 10000),
            ('song', 0, True), ('song', 0, 10000.0),
            ('song', 0, '10000'), ('song', 0, None),
        )
        for values in invalid:
            with self.subTest(values=values):
                self.assertEqual(self.book.add(*values), 'unavailable')
                self.assertEqual(self.book.positions('song'), ())
        self.assertEqual(self.book.add('song', 9999, 10000), 'added')
        self.assertEqual(self.book.positions('song'), (9999,))

    def test_track_limit_evicts_oldest_inserted_track_only_on_successful_add(self):
        for index in range(32):
            self.assertEqual(self.book.add(str(index), 1000, 10000), 'added')
        self.assertEqual(self.book.add('0', 1001, 10000), 'duplicate')
        self.assertEqual(self.book.add('new', -1, 10000), 'unavailable')
        self.assertEqual(self.book.positions('0'), (1000,))
        self.assertEqual(self.book.add('0', 2000, 10000), 'added')
        self.assertEqual(self.book.add('new', 3000, 10000), 'added')
        self.assertEqual(self.book.positions('0'), ())
        self.assertEqual(self.book.positions('1'), (1000,))
        self.assertEqual(self.book.positions('31'), (1000,))
        self.assertEqual(self.book.positions('new'), (3000,))
        self.book.add('next', 4000, 10000)
        self.assertEqual(self.book.positions('1'), ())
        self.assertEqual(self.book.positions('2'), (1000,))

    def test_clear_frees_a_track_slot_and_readding_places_it_last(self):
        for index in range(32):
            self.book.add(str(index), 0, 10000)
        self.book.clear('0')
        self.book.add('0', 2000, 10000)
        self.book.add('new', 3000, 10000)
        self.assertEqual(self.book.positions('0'), (2000,))
        self.assertEqual(self.book.positions('1'), ())
        self.assertEqual(self.book.positions('2'), (0,))


if __name__ == '__main__':
    unittest.main()
