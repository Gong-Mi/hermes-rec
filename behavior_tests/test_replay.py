"""A replay ring append, a transport write and a client's cursor are distinct facts."""

import asyncio
from queue import Queue
import unittest

from gateway.session_controls import AuthorityConnection
from support import WAIT, new_owner


class Sink:
    def __init__(self):
        self.accepting = True
        self.offered = Queue()

    def write(self, frame):
        params = frame["params"]
        accepted = self.accepting
        self.offered.put((params["seq"], params["payload"]["text"], accepted))
        return accepted

    async def receive(self):
        return await asyncio.to_thread(self.offered.get, True, WAIT)


class ReplayBehavior(unittest.IsolatedAsyncioTestCase):
    async def test_only_reconnecting_viewer_catches_the_gap_before_live_delivery(self):
        _store, owner, ref, live = new_owner(self)
        stream = live.event_stream
        old, reconnected, fresh = (Sink() for _ in range(3))
        old_conn, reconnect_conn, fresh_conn = (
            AuthorityConnection(owner, sink, {"user_id": "viewer"})
            for sink in (old, reconnected, fresh)
        )
        for connection in (old_conn, reconnect_conn, fresh_conn):
            self.addAsyncCleanup(connection.close)

        async def dispatch(connection, method, **params):
            reply = await asyncio.wait_for(connection.dispatch({
                "id": 1, "method": method, "params": {"session_id": ref.session_id, **params},
            }), WAIT)
            self.assertNotIn("error", reply)
            return reply["result"]

        def publish(text):
            return stream.publish(ref.session_id, {"text": text}, event_type="message.delta")

        started = await dispatch(old_conn, "session.resume", omit_messages=True)
        publish("before")
        seen, text, accepted = await old.receive()
        self.assertEqual((text, accepted), ("before", True))
        cursor = {"replay_epoch": started["replay_epoch"], "last_sequence": seen}
        replay = await dispatch(reconnect_conn, "session.events.since", **cursor)
        self.assertEqual(replay["events"], [])
        old.accepting = False
        # publish accepted work into fanout; the watched transport actually refused it.
        self.assertTrue(publish("gap"))
        self.assertEqual(await old.receive(), (seen + 1, "gap", False))
        replay = stream.since(cursor["replay_epoch"], seen)
        self.assertEqual([(e["seq"], e["payload"]["text"]) for e in replay["events"]], [(seen + 1, "gap")])

        resumed = await dispatch(reconnect_conn, "session.resume", omit_messages=True)
        # catch_up uses the reconnect's staged position and writes before attach returns.
        self.assertEqual(reconnected.offered.get_nowait(), (seen + 1, "gap", True))
        self.assertEqual(resumed["last_sequence"], seen + 1)
        await dispatch(fresh_conn, "session.resume", omit_messages=True)
        publish("live")
        self.assertEqual(await reconnected.receive(), (seen + 2, "live", True))
        self.assertEqual(await fresh.receive(), (seen + 2, "live", True))
        self.assertTrue(reconnected.offered.empty())
        self.assertTrue(fresh.offered.empty())
