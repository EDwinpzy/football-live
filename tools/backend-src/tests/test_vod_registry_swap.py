import pathlib
import sys
import threading
import unittest
from types import SimpleNamespace


BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402
import vod_resolver  # noqa: E402


class _TrackedRegistry:
    def __init__(self, version, service_lock):
        self.version = version
        self.history = object()
        self.stats = object()
        self.close_calls = 0
        self.closed = False
        self.closed_while_service_locked = False
        self._service_lock = service_lock

    def resolve_subject(self, subject):
        return [self, subject]

    def close(self):
        self.close_calls += 1
        self.closed = True
        self.closed_while_service_locked |= self._service_lock.locked()


class _ObservedLock:
    def __init__(self):
        self._lock = threading.Lock()
        self._attempt_guard = threading.Lock()
        self._attempts = 0
        self.second_attempted = threading.Event()

    def __enter__(self):
        with self._attempt_guard:
            self._attempts += 1
            if self._attempts == 2:
                self.second_attempted.set()
        self._lock.acquire()
        return self

    def __exit__(self, *_exc):
        self._lock.release()

    def locked(self):
        return self._lock.locked()


class VodRegistrySwapTest(unittest.TestCase):
    def test_same_version_concurrent_registry_instances_commit_as_one_service_state(self):
        original_service = proxy.VOD_SERVICE
        original_lock = proxy._VOD_SERVICE_LOCK
        original_refresh = proxy._refresh_remote_vod_registry
        service_lock = _ObservedLock()
        old_registry = _TrackedRegistry(1, service_lock)
        registry_a = _TrackedRegistry(2, service_lock)
        registry_b = _TrackedRegistry(2, service_lock)
        resolver = vod_resolver.VodResolver(None, None, old_registry)
        service = SimpleNamespace(
            registry=old_registry,
            _resolve_sources=old_registry.resolve_subject,
            resolver=resolver,
        )
        first_replace_paused = threading.Event()
        release_first_replace = threading.Event()
        second_refresh_entered = threading.Event()
        release_second_refresh = threading.Event()
        second_refresh_returned = threading.Event()
        second_call_done = threading.Event()
        errors = []
        original_replace = resolver.replace_registry

        def controlled_replace(registry):
            previous = original_replace(registry)
            if registry is registry_a:
                first_replace_paused.set()
                if not release_first_replace.wait(3):
                    raise TimeoutError("first registry replacement was not released")
            return previous

        def refresh_for_thread():
            if threading.current_thread().name == "registry-swap-second":
                second_refresh_entered.set()
                if not release_second_refresh.wait(3):
                    raise TimeoutError("second registry refresh was not released")
                second_refresh_returned.set()
                return registry_b
            return registry_a

        resolver.replace_registry = controlled_replace
        proxy.VOD_SERVICE = service
        proxy._VOD_SERVICE_LOCK = service_lock
        proxy._refresh_remote_vod_registry = refresh_for_thread

        def call_service(done=None):
            try:
                proxy._get_vod_service()
            except BaseException as exc:
                errors.append(exc)
            finally:
                if done is not None:
                    done.set()

        first = threading.Thread(target=call_service, name="registry-swap-first")
        second = threading.Thread(
            target=lambda: call_service(second_call_done), name="registry-swap-second")
        try:
            first.start()
            self.assertTrue(first_replace_paused.wait(1))
            second.start()
            self.assertTrue(second_refresh_entered.wait(1))
            release_second_refresh.set()
            self.assertTrue(second_refresh_returned.wait(1))

            # The first request has changed resolver.registry but has not yet
            # committed the matching service pointers. The second request must
            # serialize behind that complete commit.
            self.assertTrue(service_lock.second_attempted.wait(1))
            self.assertFalse(second_call_done.is_set())
        finally:
            release_second_refresh.set()
            release_first_replace.set()
            first.join(timeout=3)
            second.join(timeout=3)
            proxy.VOD_SERVICE = original_service
            proxy._VOD_SERVICE_LOCK = original_lock
            proxy._refresh_remote_vod_registry = original_refresh

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        active = service.registry
        self.assertIs(active, registry_a)
        self.assertIs(resolver.registry, active)
        self.assertIs(service._resolve_sources.__self__, active)
        self.assertFalse(active.closed)
        self.assertEqual(old_registry.close_calls, 1)
        self.assertFalse(old_registry.closed_while_service_locked)
        self.assertEqual(registry_b.close_calls, 1)
        self.assertFalse(registry_b.closed_while_service_locked)


if __name__ == "__main__":
    unittest.main()
