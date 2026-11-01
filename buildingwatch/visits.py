import hashlib
import hmac
import math
import re
from .calendar_utils import instant


class Visits:
    """Approval is a trusted operator operation; camera inputs can never create visits."""
    def __init__(self, store, buildings, operators=None):
        self.store, self.buildings = store, buildings
        self.operators = {} if operators is None else operators
        if not isinstance(self.operators,dict):
            raise ValueError('Operator grants must be an object')
        for name, grant in self.operators.items():
            if (not isinstance(name,str) or not name or not isinstance(grant,dict)
                    or set(grant) != {'token_sha256','buildings'}
                    or not isinstance(grant['token_sha256'],str)
                    or not re.fullmatch(r'[0-9a-f]{64}',grant['token_sha256'])
                    or not isinstance(grant['buildings'],list)
                    or any(not isinstance(b,str) or b not in buildings for b in grant['buildings'])):
                raise ValueError('Invalid operator token hash or building grants')

    def approve(self, token, building, start, end, now):
        digest = hashlib.sha256(token.encode()).hexdigest()
        actor = None
        for name, grant in self.operators.items():
            if hmac.compare_digest(digest,grant['token_sha256']) and building in grant['buildings']:
                actor = name
                break
        if actor is None:
            self.store.audit(now,building,'visit_denied',reason='unauthenticated_or_not_authorized')
            raise PermissionError('Operator credentials do not authorize this building')
        first, last = instant(start).timestamp(), instant(end).timestamp()
        if first >= last or last <= now:
            raise ValueError('Visit must end in the future and after its start')
        b = self.buildings[building]
        effective_end = last + b.c['visit_end_grace_minutes'] * 60
        with self.store.transaction():
            cursor = self.store.db.execute('INSERT INTO bw_visits(building,actor,start,end,effective_end,status) VALUES(?,?,?,?,?,?)',
                                           (building,actor,first,last,effective_end,'approved'))
            self.store.audit(now,building,'visit_approved',visit_id=cursor.lastrowid,actor=actor,
                             start=first,ends_at_exclusive=last,effective_end_exclusive=effective_end)
        return dict(id=cursor.lastrowid,actor=actor,start=start,ends_at_exclusive=end,
                    grace_minutes=b.c['visit_end_grace_minutes'])

    def expected(self, building, at):
        return self.store.db.execute("SELECT 1 FROM bw_visits WHERE building=? AND status='approved' AND start<=? AND effective_end>?",
                                     (building,at,at)).fetchone() is not None

    def cleanup(self, b, at):
        self.store.db.execute("UPDATE bw_visits SET status='expired' WHERE building=? AND effective_end<=?",(b.id,at))
        self.store.db.execute('DELETE FROM bw_visits WHERE building=? AND effective_end<?',
                              (b.id,at - b.c['visit_retention_days'] * 86400))
