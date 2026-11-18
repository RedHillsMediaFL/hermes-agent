"""Initial same-user ownership approval, never routine remote policy editing."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

from hermes_cli.agent_setup.models import OperatorError, identifier
from hermes_cli.agent_setup.storage import OperatorStore


def local_store():
    from hermes_constants import get_default_hermes_root
    from hermes_cli.install_identity import get_install_id
    root = get_default_hermes_root()
    # The existing installation's OS owner is the authority; never adopt a
    # different user's tree through sudo/a supplied profile path.
    if root.is_symlink() or (hasattr(os, 'getuid') and root.stat().st_uid != os.getuid()):
        raise OperatorError('operator_local_owner_required', 403)
    installation = get_install_id()
    if not installation:
        raise OperatorError('operator_authority_unavailable', 503)
    return OperatorStore(root, installation)


def command(args):
    try:
        store = local_store()
        action = args.operator_action
        if action == 'list':
            for row in store.pending():
                print(json.dumps({k: row[k] for k in ['pairing_id', 'account_id', 'device_id', 'profiles', 'expires_at']}))
            return
        if action == 'revoke':
            store.revoke(identifier(args.grant_id))
            print('Operator grant revoked.')
            return
        if not re.fullmatch(r'[0-9A-F]{8}', args.code):
            raise OperatorError('invalid_operator_code')
        row = store.pairing_by_code(args.code)
        print('Requesting account: ' + row['account_id'])
        print('Registered app instance: ' + row['device_id'])
        print('Requested profiles: ' + ', '.join(row['profiles']))
        print('Authority: recipient-policy read/write only.')
        if action == 'deny':
            store.decide(row['pairing_id'], 'denied')
            print('Pairing denied.')
            return
        if not sys.stdin.isatty() or input('Type approve to authorize exactly this request: ').strip() != 'approve':
            raise OperatorError('operator_approval_not_confirmed', 403)
        store.decide(row['pairing_id'], 'approved', tuple(row['profiles']))
        print('Device approved. Continue in Agent to finish pairing.')
    except (OperatorError, OSError, sqlite3.Error) as exc:
        print(exc.code if isinstance(exc, OperatorError) else 'operator_authority_unavailable', file=sys.stderr)
        raise SystemExit(1) from None
