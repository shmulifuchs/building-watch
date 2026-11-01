import json
from datetime import datetime, timedelta, timezone
from .calendar_utils import instant
from .config import clock


def utc(at):
    return datetime.fromtimestamp(at,timezone.utc)


class Rules:
    def __init__(self, store, visits):
        self.store, self.visits = store, visits

    def emit(self, b, key, kind, at, text, **extra):
        body = dict(building_id=b.id,building_name=b.name,destination=b.destination,
                    type=kind,created_at=utc(at).isoformat(),text=b.name + ': ' + text,**extra)
        self.store.enqueue(b.id,key,body,at)

    def media(self, b, key, kind, at, start, end, **details):
        body = dict(building_id=b.id,building_name=b.name,destination=b.destination,
                    type=kind,start=utc(start).isoformat(),end=utc(end).isoformat(),
                    duration_seconds=end-start,created_at=utc(at).isoformat(),**details)
        self.store.db.execute('INSERT OR IGNORE INTO bw_media(building,key,body,status) VALUES(?,?,?,?)',
                               (b.id,key,json.dumps(body),'awaiting_recording'))

    def healthy(self, b, s, at):
        return s.get('heartbeat') is not None and not s.get('disconnected') and at-s['heartbeat'] <= b.c['heartbeat_seconds']

    def event(self, b, s, event, at):
        kind, event_id = event['type'],event['id']
        s.setdefault('started',at)
        if kind == 'heartbeat':
            recovering = s.get('offline_reported',False)
            first_connection = s.get('heartbeat') is None
            s.update(heartbeat=at,disconnected=False,offline_reported=False)
            if recovering or (first_connection and s.get('never_connected_reported')):
                self.emit(b,'recovery:'+event_id,'camera_recovered',at,'Camera connected; usable frames are being reported.')
            return
        if kind == 'offline':
            s['disconnected'] = True
            if s.get('heartbeat') is not None:
                self.outage(b,s,at)
            return
        healthy = self.healthy(b,s,at)
        if kind in ('entry','exit','activity','suspicious_start'):
            day = utc(at).astimezone(b.zone).date().isoformat()
            wording = ('First entry detected today.' if kind == 'entry' else
                       'First person detected today; exact arrival time is unconfirmed.')
            self.emit(b,'arrival:'+day,'first_arrival',at,wording,observation_kind=kind)
            self.media(b,'arrival:'+day,'first_arrival',at,at-3,at+7,observation_kind=kind)
        if kind == 'exit':
            s['last_exit'] = dict(id=event_id,at=at,coverage_healthy=healthy)
            s['departure'] = dict(id=event_id,at=at,verified=healthy)
            s['occupancy'] = 'unconfirmed'
            self.store.audit(at,b.id,'exit_recorded',event_id=event_id,coverage_healthy=healthy,
                             occupancy='unconfirmed')
        if kind in ('entry','activity'):
            s.pop('departure',None)
            s['occupancy'] = 'person_detected'
        if kind in ('entry','activity') and not b.open_at(utc(at)) and not self.visits.expected(b.id,at):
            self.suspicious(b,s,at,'Person activity outside scheduled or approved hours',False)
        if kind == 'suspicious_start':
            self.suspicious(b,s,at,event.get('reason','Explicitly flagged activity'),True)
        if kind in ('entry','exit','activity') and s.get('suspicious'):
            s['suspicious']['last_activity'] = at
        if kind == 'suspicious_end':
            self.finish(b,s,at,'explicit_end')

    def outage(self, b, s, at):
        if not s.get('offline_reported'):
            self.emit(b,'offline:'+str(s['heartbeat']),'camera_offline',at,
                      'Previously connected camera is not responding. Occupancy cannot be verified.')
            s['offline_reported'] = True
        if s.get('departure'):
            s['departure']['verified'] = False
        if s.get('suspicious'):
            s['suspicious']['recording_gap_possible'] = True

    def suspicious(self, b, s, at, reason, manual):
        if not s.get('suspicious'):
            s['suspicious'] = dict(start=at,next_start=at-3,last_activity=at,part=1,
                                   reason=reason,manual=manual,
                                   recording_gap_possible=not self.healthy(b,s,at))
            self.emit(b,'suspicious-start:'+str(at),'suspicious_activity_started',at,
                      reason + '. Recording requested for the full event in numbered parts.')
        else:
            s['suspicious']['last_activity'] = at
            s['suspicious']['manual'] |= manual

    def chunks(self, b, current, through, final=False, reason=None):
        size = b.c['clip_chunk_seconds']
        while through > current['next_start']:
            end = min(through,current['next_start']+size)
            if not final and end-current['next_start'] < size:
                break
            part = current['part']
            self.media(b,'suspicious:'+str(current['start'])+':'+str(part),
                       'suspicious_activity',through,current['next_start'],end,
                       event_id=str(current['start']),part=part,final_part=final and end==through,
                       event_end_reason=reason,full_event=True,
                       recording_gap_possible=current['recording_gap_possible'])
            current['part'] += 1
            current['next_start'] = end

    def finish(self, b, s, at, reason):
        current = s.pop('suspicious',None)
        if current:
            self.chunks(b,current,at+3,True,reason)
            self.emit(b,'suspicious-end:'+str(current['start']),'suspicious_activity_finished',at,
                      'Flagged event closed (' + reason + '); requested footage is split into ' +
                      str(current['part']-1) + ' numbered parts.',event_end_reason=reason)

    def reminders(self, b, s, at):
        local = utc(at).astimezone(b.zone)
        previous = s.get('last_timer',at)
        earliest = max(utc(previous).astimezone(b.zone).date(),
                       local.date()-timedelta(days=b.c['reminder_catchup_days']))
        day = earliest
        while day <= local.date():
            due = datetime.combine(day,clock(b.c['checkin_time']),b.zone).timestamp()
            if previous <= due <= at:
                friday = day.weekday() == 4
                tomorrow = day+timedelta(days=1)
                if friday or b.holiday(tomorrow):
                    holidays = []
                    for n in range(1,b.c['reminder_lookahead_days']+1):
                        future = day+timedelta(days=n)
                        name = b.holiday(future)
                        if name:
                            holidays.append(name+' ('+future.isoformat()+')')
                    period = 'the upcoming weekend' if friday else 'the upcoming holiday'
                    if holidays:
                        period += ', including '+', '.join(holidays)
                    self.emit(b,'checkin:'+day.isoformat(),'schedule_checkin',at,
                              'Is anyone expected during '+period+'? Please provide dates and '
                              'arrival/departure times for operator confirmation. Normal rules '
                              'remain active until approved.',scheduled_for=utc(due).isoformat(),
                              catchup=day<local.date())
            # On first startup after reminder time, catch up today's reminder only.
            elif 'last_timer' not in s and day == local.date() and due < at:
                copy = dict(s,last_timer=due)
                self.reminders(b,copy,at)
            day += timedelta(days=1)

    def advance(self, b, s, at):
        if at < s.get('watermark',float('-inf')):
            return
        s.setdefault('started',at)
        hb = s.get('heartbeat')
        if hb is None:
            if at-s['started'] >= b.c['startup_grace_seconds'] and not s.get('never_connected_reported'):
                self.emit(b,'never-connected:'+str(s['started']),'camera_never_connected',at,
                          'Camera has not connected since monitoring started. Check initial setup.')
                s['never_connected_reported'] = True
        elif s.get('disconnected') or at-hb >= b.c['offline_alert_after_seconds']:
            self.outage(b,s,at)
        healthy = self.healthy(b,s,at)
        if not healthy:
            if s.get('departure'):
                s['departure']['verified'] = False
            if s.get('suspicious'):
                s['suspicious']['recording_gap_possible'] = True
        pending = s.get('departure')
        if pending and at-pending['at'] >= b.c['inactivity_minutes']*60:
            kind = 'possible_last_departure' if pending['verified'] and healthy else 'departure_unverified'
            text = ('Possible last departure after '+str(b.c['inactivity_minutes'])+
                    ' minutes without detected person activity. Occupancy is unconfirmed.' if kind == 'possible_last_departure'
                    else 'Exit recorded, but interrupted camera coverage prevents the inactivity check. Occupancy is unconfirmed.')
            self.emit(b,'departure:'+pending['id'],kind,at,text)
            self.media(b,'departure:'+pending['id'],kind,at,pending['at']-3,pending['at']+7,
                       recording_gap_possible=not pending['verified'])
            s.pop('departure',None)
        current = s.get('suspicious')
        if current:
            timeout = b.c['manual_timeout_seconds'] if current['manual'] else b.c['event_quiet_seconds']
            if at-current['last_activity'] >= timeout:
                end = current['last_activity']+timeout if current['manual'] else current['last_activity']
                self.finish(b,s,end,'manual_timeout' if current['manual'] else 'quiet_period')
            else:
                self.chunks(b,current,at if current['manual'] else current['last_activity'])
        self.reminders(b,s,at)
        self.visits.cleanup(b,at)
        s['last_timer'] = at
