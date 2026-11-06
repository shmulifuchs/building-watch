import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .calendar_utils import us_holidays


DEFAULT_HOURS = {str(i): [['09:00', '17:00']] if i < 5 else [] for i in range(7)}
DEFAULTS = dict(holiday_calendar='US_FEDERAL', inactivity_minutes=15, ai_review=False,
                review_every_seconds=60, ai_daily_limit=100,
                heartbeat_seconds=60, offline_alert_after_seconds=120,
                startup_grace_seconds=300, event_quiet_seconds=30,
                manual_timeout_seconds=300, clip_chunk_seconds=60,
                checkin_time='15:00', reminder_lookahead_days=4,
                reminder_catchup_days=4, visit_end_grace_minutes=0,
                visit_retention_days=30)
KEYS = set(DEFAULTS) | {'id','name','timezone','destination','weekly_hours',
                        'date_overrides','custom_holidays','removed_holidays','entrances'}


def clock(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{2}:\d{2}', value):
        raise ValueError('Use local times in HH:MM format')
    return time.fromisoformat(value)


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Dates must use YYYY-MM-DD')
    return date.fromisoformat(value)


def windows(value):
    if not isinstance(value, list):
        raise ValueError('Hours must be a list of [start,end] windows')
    for window in value:
        if not isinstance(window, list) or len(window) != 2:
            raise ValueError('Each hours window needs exactly two times')
        start, end = map(clock, window)
        if start == end:
            raise ValueError('Equal start/end is ambiguous; use [] for closed')


class Building:
    def __init__(self, config):
        if not isinstance(config, dict):
            raise ValueError('Building configuration must be an object')
        if set(config) - KEYS:
            raise ValueError('Unknown building configuration keys: ' + str(set(config) - KEYS))
        required = {'id','timezone','weekly_hours','destination'}
        if required - set(config):
            raise ValueError('Missing building settings: ' + ', '.join(sorted(required - set(config))))
        for key in ('id','timezone'):
            if not isinstance(config[key], str) or not config[key].strip():
                raise ValueError(key + ' must be a nonempty string')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', config['id']):
            raise ValueError('Building ID must contain letters, digits, underscore or hyphen')
        self.c = {**DEFAULTS, **config}
        self.id = config['id']
        self.name = config.get('name', self.id)
        if not isinstance(self.name, str) or not self.name:
            raise ValueError('Building name must be a nonempty string')
        try:
            self.zone = ZoneInfo(config['timezone'])
        except ZoneInfoNotFoundError:
            raise ValueError('Unknown timezone: ' + config['timezone']) from None
        self.destination = config['destination']
        if config.get('entrances',1) != 1:
            raise ValueError('This event schema currently supports one entrance per building')
        if self.destination is not None and not re.fullmatch(r'[\d-]{9,31}@g\.us', str(self.destination)):
            raise ValueError('Destination must be a WhatsApp group ID, or null for local-only mode')
        if self.c['holiday_calendar'] not in ('US_FEDERAL','CUSTOM'):
            raise ValueError('Choose US_FEDERAL or CUSTOM with explicit dates')
        weekly = self.c['weekly_hours']
        if not isinstance(weekly, dict) or set(weekly) != {str(i) for i in range(7)}:
            raise ValueError('weekly_hours must explicitly define keys 0–6, Monday–Sunday')
        for value in weekly.values():
            windows(value)
        for key in ('date_overrides','custom_holidays'):
            if not isinstance(self.c.get(key, {}), dict):
                raise ValueError(key + ' must be an object')
            for day, value in self.c.get(key, {}).items():
                iso_date(day)
                if key == 'date_overrides':
                    windows(value)
                elif not isinstance(value, str) or not value:
                    raise ValueError('Custom holiday needs a name')
        if not isinstance(self.c.get('removed_holidays', []), list):
            raise ValueError('removed_holidays must be a list')
        for day in self.c.get('removed_holidays', []):
            iso_date(day)
        if not isinstance(self.c['ai_review'], bool):
            raise ValueError('ai_review must be true or false')
        for key, default in DEFAULTS.items():
            if isinstance(default, int) and not isinstance(default, bool):
                value = self.c[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < (0 if key == 'visit_end_grace_minutes' else 1):
                    raise ValueError(key + ' must be an integer in its valid range')
        if self.c['offline_alert_after_seconds'] < self.c['heartbeat_seconds']:
            raise ValueError('Offline timeout must be at least heartbeat timeout')
        if self.c['clip_chunk_seconds'] > 300:
            raise ValueError('Clip chunks may be at most 300 seconds')
        if self.c['reminder_lookahead_days'] > 14 or self.c['reminder_catchup_days'] > 14:
            raise ValueError('Reminder lookahead/catchup must be at most 14 days')
        clock(self.c['checkin_time'])

    def holiday(self, day):
        if day.isoformat() in self.c.get('removed_holidays', []):
            return None
        custom = self.c.get('custom_holidays', {})
        if day.isoformat() in custom:
            return custom[day.isoformat()]
        return us_holidays(day.year).get(day) if self.c['holiday_calendar'] == 'US_FEDERAL' else None

    def hours(self, day):
        if day.isoformat() in self.c.get('date_overrides', {}):
            return self.c['date_overrides'][day.isoformat()]
        return [] if self.holiday(day) else self.c['weekly_hours'][str(day.weekday())]

    def open_at(self, at):
        local = at.astimezone(self.zone)
        # Windows belong to their start date; an overnight interval can spill into a closed day.
        for day in (local.date() - timedelta(days=1), local.date()):
            for start, end in self.hours(day):
                start_clock, end_clock = clock(start), clock(end)
                begin = datetime.combine(day, start_clock, self.zone)
                end_day = day + timedelta(days=1) if end_clock < start_clock else day
                finish = datetime.combine(end_day, end_clock, self.zone)
                if begin.astimezone(timezone.utc) <= at < finish.astimezone(timezone.utc):
                    return True
        return False


def validate(configs):
    buildings = [Building(c) for c in configs]
    if not buildings or len({b.id for b in buildings}) != len(buildings):
        raise ValueError('Provide buildings with unique IDs')
    return {b.id:b for b in buildings}
