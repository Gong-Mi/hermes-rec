"""Finite child process for the cron handoff cases; never executes a cron job."""

import argparse
import json
import os
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("ack", "no_ack", "no_adoption"))
    parser.add_argument("--external-worker-file", type=Path, required=True)
    parser.add_argument("--ack-file", type=Path, required=True)
    args = parser.parse_args()
    # The caller isolates HOME and HERMES_HOME before starting the real launcher.
    home = Path(os.environ["HERMES_HOME"]).resolve(strict=True)
    handoffs = home / "cron" / "external-workers"
    for path in (args.external_worker_file, args.ack_file):
        if path.resolve().parent != handoffs:
            parser.error("handoff files must be in the isolated home")
    payload = json.loads(args.external_worker_file.read_text(encoding="utf-8"))
    if Path(payload["profile_home"]) != home:
        parser.error("the handoff belongs to a different home")
    if args.mode == "no_adoption":
        return 0

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from hermes_agent.compat import install
    install()
    from cron.executions import adopt_claimed_execution, finish_execution

    execution = payload["job"]["execution_id"]
    if adopt_claimed_execution(execution) is None:
        raise RuntimeError("fixture did not take ownership")
    if args.mode == "ack":
        # Publish a complete record atomically, as the production worker does.
        temp = args.ack_file.with_suffix(".tmp")
        temp.write_text(json.dumps({"execution_id": execution, "pid": os.getpid()}), encoding="utf-8")
        temp.replace(args.ack_file)
        # Keep this control alive until the dispatcher reads/unlinks the ACK.
        # Do not rely on a timed sleep winning the dispatcher's check-vs-exit race.
        deadline = time.monotonic() + 8
        while args.ack_file.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("fixture acknowledgement was not consumed")
            time.sleep(0.01)
    with (home / "fixture-effect").open("x", encoding="utf-8") as file:
        file.write(execution + "\n")
    finished = finish_execution(execution, success=True)
    if finished is None or finished["status"] != "completed":
        raise RuntimeError("fixture did not commit completion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
