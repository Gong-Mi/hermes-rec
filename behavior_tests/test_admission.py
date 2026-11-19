"""A cancelled API observer is not an authorized cancellation of a durable admission."""

import asyncio
import unittest
from unittest.mock import patch

from gateway import session_finite
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_api_turn import observe_api_turn
from gateway.session_authority import admission_row
from gateway.session_contract import SessionActor, Submission
from gateway.session_runtime_workers import mutation_tasks
from support import WAIT, new_owner


class AdmissionBehavior(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, stimulus):
        # Use a mounted messaging lane so the observer test doesn't also depend on
        # provisioning a particular API route. The API waiter is origin-agnostic.
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="fixture", user_id="writer")
        store, owner, ref, live = new_owner(self, source=source, stored_source=source.platform.value)
        actor = SessionActor("writer", ref.profile_id,
                             frozenset({"session:submit", "session:control", "session:read"}), "fixture")
        frames, claims = [], []
        entered, release, observing = asyncio.Event(), asyncio.Event(), asyncio.Event()
        live.event_stream.observers.add(frames.append)
        schedule = owner._schedule
        observer = survivor = None

        async def turn(_owner, _ref, row):
            claims.append((row["admission_id"], row["request_id"], row["generation"]))
            entered.set()
            await release.wait()
            return "fixture reply"

        with patch.object(session_finite, "execute_finite_admission", turn):
            try:
                # Hold drain scheduling until the desired boundary. The observer sets its waiter
                # before calling _schedule, and does not yield before it awaits that waiter.
                with patch.object(owner, "_schedule", lambda _ref: observing.set()):
                    receipt = await owner.submit(actor, Submission("same-input", ref, {"text": "one"}, "queue"))
                    key = receipt.admission_id
                    row = store.admissions.get(admission_id=key)
                    self.assertEqual((row.status, row.generation), ("queued", None))
                    observing.clear()
                    observer = asyncio.create_task(observe_api_turn((owner, ref, admission_row(row))))
                    await asyncio.wait_for(observing.wait(), WAIT)
                    if stimulus == "cancel_queued":
                        self.assertEqual((await owner.cancel_queued(actor, ref, key)).outcome, "cancelled")
                    elif stimulus == "leave_queued":
                        observing.clear()
                        survivor = asyncio.create_task(observe_api_turn((owner, ref, admission_row(row))))
                        await asyncio.wait_for(observing.wait(), WAIT)
                        await self.leave(observer)
                        self.assertEqual(store.admissions.get(admission_id=key).status, "queued")
                schedule(ref)
                drain = live.task
                if drain is None:
                    self.fail("resuming the test drain did not schedule a task")
                if stimulus == "leave_started":
                    await asyncio.wait_for(entered.wait(), WAIT)
                    started = store.admissions.get(admission_id=key)
                    self.assertEqual(started.status, "started")
                    self.assertIsNotNone(started.generation)
                    await self.leave(observer)
                release.set()
                await asyncio.wait_for(asyncio.shield(drain), WAIT)
                row = store.admissions.get(admission_id=key)
                self.assertEqual((row.status, row.request_id), ("terminal", "same-input"))
                related = [f["params"] for f in frames if f["params"].get("admission_id") == key]
                starts = [f for f in related if f["type"] == "message.start"]
                completions = [f["payload"]["outcome"] for f in related if f["type"] == "message.complete"]
                if stimulus == "cancel_queued":
                    self.assertEqual((row.outcome, row.generation, claims, starts), ("cancelled", None, [], []))
                    result, _ = await asyncio.wait_for(observer, WAIT)
                    self.assertTrue(result["interrupted"])
                else:
                    self.assertEqual(row.outcome, "completed")
                    self.assertIsNotNone(row.generation)
                    self.assertEqual(claims, [(key, "same-input", row.generation)])
                    self.assertEqual(len(starts), 1)
                    # A fresh observer of this same admission sees its committed answer even
                    # after the first observer was cancelled. No second submit/turn is needed.
                    result, _ = await asyncio.wait_for(observe_api_turn((owner, ref, admission_row(row))), WAIT)
                    self.assertEqual(result["final_response"], "fixture reply")
                    if stimulus == "stay":
                        self.assertEqual((await asyncio.wait_for(observer, WAIT))[0], result)
                    if survivor is not None:
                        self.assertFalse(survivor.cancelled(), "one viewer cancelled another's observation")
                        self.assertEqual((await asyncio.wait_for(survivor, WAIT))[0], result)
                self.assertEqual(completions, [row.outcome])
            finally:
                release.set()
                observers = [task for task in (observer, survivor) if task is not None]
                for task in observers:
                    task.cancel()
                await asyncio.gather(*observers, return_exceptions=True)
                if live.task is not None:
                    await asyncio.wait_for(asyncio.gather(live.task, return_exceptions=True), WAIT)
                pending = mutation_tasks(owner)
                if pending:
                    await asyncio.wait_for(asyncio.gather(*pending), WAIT)

    async def leave(self, task):
        self.assertFalse(task.done())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_observer_stays_through_commit(self):
        await self.exercise("stay")

    async def test_observer_can_leave_while_queued(self):
        await self.exercise("leave_queued")

    async def test_observer_can_leave_after_claim(self):
        await self.exercise("leave_started")

    async def test_control_cancels_without_claiming(self):
        await self.exercise("cancel_queued")
