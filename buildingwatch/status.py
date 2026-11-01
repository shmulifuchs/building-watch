"""Read-only, credential-free status snapshots for the private dashboard."""
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .config import validate


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def snapshot(config_path, db_path, now=None):
    now = time.time() if now is None else now
    raw = json.loads(Path(config_path).read_text())
    if not isinstance(raw, dict) or set(raw) != {'buildings'} or not isinstance(raw['buildings'], list):
        raise ValueError('Configuration must contain only a buildings list')
    buildings = validate(raw['buildings'])
    if len(buildings) > 32:
        raise ValueError('The pilot dashboard supports up to 32 buildings')
    result = {'schema_version': 1, 'captured_at': stamp(now), 'buildings': []}
    database = Path(db_path).resolve()
    connection = None
    try:
        if database.exists():
            connection = sqlite3.connect('file:' + quote(str(database), safe='/') + '?mode=ro', uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA busy_timeout=3000')
            connection.execute('BEGIN')
        for building in buildings.values():
            state, alerts, recordings = {}, [], []
            if connection:
                row = connection.execute('SELECT body FROM bw_state WHERE building=?', (building.id,)).fetchone()
                state = json.loads(row['body']) if row else {}
                for row in connection.execute('SELECT id,body,status FROM bw_queue WHERE building=? ORDER BY id DESC LIMIT 20', (building.id,)):
                    body = json.loads(row['body'])
                    alerts.append(dict(id=row['id'], type=body['type'], created_at=body['created_at'],
                                       text=body['text'][:2000], delivery_status=row['status']))
                for row in connection.execute('SELECT id,body,status FROM bw_media WHERE building=? ORDER BY id DESC LIMIT 20', (building.id,)):
                    body = json.loads(row['body'])
                    recordings.append(dict(id=row['id'], type=body['type'], start=body['start'],
                                           end=body['end'], duration_seconds=body['duration_seconds'],
                                           part=body.get('part'), final_part=body.get('final_part', False),
                                           recording_gap_possible=body.get('recording_gap_possible', False),
                                           status=row['status']))
            local = datetime.fromtimestamp(now, timezone.utc).astimezone(building.zone)
            result['buildings'].append(dict(
                id=building.id, name=building.name, timezone=building.c['timezone'],
                weekly_hours=building.c['weekly_hours'], hours_today=building.hours(local.date()),
                open_now=building.open_at(datetime.fromtimestamp(now, timezone.utc)),
                destination_configured=building.destination is not None,
                heartbeat_seconds=building.c['heartbeat_seconds'],
                offline_after_seconds=building.c['offline_alert_after_seconds'],
                last_heartbeat=stamp(state.get('heartbeat')),
                service_updated_at=stamp(state.get('last_timer')),
                disconnected=bool(state.get('disconnected')),
                occupancy=state.get('occupancy', 'unconfirmed'),
                suspicious_active=bool(state.get('suspicious')),
                alerts=alerts, recordings=recordings))
        return result
    finally:
        if connection:
            connection.close()
