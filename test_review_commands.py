"""Exercise the review fixes through command entry points."""
import contextlib
import copy
import importlib.machinery
import importlib.util
import io
import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import building_watch


ROOT = Path(__file__).resolve().parent


class ReviewCommandTests(unittest.TestCase):
    def test_watch_check_reports_unknown_timezone_without_traceback(self):
        loader = importlib.machinery.SourceFileLoader('watch_test_module', str(ROOT / 'watch'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        watch = importlib.util.module_from_spec(spec)
        loader.exec_module(watch)
        template = json.loads((ROOT / 'buildings.pilot.example.json').read_text())
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for zone in ('America/New_Yrok', 'Not/AZone'):
                with self.subTest(zone=zone):
                    config = copy.deepcopy(template)
                    config['buildings'][0]['timezone'] = zone
                    (folder / 'buildings.json').write_text(json.dumps(config))
                    stderr = io.StringIO()
                    with mock.patch.object(watch, 'LOCAL', folder), mock.patch('sys.argv', ['watch', 'check']), contextlib.redirect_stderr(stderr):
                        self.assertEqual(watch.main(), 1)
                    self.assertIn('Unknown timezone: ' + zone, stderr.getvalue())
                    self.assertNotIn('Traceback', stderr.getvalue())
                    self.assertFalse((folder / 'watch.sqlite3').exists())

    def test_replay_interrupt_stops_before_more_events_or_final_flush(self):
        original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            with tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                config = folder / 'buildings.json'
                config.write_text(json.dumps({'buildings': []}))
                events = folder / 'events.jsonl'
                for count in (1, 2):
                    with self.subTest(event_count=count):
                        events.write_text('{}\n' * count)
                        engine = mock.Mock()
                        engine.ingest.side_effect = lambda *_args, **_kwargs: signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
                        argv = ['building_watch.py', '--config', str(config), 'replay', str(events), '--until', '2026-11-10T15:00:00-05:00']
                        stdout = io.StringIO()
                        with mock.patch('sys.argv', argv), mock.patch.object(building_watch, 'Engine', return_value=engine), contextlib.redirect_stdout(stdout):
                            self.assertEqual(building_watch.main(), 130)
                        self.assertEqual(engine.ingest.call_count, 1)
                        engine.advance.assert_not_called()
                        engine.alerts.assert_not_called()
                        engine.close.assert_called_once_with()
                        self.assertEqual(stdout.getvalue(), '')
        finally:
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)


if __name__ == '__main__':
    unittest.main()
