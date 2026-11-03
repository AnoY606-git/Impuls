import unittest
from threading import Event
from impuls_player.secrets import SecretStore


class Backend:
    def __init__(self): self.value = None
    def lookup(self): return self.value
    def store(self, value): self.value = value
    def clear(self): self.value = None


class SecretStoreTests(unittest.TestCase):
    def test_store_lookup_and_delete(self):
        backend = Backend()
        store = SecretStore(backend)
        store.save('token')
        self.assertEqual(store.load(), 'token')
        store.clear()
        self.assertIsNone(store.load())

    def test_rejects_empty_tokens(self):
        with self.assertRaises(ValueError):
            SecretStore(Backend()).save('  ')

    def test_backend_failures_are_not_replaced_with_insecure_fallback(self):
        class BrokenBackend(Backend):
            def store(self, value): raise RuntimeError('secret service unavailable')
        with self.assertRaises(RuntimeError):
            SecretStore(BrokenBackend()).save('token')

class SecretTimeoutTests(unittest.TestCase):
    def test_secret_service_call_is_cancelled_after_deadline(self):
        from impuls_player.secrets import _bounded_call
        cancelled = Event()
        def blocked(cancellable):
            cancellable.connect(lambda *_: cancelled.set())
            self.assertTrue(cancelled.wait(1))
            self.assertTrue(cancellable.is_cancelled())
        _bounded_call(blocked, timeout=0.02)

if __name__ == '__main__': unittest.main()
