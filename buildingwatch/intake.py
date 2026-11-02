import hashlib
import json
import math
from .calendar_utils import instant


KINDS = {'entry','exit','activity','heartbeat','offline','suspicious_start','suspicious_end'}
ALLOWED = {'id','building','type','at','reason'}


class Intake:
    def __init__(self, store, buildings, rules, max_future_seconds=30):
        self.store, self.buildings, self.rules = store, buildings, rules
        self.max_future_seconds = max_future_seconds

    def ingest(self, event, received_at, replay=False):
        db = self.store.db
        with self.store.transaction():
            try:
                if not isinstance(event,dict) or set(event) - ALLOWED:
                    raise ValueError('Unsupported fields; visit approvals are a separate authenticated operation')
                if event.get('building') not in self.buildings:
                    raise ValueError('Unknown building')
                if event.get('type') not in KINDS:
                    raise ValueError('Unsupported camera event type')
                if not isinstance(event.get('id'),str) or not 0 < len(event['id']) <= 256:
                    raise ValueError('Event ID must be a nonempty string of at most 256 characters')
                parsed_at = instant(event['at'])
                if not 1900 <= parsed_at.year <= 9997:
                    raise ValueError('Event timestamp year must be between 1900 and 9997')
                at = parsed_at.timestamp()
                if 'reason' in event and (not isinstance(event['reason'],str) or len(event['reason']) > 1000):
                    raise ValueError('Reason must be a string of at most 1000 characters')
                normalized = {**event,'at':parsed_at.isoformat()}
                body = json.dumps(normalized,sort_keys=True,separators=(',',':'))
                digest = hashlib.sha256(body.encode()).hexdigest()
            except (ValueError,TypeError,KeyError,AttributeError,OverflowError,OSError) as error:
                building = event.get('building') if isinstance(event,dict) else None
                building = building if isinstance(building,str) else None
                self.store.audit(received_at,building,'quarantine_invalid',reason=str(error))
                return 'invalid'
            prior = db.execute('SELECT digest FROM bw_events WHERE building=? AND id=?',
                               (event['building'],event['id'])).fetchone()
            if prior:
                if prior['digest'] == digest:
                    return 'duplicate'
                self.store.audit(received_at,event['building'],'id_collision',event_id=event['id'],
                                 original_digest=prior['digest'],conflicting_digest=digest)
                return 'collision'
            state = self.store.state(event['building'])
            reason = None
            if not replay and at > received_at + self.max_future_seconds:
                reason = 'future_timestamp'
            if at < state.get('watermark',float('-inf')):
                reason = 'late_beyond_buffer'
            status = 'quarantined' if reason else 'pending'
            db.execute('INSERT INTO bw_events VALUES(?,?,?,?,?,?)',
                       (event['building'],event['id'],at,digest,body,status))
            if reason:
                self.store.audit(received_at,event['building'],reason,event_id=event['id'],event_at=at)
                return 'quarantined'
            return 'buffered'

    def drain(self, cutoff):
        rows = self.store.db.execute("SELECT * FROM bw_events WHERE status='pending' AND at<=? ORDER BY at, CASE WHEN json_extract(body,'$.type')='heartbeat' THEN 0 ELSE 1 END, rowid",(cutoff,)).fetchall()
        for row in rows:
            with self.store.transaction():
                if row['building'] not in self.buildings:
                    self.store.db.execute("UPDATE bw_events SET status='quarantined' WHERE building=? AND id=?",(row['building'],row['id']))
                    self.store.audit(cutoff,row['building'],'building_removed',event_id=row['id'])
                    continue
                event = json.loads(row['body'])
                try:
                    event_year = instant(event['at']).year
                    if not 1900 <= event_year <= 9997:
                        raise ValueError('Event timestamp year must be between 1900 and 9997')
                except (ValueError,TypeError,KeyError,AttributeError,OverflowError,OSError) as error:
                    self.store.db.execute("UPDATE bw_events SET status='quarantined' WHERE building=? AND id=?",(row['building'],row['id']))
                    self.store.audit(cutoff,row['building'],'quarantine_invalid',event_id=row['id'],reason=str(error))
                    continue
                state = self.store.state(row['building'])
                if row['at'] < state.get('watermark',float('-inf')):
                    self.store.db.execute("UPDATE bw_events SET status='quarantined' WHERE building=? AND id=?",(row['building'],row['id']))
                    self.store.audit(cutoff,row['building'],'late_beyond_buffer',event_id=row['id'])
                    continue
                # Advance timers to each event first, so restart catch-up respects gaps between heartbeats.
                before = max(state.get('watermark',row['at']-0.000001),row['at']-0.000001)
                self.rules.advance(self.buildings[row['building']],state,before)
                self.rules.event(self.buildings[row['building']],state,event,row['at'])
                state['watermark'] = row['at']
                self.store.save(row['building'],state)
                self.store.db.execute("UPDATE bw_events SET status='processed' WHERE building=? AND id=?",(row['building'],row['id']))

    def quarantine_line(self, at, reason):
        self.store.audit(at,None,'quarantine_invalid_line',reason=reason)
