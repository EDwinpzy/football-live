import datetime as dt
import threading
import time
import unittest

from tools.selfhosted.scheduler import DatabasePublishLock, next_daily_run


class FakeRpc:
    def __init__(self):
        self.owner = None
        self.expiry = 0.0
        self.calls = []
        self.guard = threading.Lock()

    def __call__(self, name, payload):
        self.calls.append((name, payload["p_action"]))
        self.assert_name(name)
        owner = payload["p_owner"]
        action = payload["p_action"]
        with self.guard:
            now = time.monotonic()
            if action == "acquire":
                if self.owner is not None and self.expiry > now and self.owner != owner:
                    return {"locked": False}
                self.owner, self.expiry = owner, now + 30
                return {"locked": True}
            if action == "renew":
                if self.owner != owner or self.expiry <= now:
                    return {"locked": False}
                self.expiry = now + 30
                return {"locked": True}
            if action == "release":
                if self.owner == owner:
                    self.owner, self.expiry = None, 0
                    return {"released": True}
                return {"released": False}
        raise AssertionError(f"unexpected action: {action}")

    @staticmethod
    def assert_name(name):
        if name != "iptv_admin_publish_lock":
            raise AssertionError(f"unexpected RPC: {name}")


class SchedulerTests(unittest.TestCase):
    def test_next_daily_run_uses_the_next_local_occurrence(self):
        zone = dt.timezone(dt.timedelta(hours=8))
        before = dt.datetime(2026, 9, 23, 6, 29, tzinfo=zone)
        after = dt.datetime(2026, 9, 23, 6, 30, tzinfo=zone)
        self.assertEqual(next_daily_run(before, 6, 30), dt.datetime(2026, 9, 23, 6, 30, tzinfo=zone))
        self.assertEqual(next_daily_run(after, 6, 30), dt.datetime(2026, 9, 24, 6, 30, tzinfo=zone))

    def test_database_lock_blocks_second_publisher_and_releases(self):
        rpc = FakeRpc()
        first = DatabasePublishLock(rpc, heartbeat_seconds=60)
        second = DatabasePublishLock(rpc, heartbeat_seconds=60)
        with first.hold() as acquired:
            self.assertTrue(acquired)
            with second.hold() as competing:
                self.assertFalse(competing)
        self.assertIsNone(rpc.owner)
        self.assertIn(("iptv_admin_publish_lock", "release"), rpc.calls)

    def test_lock_rpc_failure_does_not_run_as_unlocked(self):
        def broken_rpc(_name, _payload):
            raise OSError("RPC unavailable")

        lock = DatabasePublishLock(broken_rpc, heartbeat_seconds=60)
        with self.assertRaises(OSError):
            with lock.hold():
                self.fail("publisher must not run if the shared lock is unavailable")

    def test_active_lease_handle_reports_heartbeat_loss_before_publication(self):
        class LostOnRenewRpc(FakeRpc):
            def __init__(self):
                super().__init__()
                self.renew_attempted = threading.Event()

            def __call__(self, name, payload):
                if payload["p_action"] == "renew":
                    self.renew_attempted.set()
                    return {"locked": False}
                return super().__call__(name, payload)

        rpc = LostOnRenewRpc()
        lock = DatabasePublishLock(rpc, heartbeat_seconds=0.1)
        with lock.hold() as lease:
            self.assertTrue(callable(getattr(lease, "assert_valid", None)))
            self.assertTrue(rpc.renew_attempted.wait(timeout=2), "heartbeat did not attempt renewal")
            with self.assertRaisesRegex(RuntimeError, "IPTV publish lease was lost"):
                lease.assert_valid()


if __name__ == "__main__":
    unittest.main()
