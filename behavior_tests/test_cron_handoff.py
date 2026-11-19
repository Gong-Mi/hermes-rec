"""A missing launch acknowledgement does not imply the durable cron claim was never adopted."""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cron import executions, scheduler
from hermes_constants import get_hermes_home
from tools import process_registry


class CronHandoffBehavior(unittest.TestCase):
    def exercise(self, mode):
        home = get_hermes_home() / mode
        home.mkdir()
        job_id = "fixture-job"
        ledger = home / "cron" / "executions.db"
        marker = home / "fixture-effect"
        child = Path(__file__).with_name("cron_fixture.py").resolve()
        processes = []

        def fixture_dispatch(command, **_kwargs):
            argv = [sys.executable, "-IB", str(child), mode, *command[3:]]
            return process_registry.GatewayChildDispatch("degraded", argv)

        def spawn_fixture(argv, **kwargs):
            self.assertEqual(Path(argv[2]), child)
            process = subprocess.Popen(argv, **kwargs)
            processes.append(process)
            return process

        process_api = SimpleNamespace(Popen=spawn_fixture, DEVNULL=subprocess.DEVNULL,
                                      TimeoutExpired=subprocess.TimeoutExpired)
        with (patch.object(executions, "EXECUTIONS_FILE", ledger),
              patch.object(scheduler, "_get_hermes_home", lambda: home),
              patch.object(scheduler, "subprocess", process_api),
              patch.object(process_registry, "restart_safe_gateway_child_argv", fixture_dispatch),
              patch.object(scheduler, "_record_external_cron_worker",
                           wraps=scheduler._record_external_cron_worker) as record):
            self.assertTrue(scheduler.try_register_running_job(job_id))
            try:
                claim = executions.create_execution(job_id, source="behavior_fixture")
                execution = claim["id"]
                job = {"id": job_id, "execution_id": execution, "prompt": "unused"}
                if mode == "ack":
                    self.assertTrue(scheduler._launch_external_cron_worker(job))
                    record.assert_called_once_with(job_id, processes[0].pid, scope_isolated=False)
                else:
                    with self.assertRaisesRegex(RuntimeError, r"before ownership acknowledgement \(exit 0\)"):
                        scheduler._launch_external_cron_worker(job)
                    record.assert_not_called()
                self.assertEqual(len(processes), 1)
                self.assertEqual(processes[0].poll(), 0)

                terminal = executions.get_execution(execution)
                if terminal is None:
                    self.fail("execution disappeared from the ledger")
                if mode == "no_adoption":
                    self.assertEqual((terminal["status"], terminal["pid"], terminal["handoff_pending"]),
                                     ("claimed", os.getpid(), 1))
                    self.assertFalse(marker.exists())
                    failed = executions.finish_execution(execution, success=False, error="fixture did not adopt")
                    self.assertIsNotNone(failed)
                    self.assertEqual(failed and failed["status"], "failed")
                else:
                    self.assertEqual((terminal["status"], terminal["pid"], terminal["handoff_pending"]),
                                     ("completed", processes[0].pid, 0))
                    self.assertEqual(marker.read_text(encoding="utf-8"), execution + "\n")
                    self.assertIsNone(executions.finish_execution(execution, success=False, error="not observed"))
                    self.assertEqual(executions.get_execution(execution), terminal)
            finally:
                scheduler.release_running_job(job_id)
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2)

    def test_observed_ack_matches_an_adopted_and_completed_execution(self):
        self.exercise("ack")

    def test_missing_ack_does_not_erase_completed_work(self):
        self.exercise("no_ack")

    def test_same_launcher_error_without_adoption_leaves_the_dispatcher_as_owner(self):
        self.exercise("no_adoption")
