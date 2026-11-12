# Rule: No Unmanaged Background Sessions on Fixes

When investigating, debugging, or applying fixes to the MEIC codebase:

1. **NEVER spin up long-running background processes** (such as `python run.py`, `python relay.py`, or `python server.py`) using daemon commands or background task sessions behind the scenes.
2. **Only execute short, synchronous verification commands** (e.g. running specific unit test files via `pytest`, static file checks, git status, inspection).
3. **Bot Lifecycle is Operator-Managed**: The operator controls live bot process startup and shutdown via the Dashboard UI or their dedicated terminals.
4. **Prevent Orphan Locks and Port Conflicts**: Unmanaged background sessions leave orphan locks in `runtime/locks/`, collide on ports (5002, 5005), and cause memory leaks that are difficult for the operator to inspect and terminate.

