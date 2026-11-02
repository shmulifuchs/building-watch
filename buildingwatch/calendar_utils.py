import calendar
import re
from datetime import date, datetime, timezone, timedelta

def instant(value):
    value = value.replace('Z', '+00:00')
    # Python 3.9/3.10 require exactly 3 or 6 digits. Normalize seconds
    # to microseconds, matching the precision supported by datetime.
    value = re.sub(r'^(\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2})\.(\d+)',
                   lambda match: match.group(1)+'.'+match.group(2)[:6].ljust(6, '0'), value)
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('Include a timezone offset in every timestamp')
    return result.astimezone(timezone.utc)

def us_holidays(year):
    """Nationwide federal holidays, including observed dates across year boundaries."""
    result = {}
    for y in (year - 1, year, year + 1):
        fixed = [(1, 1, 'New Year’s Day'), (6, 19, 'Juneteenth'),
                 (7, 4, 'Independence Day'), (11, 11, 'Veterans Day'),
                 (12, 25, 'Christmas Day')]
        for month, day, name in fixed:
            d = date(y, month, day)
            result[d] = name
            if d.weekday() == 5:
                result[d - timedelta(days=1)] = name + ' (observed)'
            elif d.weekday() == 6:
                result[d + timedelta(days=1)] = name + ' (observed)'
        for month, weekday, nth, name in [
            (1, 0, 3, 'Martin Luther King Jr. Day'),
            (2, 0, 3, 'Washington’s Birthday'),
            (5, 0, -1, 'Memorial Day'), (9, 0, 1, 'Labor Day'),
            (10, 0, 2, 'Columbus Day'), (11, 3, 4, 'Thanksgiving Day')]:
            days = [date(y, month, n) for n in range(1, calendar.monthrange(y, month)[1] + 1)
                    if date(y, month, n).weekday() == weekday]
            result[days[nth - 1 if nth > 0 else -1]] = name
    return {d: name for d, name in result.items() if d.year == year}
