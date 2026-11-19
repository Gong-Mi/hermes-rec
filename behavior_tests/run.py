"""Run only the independent behavior suites in fresh, credential-free processes."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


HERE = Path(__file__).resolve().parent
SUITES = ("test_admission", "test_receipt", "test_replay", "test_cron_handoff")
_CHILD = """
import sys
import unittest
from pathlib import Path

path = Path(sys.argv[1])
sys.path[:0] = [str(path.parent.parent), str(path.parent)]

def check_socket(event, args):
    # The receipt fixture needs loopback only. Reject unexpected Python
    # socket calls before a product import; this is not an OS sandbox.
    if event == 'socket.getaddrinfo':
        address = (args[0],)
    elif event in ('socket.bind', 'socket.connect'):
        address = args[1]
    else:
        return
    if (path.stem != 'test_receipt' or not isinstance(address, tuple)
            or address[0] != '127.0.0.1'):
        raise RuntimeError('unexpected socket access in a behavior fixture')

sys.addaudithook(check_socket)
from hermes_agent.compat import install
install()
suite = unittest.defaultTestLoader.loadTestsFromName(path.stem)
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.testsRun and result.wasSuccessful() and not (
    result.skipped or result.expectedFailures) else 1)
"""


def run_one(name):
    path = HERE / f"{name}.py"
    if not path.is_file():
        print(f"Missing suite: {path.name}", file=sys.stderr)
        return False
    with tempfile.TemporaryDirectory(prefix="hermes-behavior-") as directory:
        scratch = Path(directory)
        home, state, tmp = (scratch / part for part in ("home", "state", "tmp"))
        for subdir in (home, state, tmp):
            subdir.mkdir()
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
        env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(state),
                   TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp), TZ="UTC")
        print(f"=== {path.name} ===", flush=True)
        try:
            result = subprocess.run(
                [sys.executable, "-IBu", "-X", f"pycache_prefix={scratch / 'pycache'}",
                 "-c", _CHILD, str(path)],
                cwd=scratch, env=env, stdin=subprocess.DEVNULL, timeout=45, check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"Suite timed out: {path.name}", file=sys.stderr)
            return False
        return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", nargs="*", metavar="SUITE", help=", ".join(SUITES))
    names = parser.parse_args().suite or SUITES
    if any(name not in SUITES for name in names) or len(names) != len(set(names)):
        parser.error("select distinct suite names from: " + ", ".join(SUITES))
    passed = sum(run_one(name) for name in names)
    print(f"Behavior suites: {passed}/{len(names)} passed", flush=True)
    return 0 if passed == len(names) else 1


if __name__ == "__main__":
    raise SystemExit(main())
