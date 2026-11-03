"""Explicit opt-in storage in the desktop's Secret Service keyring."""

from threading import Timer


def _bounded_call(operation, timeout=15):
    # Secret Service may show an unlock dialog or stop responding. Gio's
    # cancellable interrupts libsecret's synchronous D-Bus operation.
    from gi.repository import Gio
    cancellable = Gio.Cancellable()
    timer = Timer(timeout, cancellable.cancel)
    timer.daemon = True
    timer.start()
    try:
        return operation(cancellable)
    finally:
        timer.cancel()


class _SecretServiceBackend:
    def __init__(self):
        import gi
        gi.require_version('Secret', '1')
        from gi.repository import Secret
        self._secret = Secret
        self._schema = Secret.Schema.new(
            'io.github.AnoY606_git.Impuls', Secret.SchemaFlags.NONE,
            {'account': Secret.SchemaAttributeType.STRING})
        self._attributes = {'account': 'yandex-music'}

    def lookup(self):
        return _bounded_call(lambda cancel: self._secret.password_lookup_sync(
            self._schema, self._attributes, cancel))

    def store(self, value):
        ok = _bounded_call(lambda cancel: self._secret.password_store_sync(
            self._schema, self._attributes, self._secret.COLLECTION_DEFAULT,
            'Impuls: Яндекс Музыка', value, cancel))
        if not ok:
            raise RuntimeError('Secret Service storage failed')

    def clear(self):
        _bounded_call(lambda cancel: self._secret.password_clear_sync(
            self._schema, self._attributes, cancel))


class SecretStore:
    def __init__(self, backend=None):
        self._backend = backend

    def _get_backend(self):
        if self._backend is None:
            self._backend = _SecretServiceBackend()
        return self._backend

    def load(self):
        return self._get_backend().lookup()

    def save(self, token):
        if not isinstance(token, str) or not token.strip():
            raise ValueError('A non-empty token is required')
        self._get_backend().store(token)

    def clear(self):
        self._get_backend().clear()
