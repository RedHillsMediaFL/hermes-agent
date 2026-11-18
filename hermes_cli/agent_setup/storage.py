"""Transactional pairing redemption and persisted, hashed, revocable grants."""
from __future__ import annotations

from contextlib import closing, contextmanager
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from hermes_cli.agent_setup.models import CAPABILITIES, OperatorError, pkce_challenge


class OperatorStore:
    def __init__(self, root: Path, installation_id: str):
        self.path = root / 'operator-delegations' / 'state.sqlite3'
        self.installation_id = installation_id

    @contextmanager
    def transaction(self):
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or self.path.is_symlink():
            raise OSError('operator storage must not be symlinked')
        os.chmod(directory, 0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute('CREATE TABLE IF NOT EXISTS pairings (id TEXT PRIMARY KEY, '
                               'installation TEXT, account TEXT, device TEXT, profiles TEXT, challenge TEXT, '
                               'code TEXT, status TEXT, expires INTEGER)')
            connection.execute('CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, '
                               'token_hash TEXT UNIQUE, installation TEXT, account TEXT, device TEXT, '
                               'profiles TEXT, expires INTEGER, revoked INTEGER NOT NULL DEFAULT 0)')
            connection.execute('CREATE TABLE IF NOT EXISTS renewals (old_id TEXT PRIMARY KEY, '
                               'operation TEXT, new_id TEXT, created INTEGER)')
            connection.execute('BEGIN IMMEDIATE')
            yield connection

    def _pair(self, connection, pairing_id, account=None):
        row = connection.execute('SELECT * FROM pairings WHERE id=? AND installation=?',
                                 (pairing_id, self.installation_id)).fetchone()
        if row is None or (account is not None and row['account'] != account):
            raise OperatorError('operator_pairing_unavailable', 404)
        return row

    def _receipt(self, row):
        status = 'expired' if row['expires'] <= time.time() else row['status']
        return {'schema_version': 1, 'installation_id': row['installation'], 'pairing_id': row['id'],
                'account_id': row['account'], 'device_id': row['device'],
                'profiles': json.loads(row['profiles']), 'capabilities': sorted(CAPABILITIES),
                'verification_code': row['code'], 'expires_at': row['expires'], 'status': status}

    def create(self, account, value):
        now = int(time.time())
        with self.transaction() as c:
            c.execute('DELETE FROM pairings WHERE expires<=?', (now,))
            count = c.execute('SELECT COUNT(*) FROM pairings WHERE account=?', (account,)).fetchone()[0]
            total = c.execute('SELECT COUNT(*) FROM pairings').fetchone()[0]
            if count >= 8 or total >= 256:
                raise OperatorError('operator_pairing_limit', 429)
            pairing_id = str(uuid.uuid4())
            c.execute('INSERT INTO pairings VALUES (?,?,?,?,?,?,?,?,?)',
                      (pairing_id, self.installation_id, account, value.device_id, json.dumps(value.profiles),
                       value.code_challenge, secrets.token_hex(4).upper(), 'pending', now + 600))
            return self._receipt(self._pair(c, pairing_id))

    def pairing(self, pairing_id, account=None):
        with self.transaction() as c:
            return self._receipt(self._pair(c, pairing_id, account))

    def decide(self, pairing_id, decision, expected_profiles=None):
        with self.transaction() as c:
            row = self._pair(c, pairing_id)
            if row['expires'] <= time.time() or row['status'] != 'pending':
                raise OperatorError('operator_pairing_not_pending', 409)
            if decision == 'approved' and tuple(json.loads(row['profiles'])) != expected_profiles:
                raise OperatorError('operator_profile_mismatch', 403)
            c.execute('UPDATE pairings SET status=? WHERE id=?', (decision, pairing_id))
            return self._receipt(self._pair(c, pairing_id))

    def redeem(self, pairing_id, account, device, verifier):
        challenge = pkce_challenge(verifier)
        with self.transaction() as c:
            row = self._pair(c, pairing_id, account)
            if (row['expires'] <= time.time() or row['status'] != 'approved'
                    or row['device'] != device or not hmac.compare_digest(row['challenge'], challenge)):
                raise OperatorError('operator_redemption_refused', 403)
            token, grant_id = 'hop1_' + secrets.token_urlsafe(32), str(uuid.uuid4())
            expiry = int(time.time()) + 30 * 86400
            c.execute('INSERT INTO grants VALUES (?,?,?,?,?,?,?,0)',
                      (grant_id, self.token_hash(token), self.installation_id, account, device,
                       row['profiles'], expiry))
            c.execute("UPDATE pairings SET status='redeemed' WHERE id=?", (pairing_id,))
            return {**self._principal(c.execute('SELECT * FROM grants WHERE id=?', (grant_id,)).fetchone()),
                    'operator_grant': token}

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    @staticmethod
    def _principal(row):
        return {'schema_version': 1, 'installation_id': row['installation'], 'grant_id': row['id'],
                'account_id': row['account'], 'device_id': row['device'],
                'profiles': json.loads(row['profiles']), 'capabilities': sorted(CAPABILITIES),
                'expires_at': row['expires']}

    def principal(self, *, token=None, grant_id=None):
        if (token is None) == (grant_id is None):
            return None
        key, value = ('token_hash', self.token_hash(token)) if token is not None else ('id', grant_id)
        with self.transaction() as c:
            row = c.execute(f'SELECT * FROM grants WHERE {key}=? AND installation=?',
                            (value, self.installation_id)).fetchone()
            if row is None or row['revoked'] or row['expires'] <= time.time():
                return None
            return self._principal(row)

    def revoke(self, grant_id):
        with self.transaction() as c:
            c.execute('WITH RECURSIVE branch(id) AS (SELECT ? UNION ALL '
                      'SELECT renewals.new_id FROM renewals JOIN branch ON renewals.old_id=branch.id) '
                      'UPDATE grants SET revoked=1 WHERE installation=? AND id IN (SELECT id FROM branch)',
                      (grant_id, self.installation_id))

    def renew(self, token, account, device, operation):
        """Rotate once; an exact lost-response replay recovers the same token for two minutes."""
        with self.transaction() as c:
            old = c.execute('SELECT * FROM grants WHERE token_hash=? AND installation=?',
                            (self.token_hash(token), self.installation_id)).fetchone()
            if old is None or old['account'] != account or old['device'] != device:
                raise OperatorError('operator_renewal_refused', 403)
            receipt = c.execute('SELECT * FROM renewals WHERE old_id=?', (old['id'],)).fetchone()
            new_token = 'hop1_' + base64.urlsafe_b64encode(hmac.digest(
                token.encode(), ('operator-renewal:' + operation).encode(), 'sha256')).decode().rstrip('=')
            if receipt is not None:
                new = c.execute('SELECT * FROM grants WHERE id=?', (receipt['new_id'],)).fetchone()
                if (receipt['operation'] != operation or receipt['created'] + 120 <= time.time()
                        or new is None or new['revoked'] or new['expires'] <= time.time()):
                    raise OperatorError('operator_renewal_refused', 403)
                return {**self._principal(new), 'operator_grant': new_token}
            if old['revoked'] or old['expires'] <= time.time():
                raise OperatorError('operator_renewal_refused', 403)
            grant_id, expiry = str(uuid.uuid4()), int(time.time()) + 30 * 86400
            c.execute('INSERT INTO grants VALUES (?,?,?,?,?,?,?,0)',
                      (grant_id, self.token_hash(new_token), self.installation_id, account,
                       device, old['profiles'], expiry))
            c.execute('UPDATE grants SET revoked=1 WHERE id=?', (old['id'],))
            c.execute('INSERT INTO renewals VALUES (?,?,?,?)',
                      (old['id'], operation, grant_id, int(time.time())))
            new = c.execute('SELECT * FROM grants WHERE id=?', (grant_id,)).fetchone()
            return {**self._principal(new), 'operator_grant': new_token}

    def pending(self):
        with self.transaction() as c:
            rows = c.execute("SELECT * FROM pairings WHERE installation=? AND status='pending' AND expires>?",
                             (self.installation_id, int(time.time()))).fetchall()
            return [self._receipt(row) for row in rows]

    def pairing_by_code(self, code):
        with self.transaction() as c:
            rows = c.execute("SELECT * FROM pairings WHERE installation=? AND code=? AND status='pending' AND expires>?",
                             (self.installation_id, code, int(time.time()))).fetchall()
            if len(rows) != 1:
                raise OperatorError('operator_pairing_unavailable', 404)
            return self._receipt(rows[0])
