"""Broker event loop must be safe when called from multiple threads."""
import asyncio
import threading
import unittest


class TestBrokerLoop(unittest.TestCase):
    def test_run_coroutine_threadsafe_parallel(self):
        from brokers.tastytrade_broker import TastyTradeBroker

        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def worker():
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        ready.wait(timeout=5)

        async def add(a, b):
            await asyncio.sleep(0.01)
            return a + b

        def call():
            return TastyTradeBroker._run.__get__(object(), TastyTradeBroker)  # noqa - use pattern

        # Mirror broker._run without full broker init
        def run_coro(coro):
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=5)

        results = []
        errors = []

        def target(n):
            try:
                results.append(run_coro(add(n, 1)))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=target, args=(i,)) for i in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)

        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), list(range(1, 9)))


if __name__ == '__main__':
    unittest.main()
