import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from buildingwatch.service import Engine
from buildingwatch.status import snapshot
from dashboard_sync import publish
from test_building_watch import config, T, timestamp


class StatusSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'buildings.json'
        self.config.write_text(json.dumps({'buildings': [config()]}))
        self.db = self.root / 'watch.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def test_unstarted_service_does_not_create_a_database(self):
        value = snapshot(self.config, self.db, T)
        self.assertFalse(self.db.exists())
        building = value['buildings'][0]
        self.assertIsNone(building['last_heartbeat'])
        self.assertIsNone(building['service_updated_at'])
        self.assertEqual(building['occupancy'], 'unconfirmed')

    def test_snapshot_does_not_export_routes_credentials_or_recording_paths(self):
        engine = Engine([config()], str(self.db))
        engine.ingest(dict(id='entry', building='a', type='entry', at=timestamp(T)), T, replay=True)
        engine.advance(T, flush=True)
        state = engine.store.state('a')
        state['private_token'] = 'FAKE-PRIVATE-VALUE'
        engine.store.save('a', state)
        engine.db.execute("UPDATE bw_media SET path='/private/footage/clip.mp4'")
        before = engine.db.execute('SELECT body FROM bw_state').fetchone()[0]
        value = snapshot(self.config, self.db, T + 10)
        after = engine.db.execute('SELECT body FROM bw_state').fetchone()[0]
        engine.close()
        self.assertEqual(before, after)
        exported = json.dumps(value)
        for private in ('12345678901@g.us', 'FAKE-PRIVATE-VALUE', '/private/footage', 'provider_id'):
            self.assertNotIn(private, exported)
        self.assertTrue(value['buildings'][0]['destination_configured'])
        self.assertEqual(len(value['buildings'][0]['alerts']), 1)
        self.assertEqual(value['buildings'][0]['service_updated_at'], timestamp(T))

    def test_alerts_are_bounded_and_isolated_by_building(self):
        self.config.write_text(json.dumps({'buildings': [config(), config('b')]}))
        engine = Engine([config(), config('b')], str(self.db))
        for number in range(25):
            engine.store.enqueue('a', str(number), dict(destination=None, type='test', text='For A', created_at=timestamp(T)), T)
        engine.store.enqueue('b', 'one', dict(destination=None, type='test', text='For B', created_at=timestamp(T)), T)
        engine.close()
        a, b = snapshot(self.config, self.db, T)['buildings']
        self.assertEqual(len(a['alerts']), 20)
        self.assertTrue(all(row['text'] == 'For A' for row in a['alerts']))
        self.assertEqual([row['text'] for row in b['alerts']], ['For B'])

    def test_publisher_requires_https_and_rejects_url_credentials(self):
        for url in ('http://example.com', 'https://example.com/other', 'https://user:secret@example.com'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                publish({}, {'url': url, 'token': 'FAKE-TEST-TOKEN'})

    def test_publisher_uses_private_header_and_a_nonredirecting_client(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, *_): return b'{"ok":true}'
        with patch('dashboard_sync.urllib.request.build_opener') as builder:
            builder.return_value.open.return_value = Response()
            publish({'schema_version': 1}, {'url': 'https://example.com', 'token': 'FAKE-TEST-TOKEN'})
            request = builder.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://example.com/api/status')
            self.assertEqual(request.headers['Oai-sites-authorization'], 'Bearer FAKE-TEST-TOKEN')
            self.assertEqual(builder.call_args.args[0].__class__.__name__, 'NoRedirect')


if __name__ == '__main__':
    unittest.main()
