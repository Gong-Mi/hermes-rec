"""Private store and owner fixtures for the independent behavior suite."""

from pathlib import Path
from types import SimpleNamespace

from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_contract import SessionRef
from persistence.conversation.sessions import NewSession
from persistence.core.identity import SessionId
from persistence_assembly import open_persistence, profile_scope


WAIT = 8


def new_owner(case, *, source=None, stored_source="test"):
    # run.py gives each module a clean cwd; each case gets its own ledger and epoch.
    home = Path.cwd() / case._testMethodName
    home.mkdir()
    store = open_persistence(profile_scope(home=home), eager=True)
    case.addCleanup(store.close)
    ref = SessionRef(str(home), SessionId("behavior-session"))
    store.sessions.create(NewSession(session_id=ref.session_id, source=stored_source))
    epoch = store.runtime.begin_epoch(instance_id="behavior-owner")
    runner = SimpleNamespace(_draining=False, config=SimpleNamespace(multiplex_profiles=False),
                             _adapter_for_source=lambda source: None)
    authority = SessionAuthority(runner, profile_id=ref.profile_id, instance_id="behavior-owner",
                                 epoch=epoch, persistence=store)
    live = authority.sessions[ref.session_id] = LiveSession(source, "behavior-route")
    return store, authority, ref, live
