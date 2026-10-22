"""The football backend binds its threaded server before serving requests."""
import pathlib
import sys
import unittest
from unittest import mock

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import proxy  # noqa: E402


def _server_stub(events, fail):
    class _Server:
        def __init__(self, address, handler):
            events.append(("bind", address, handler))
            if fail:
                raise OSError(98, "Address already in use")

        def serve_forever(self):
            events.append(("serve",))
            raise KeyboardInterrupt

        def shutdown(self):
            events.append(("shutdown",))

    return _Server


class StartupBindOrder(unittest.TestCase):
    def _run_main(self, fail):
        events = []
        with mock.patch.object(proxy, "ThreadingHTTPServer", _server_stub(events, fail)):
            try:
                proxy.main(port=8090, bind="127.0.0.1")
            except OSError:
                pass
        return events

    def test_bind_failure_does_not_enter_serve_loop(self):
        events = self._run_main(fail=True)
        self.assertEqual(["bind"], [event[0] for event in events])
        self.assertEqual(("bind", ("127.0.0.1", 8090), proxy.Handler), events[0])

    def test_successful_bind_enters_and_shuts_down_serve_loop(self):
        events = self._run_main(fail=False)
        self.assertEqual(["bind", "serve", "shutdown"], [event[0] for event in events])
        self.assertEqual(("bind", ("127.0.0.1", 8090), proxy.Handler), events[0])


if __name__ == "__main__":
    unittest.main()
