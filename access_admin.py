"""Server-side administrator CLI. Run over SSH, never expose via unauthenticated HTTP."""
import argparse
import datetime
import access_grants

p=argparse.ArgumentParser(description='Manage independent OrionAI bot grants')
p.add_argument('action',choices=['grant','revoke','list','status'])
p.add_argument('scope',choices=access_grants.SCOPES)
p.add_argument('user_id',nargs='?',type=int)
p.add_argument('--expires',help='UTC ISO date YYYY-MM-DD; omit for permanent access')
a=p.parse_args()
if a.action in ('grant','revoke','status') and a.user_id is None: p.error('user_id required')
if a.action in ('grant','revoke'):
    expiry=None
    if a.expires:
        expiry=int(datetime.datetime.strptime(a.expires,'%Y-%m-%d').replace(tzinfo=datetime.timezone.utc).timestamp())
    access_grants.set_grant(a.scope,a.user_id,a.action=='grant',expires_at=expiry)
    print(f'{a.action}: {a.scope} / {a.user_id}')
elif a.action=='status': print(access_grants.status(a.scope,a.user_id))
else:
    for row in access_grants.list_grants(a.scope): print(*row)
