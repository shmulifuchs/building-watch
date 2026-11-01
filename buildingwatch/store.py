import json
import sqlite3


class Store:
    def __init__(self, path=':memory:'):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS bw_state(building TEXT PRIMARY KEY,body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bw_events(building TEXT,id TEXT,at REAL,digest TEXT,body TEXT,
            status TEXT,PRIMARY KEY(building,id));
        CREATE TABLE IF NOT EXISTS bw_audit(id INTEGER PRIMARY KEY,at REAL,building TEXT,
            kind TEXT,detail TEXT);
        CREATE TABLE IF NOT EXISTS bw_queue(id INTEGER PRIMARY KEY,building TEXT,key TEXT,
            body TEXT,status TEXT,attempts INTEGER DEFAULT 0,due REAL,provider_id TEXT,
            error TEXT,checked REAL,UNIQUE(building,key));
        CREATE TABLE IF NOT EXISTS bw_visits(id INTEGER PRIMARY KEY,building TEXT,actor TEXT,
            start REAL,end REAL,effective_end REAL,status TEXT);
        CREATE TABLE IF NOT EXISTS bw_media(id INTEGER PRIMARY KEY,building TEXT,key TEXT,
            body TEXT,status TEXT,path TEXT,UNIQUE(building,key));
        CREATE TABLE IF NOT EXISTS bw_offsets(source TEXT PRIMARY KEY,position INTEGER);
        CREATE TABLE IF NOT EXISTS bw_meta(key TEXT PRIMARY KEY,value TEXT);
        ''')

    def state(self, building):
        row = self.db.execute('SELECT body FROM bw_state WHERE building=?',(building,)).fetchone()
        return json.loads(row['body']) if row else {}

    def save(self, building, state):
        self.db.execute('INSERT OR REPLACE INTO bw_state VALUES(?,?)',(building,json.dumps(state)))

    def audit(self, at, building, kind, **detail):
        self.db.execute('INSERT INTO bw_audit(at,building,kind,detail) VALUES(?,?,?,?)',
                        (at,building,kind,json.dumps(detail,sort_keys=True)))

    def enqueue(self, building, key, body, at):
        self.db.execute('INSERT OR IGNORE INTO bw_queue(building,key,body,status,due) VALUES(?,?,?,?,?)',
                        (building,key,json.dumps(body),'pending' if body['destination'] else 'local_only',at))

    def transaction(self):
        return Transaction(self.db)


class Transaction:
    def __init__(self, db):
        self.db = db
    def __enter__(self):
        self.db.execute('BEGIN IMMEDIATE')
    def __exit__(self, kind, value, traceback):
        self.db.execute('ROLLBACK' if kind else 'COMMIT')
