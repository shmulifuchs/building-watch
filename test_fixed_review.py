"""Offline regression checks for the reviewed Building Watch archive.

Run with: python3 building-watch-review-checks.py /path/to/extracted/building-watch
Regression checks for repaired review findings. Three
control checks pass. Temporary files and fake providers only; no network calls.
"""
import http.client
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from buildingwatch.service import Engine, JsonlInput
from buildingwatch.delivery import Delivery, Whapi, ProviderError
from test_building_watch import config, T, timestamp
from whatsapp_setup import save_token
import dashboard_sync


def event(kind, offset, identifier=None):
    return dict(id=identifier or kind + str(offset), building="a", type=kind,
                at=timestamp(T + offset))


class BrokenResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, *args):
        raise http.client.IncompleteRead(b'{"ok":', 20)


class ReviewChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = Engine([config()])
        self.addCleanup(self.engine.close)

    def feed(self, kind, offset):
        self.engine.ingest(event(kind, offset), T + offset)
        self.engine.advance(T + offset, flush=True)

    def departure_setup(self):
        self.feed("heartbeat", 0)
        self.feed("exit", 1)
        for offset in range(30, 901, 30):
            self.feed("heartbeat", offset)

    def departures(self):
        return [row["type"] for row in self.engine.alerts()
                if "departure" in row["type"]]

    def provider(self):
        credentials = self.root / "whapi.json"
        credentials.write_text(json.dumps({"token": "FAKE-REVIEW-TOKEN"}))
        return Whapi(credentials)

    def queue(self, key):
        self.engine.store.enqueue("a", key,
            dict(destination="12345678901@g.us", text="FAKE TEST"), T)

    def test_01_group_message_status_without_participant_receipts_is_not_delivered(self):
        self.queue("group")
        provider = self.provider()

        def response(path, payload=None):
            if payload is not None:
                return {"sent": True, "message": {"id": "FAKE-MESSAGE"}}
            if path.startswith("/statuses/"):
                return {"statuses": []}
            # Whapi documents that a group's message-level status can be read
            # immediately, without indicating delivery to participants.
            return {"id": "FAKE-MESSAGE", "chat_id": "12345678901@g.us",
                    "status": "read"}

        with patch.object(provider, "request", side_effect=response):
            worker = Delivery(self.engine.store, provider)
            worker.run_once(T)
            worker.run_once(T + 61)
        self.assertNotEqual(self.engine.alerts()[0]["delivery_status"], "delivered")

    def test_02_same_timestamp_heartbeat_and_entry_cancel_departure(self):
        self.departure_setup()
        # Deliberately submit entry first. Intake sorts the heartbeat first.
        for kind in ("entry", "heartbeat"):
            self.engine.ingest(event(kind, 901), T + 901)
        self.engine.advance(T + 901, flush=True)
        self.assertEqual(self.departures(), [])

    def test_03_person_related_suspicious_flag_cancels_departure(self):
        self.departure_setup()
        self.feed("suspicious_start", 900.5)
        self.engine.advance(T + 901, flush=True)
        self.assertTrue(self.engine.store.state("a").get("suspicious"))
        self.assertEqual(self.departures(), [])

    def process_bad_then_good(self, bad_line):
        spool = self.root / "events.jsonl"
        spool.write_text(bad_line + "\n" + json.dumps(event("entry", 0, "good")) + "\n")
        JsonlInput(self.engine, spool).read(T)
        self.engine.advance(T + 5)
        row = self.engine.db.execute("SELECT status FROM bw_events WHERE id='good'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "processed")

    def test_04_invalid_unicode_identifier_does_not_stop_spool(self):
        self.process_bad_then_good(json.dumps(event("entry", 0, "\ud800")))

    def test_05_excessive_json_nesting_does_not_stop_spool(self):
        self.process_bad_then_good("[" * 1100 + "0" + "]" * 1100)

    def test_06_truncated_http_response_does_not_stop_later_alerts(self):
        self.queue("first")
        self.queue("second")
        provider = self.provider()
        with patch.object(provider.opener, "open", return_value=BrokenResponse()):
            Delivery(self.engine.store, provider).run_once(T)
        statuses = [row["status"] for row in self.engine.db.execute("SELECT status FROM bw_queue ORDER BY id")]
        self.assertEqual(statuses, ["delivery_unknown", "delivery_unknown"])

    def test_07_truncated_http_response_is_handled_by_dashboard_publisher(self):
        cfg = self.root / "buildings.json"
        cfg.write_text(json.dumps({"buildings": [config()]}))
        connection = self.root / "dashboard.json"
        connection.write_text(json.dumps({"url": "https://example.com", "token": "FAKE"}))
        arguments = ["dashboard_sync.py", "--config", str(cfg), "--db",
                     str(self.root / "missing.sqlite3"), "--connection", str(connection), "--once"]
        with patch.object(sys, "argv", arguments), \
             patch("dashboard_sync.signal.signal"), \
             patch("dashboard_sync.urllib.request.build_opener") as builder:
            builder.return_value.open.return_value = BrokenResponse()
            self.assertEqual(dashboard_sync.main(), 1)

    def test_08_credentials_setup_preserves_existing_ignore_rules(self):
        ignore = self.root / ".gitignore"
        original = "important-original-rule\n"
        ignore.write_text(original)
        save_token(self.root / "whapi.json", "FAKE-REVIEW-TOKEN")
        self.assertIn(original, ignore.read_text())

    def test_control_entry_alone_at_deadline_cancels_departure(self):
        self.departure_setup()
        self.feed("entry", 901)
        self.assertEqual(self.departures(), [])

    def test_control_ordinary_malformed_json_does_not_stop_spool(self):
        self.process_bad_then_good("not valid JSON")

    def test_control_known_ambiguous_send_does_not_stop_later_alerts(self):
        self.queue("first")
        self.queue("second")
        provider = self.provider()
        with patch.object(provider, "request", side_effect=ProviderError("unknown", "timeout")):
            Delivery(self.engine.store, provider).run_once(T)
        statuses = [row["status"] for row in self.engine.db.execute("SELECT status FROM bw_queue ORDER BY id")]
        self.assertEqual(statuses, ["delivery_unknown", "delivery_unknown"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

class GroupReceiptTests(unittest.TestCase):
    def test_only_matching_participant_receipt_confirms_delivery(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'credentials.json';path.write_text('{"token":"FAKE"}')
            provider=Whapi(path)
            message={'id':'message','chat_id':'12345678901@g.us','status':'read'}
            receipt={'id':'message','recipient_id':'12345678901@g.us','viewer_id':'123456789','status':'delivered'}
            for change,expected in [({},'delivered'),({'id':'other'},'pending'),({'recipient_id':'other@g.us'},'pending'),({'viewer_id':''},'pending'),({'status':'sent'},'pending')]:
                with self.subTest(change=change),patch.object(provider,'request',side_effect=[message,{'statuses':[dict(receipt,**change)]}]):
                    self.assertEqual(provider.status('message')['status'],expected)
