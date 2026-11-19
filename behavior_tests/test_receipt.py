"""Exercise worker receipt delivery over loopback after an independently visible SQLite commit."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
import json
from pathlib import Path
from queue import Queue
import sqlite3
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError, WorkerRPC
from hermes_cli import gateway_client, gateway_runtime, gateway_runtime_discovery
from persistence.core.errors import AuthorityRefusal
from persistence.core.identity import SessionId
from persistence.sqlite import SQLiteBackend
from persistence.sqlite.authority._runtime_ends import begin_runtime_epoch
from persistence.sqlite.conversation._store.sessions import create_session
from persistence.sqlite.workers._executions import register_worker_execution
from persistence.sqlite.workers._receipt import mutate_worker_execution
from persistence_assembly import profile_scope
from support import WAIT


class ReceiptBehavior(unittest.TestCase):
    def exercise(self, *, drop):
        home = Path.cwd() / self._testMethodName
        home.mkdir()
        state = home / "state.db"
        session = SessionId("receipt-session")
        execution = "receipt-execution"
        committed = Queue()
        attempts = Queue()
        release = Event()

        def snapshot():
            # Read outside the owner connection/transaction: these bytes must be committed.
            with closing(sqlite3.connect(f"{state.as_uri()}?mode=ro", uri=True)) as reader:
                messages = reader.execute("SELECT id, content FROM messages WHERE session_id=? ORDER BY id",
                                          (session,)).fetchall()
                receipts = reader.execute(
                    "SELECT sequence, payload_digest, result_json FROM worker_receipts WHERE execution_id=?",
                    (execution,),
                ).fetchall()
                sequence = reader.execute("SELECT last_sequence FROM worker_executions WHERE execution_id=?",
                                          (execution,)).fetchone()[0]
                revision = reader.execute("SELECT runtime_revision FROM sessions WHERE id=?", (session,)).fetchone()[0]
            return messages, receipts, sequence, revision

        with ExitStack() as stack:
            profile = profile_scope(home=home)
            backend = SQLiteBackend({profile.hermes_home_key: state})
            stack.callback(backend.close)
            store = backend.open(profile)
            holder = store.holder()
            create_session(holder, session, "test")
            epoch = begin_runtime_epoch(holder, instance_id="fixture-owner")
            scope = dict(epoch=epoch, execution_id=execution, session_id=session, generation=0)
            register_worker_execution(holder, epoch=epoch, execution_id=execution, session_id=session,
                                      generation=0, kind="compute", adoption_secret="inert-fixture-value")
            before = snapshot()

            def owner_socket(ws):
                # The wire endpoint is a fixture; the owner's receipt transaction is not.
                for raw in ws:
                    frame = json.loads(raw)
                    reply = {"jsonrpc": "2.0", "id": frame["id"]}
                    params = frame["params"]
                    if frame["method"] != "worker.persist":
                        raise AssertionError("unexpected fixture RPC method")
                    attempts.put((ws.id, params))
                    try:
                        reply["result"] = mutate_worker_execution(holder, **params)
                    except AuthorityRefusal as exc:
                        reply["error"] = {"code": 4001, "message": exc.reason}
                    else:
                        if not release.is_set():
                            committed.put(params)
                            if not release.wait(WAIT):
                                raise TimeoutError("test did not release the response gate")
                            if drop:
                                ws.close()
                                return
                    ws.send(json.dumps(reply))

            protocol = "hermes-behavior-fixture"
            server = stack.enter_context(serve(owner_socket, "127.0.0.1", 0, close_timeout=1,
                                               subprotocols=[protocol]))
            listener = Thread(target=server.serve_forever, daemon=True)
            listener.start()
            stack.callback(listener.join, WAIT)
            stack.callback(server.shutdown)
            url = f"ws://127.0.0.1:{server.socket.getsockname()[1]}"
            # Keep WorkerRPC's WebSocket dialing, receive loop and reconnect. Only discovery,
            # identity and ticketing are fake; there is no contact with a live gateway.
            endpoint = object()
            stack.enter_context(patch.object(gateway_runtime, "discover_gateway_endpoint",
                                           return_value=SimpleNamespace(state="ready", endpoint=endpoint)))
            stack.enter_context(patch.object(gateway_runtime, "control_home_for", return_value=home))
            stack.enter_context(patch.object(gateway_runtime_discovery, "query_identify", return_value={"pid": -1}))
            stack.enter_context(patch.object(gateway_client, "_session_ticket", return_value=None))
            stack.enter_context(patch.object(gateway_client, "gateway_ws_target", return_value=(url, [protocol])))

            rpc = WorkerRPC(home)
            stack.callback(rpc.close)
            worker = RuntimeSessionStore(rpc, scope, home / "outbox")
            stack.callback(worker.close)
            pool = stack.enter_context(ThreadPoolExecutor(max_workers=1))
            stack.callback(release.set)
            messages = [{"role": "user", "content": "one durable message"}]
            call = pool.submit(worker.append_messages_batch, session, messages)
            original = committed.get(timeout=WAIT)
            first_connection, first_request = attempts.get_nowait()
            self.assertEqual(first_request, original)

            saved = snapshot()
            rows, receipts, sequence, revision = saved
            self.assertEqual([body for _, body in rows], ["one durable message"])
            self.assertEqual([row[0] for row in receipts], [1])
            self.assertEqual((sequence, revision), (1, before[3] + 1))
            receipt = json.loads(receipts[0][2])
            def pending():
                return json.loads(worker.path.read_text(encoding="utf-8"))["pending"]

            self.assertEqual([entry["sequence"] for entry in pending()], [1])
            self.assertFalse(call.done(), "commit was mistaken for a delivered response")
            release.set()

            if drop:
                with self.assertRaises(ConnectionClosed):
                    call.result(timeout=WAIT)
                self.assertEqual([entry["sequence"] for entry in pending()], [1])
                self.assertTrue(worker.failure)
                with self.assertRaises(WorkerPersistenceError):
                    worker.append_messages_batch(session, [{"role": "user", "content": "not sent"}])
                self.assertTrue(attempts.empty())
                self.assertEqual(worker.retry_pending(), [receipt])
                next_connection, replayed_request = attempts.get_nowait()
                self.assertNotEqual(next_connection, first_connection)
                self.assertEqual(replayed_request, first_request)
                self.assertIsNone(worker.failure)
                other = {"messages": [{"role": "user", "content": "different fixture payload"}],
                         "turn_lease_holder": None}
                with self.assertRaisesRegex(WorkerPersistenceError, "^admission_conflict$"):
                    rpc("worker.persist", **{**original, "payload": other})
            else:
                self.assertEqual(call.result(timeout=WAIT), 1)
                self.assertEqual(messages[0]["_row_id"], rows[0][0])
            self.assertEqual(pending(), [])
            self.assertEqual(snapshot(), saved, "receipt delivery or replay mutated the committed result")

    def test_committed_write_is_pending_until_reply(self):
        self.exercise(drop=False)

    def test_lost_reply_exact_replay_keeps_one_message_and_receipt(self):
        self.exercise(drop=True)
