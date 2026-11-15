# Stage 27 — sleep and feedback core

Added `runtime_control.py` with persistent per-node/per-module modes, owner-only policy mutation, priority-based work admission, and feedback tracking with moderation. `tests/test_stage27_runtime.py` exercises defaults, isolation, deep/emergency sleep, owner permissions, and feedback.

## Important limitations
- This module is **not yet wired into Telegram, Mini App, workers, scheduler, or hierarchy transport**. Workers must explicitly call `can_run`; without that integration, setting a mode will not stop a worker.
- Sleep means logical job admission, not shutting down a VM. Emergency mode rejects all work; only the local owner may restore it.
- Parent bots do not automatically receive sleep or feedback management permissions. Authentication and transport remain separate integration work.
- Existing `notification_modes.py` still handles its older settings; migration/unification remains necessary.
- No server was deployed and no live Telegram end-to-end test was run.
