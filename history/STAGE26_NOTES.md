# OrionAI Stage 26 — hierarchical node core

## Implemented
- `hierarchy.py`: persistent per-instance identity and peers in SQLite.
- Role derived from active relationships: `standalone`, `root`, `member`, `intermediate`.
- One active parent, multiple children; explicit scoped permissions.
- HMAC-SHA256 signed command envelopes, timestamp and nonce replay protection.
- Detach/revoke and audit trail.
- Tests in `tests/test_hierarchy.py`.

## Integration example (local core only)
```python
from hierarchy import Hierarchy
node = Hierarchy('data/hierarchy.db')
node.attach('parent-node-id', 'parent', 'replace-with-unique-random-shared-secret-at-least-32-chars', ['status.read'])
print(node.status())
# In a real authenticated transport endpoint:
# command = node.authorize_command(request_body)
# Dispatch command['action'] only to an explicit allowlisted handler.
```

## NOT implemented / verified
- This module is **not wired** into manager.py, hubclient.py, Telegram buttons or Mini App.
- No automatic network discovery, provisioning, cross-server transport, secure pairing ceremony, or role switching UI.
- No cross-instance ancestry reconciliation; ancestry is validated only against information provided on attachment. Distributed cycle prevention requires trusted coordination.
- Secrets are stored as plaintext in the hierarchy SQLite file; production requires secret encryption at rest, TLS/mTLS, key rotation and secure pairing.
- No full-system end-to-end test or live deployment.
- The earlier modules and any unfinished features in Stage 25 are unchanged.

## Deployment note
This is an incremental source-code stage, not a complete production-ready hierarchical bot. Do not expose this API to the Internet without secure pairing, authorization and transport integration.
