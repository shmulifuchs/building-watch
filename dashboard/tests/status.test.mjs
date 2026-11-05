import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { ageSeconds, cameraState, freshness, serviceState } from "../lib/status-model.mjs";
import { snapshotSchema } from "../lib/status-schema.mjs";

const sample = JSON.parse(readFileSync(new URL("../app/demo-data.json", import.meta.url), "utf8"));
const now = Date.parse(sample.captured_at);
const building = { ...sample.buildings[0], disconnected: false, last_heartbeat: new Date(now).toISOString(), service_updated_at: new Date(now).toISOString() };

test("a stopped publisher cannot leave camera or service looking healthy", () => {
  const fresh = freshness(sample, sample.captured_at, now + 61000);
  assert.equal(fresh, "stale");
  assert.equal(cameraState(building, fresh, now + 61000), "stale");
  assert.equal(serviceState(building, fresh, now + 61000), "stale");
});
test("camera heartbeat ages through delayed and offline independently of fresh uploads", () => {
  assert.equal(cameraState(building, "fresh", now), "online");
  assert.equal(cameraState(building, "fresh", now + 61000), "delayed");
  assert.equal(cameraState(building, "fresh", now + 120000), "offline");
  assert.equal(cameraState({ ...building, disconnected: true }, "fresh", now), "offline");
});
test("clip failures and motion occupancy do not reject a snapshot", () => {
  for (const status of ["no_footage", "clip_failed"]) {
    const recording = { id: 1, type: "first_motion", start: "2026-11-05T10:00:00+00:00", end: "2026-11-05T10:00:10+00:00", duration_seconds: 10, part: null, final_part: false, recording_gap_possible: false, status };
    const parsed = snapshotSchema.safeParse({ ...sample, buildings: [{ ...sample.buildings[0], occupancy: "motion_detected", recordings: [recording] }] });
    assert.equal(parsed.success, true, status);
  }
});
test("no camera and no service are reported as unconfigured, not healthy", () => {
  assert.equal(cameraState({ ...building, last_heartbeat: null }, "fresh", now), "not_connected");
  assert.equal(serviceState({ ...building, service_updated_at: null }, "fresh", now), "not_started");
  assert.equal(freshness(null, null, now), "missing");
});
test("a fresh upload with an old source clock is stale", () => {
  assert.equal(freshness(sample, new Date(now + 90000).toISOString(), now + 90000), "stale");
  assert.equal(ageSeconds("not a timestamp", now), Infinity);
  assert.equal(ageSeconds(new Date(now + 120000).toISOString(), now), Infinity);
});
test("snapshot validation rejects secrets, duplicate buildings, and invalid recording intervals", () => {
  assert.equal(snapshotSchema.safeParse(sample).success, true);
  assert.equal(snapshotSchema.safeParse({ ...sample, token: "FAKE-PRIVATE-VALUE" }).success, false);
  assert.equal(snapshotSchema.safeParse({ ...sample, buildings: [building, building] }).success, false);
  const wrong = structuredClone(sample);
  wrong.buildings[0].recordings[0].duration_seconds = 1;
  assert.equal(snapshotSchema.safeParse(wrong).success, false);
});
test("emoji-heavy Python snapshots satisfy the actual browser schema", () => {
  const script = `
import json, tempfile
from pathlib import Path
from buildingwatch.service import Engine
from buildingwatch.status import snapshot
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    settings = root / 'buildings.json'
    settings.write_text(Path('buildings.pilot.example.json').read_text())
    raw = json.loads(settings.read_text())['buildings']
    db = root / 'watch.sqlite3'
    engine = Engine(raw, str(db))
    engine.store.enqueue(raw[0]['id'], 'emoji', dict(destination=None,
        type='test', created_at='2026-11-02T14:00:00+00:00', text='😀'*1500), 1793628000)
    engine.close()
    print(json.dumps(snapshot(settings, db, 1793628000)))
`;
  const value = JSON.parse(execFileSync("python3", ["-c", script], {
    cwd: fileURLToPath(new URL("../../", import.meta.url)), encoding: "utf8",
  }));
  assert.equal(snapshotSchema.safeParse(value).success, true);
  assert.equal(value.buildings[0].alerts[0].text, "😀".repeat(1000));
});

// Optional controls must fail closed when configured and preserve existing behavior otherwise.
test("publisher key rejects missing/wrong values and permits only an exact configured key", async () => {
  const { publisherAllowed } = await import("../lib/access.mjs");
  assert.equal(publisherAllowed(undefined, null), true);
  assert.equal(publisherAllowed("", null), true);
  assert.equal(publisherAllowed("FAKE-TEST-KEY-😀", null), false);
  assert.equal(publisherAllowed("FAKE-TEST-KEY-😀", "FAKE-WRONG-KEY"), false);
  assert.equal(publisherAllowed("FAKE-TEST-KEY-😀", "FAKE-TEST-KEY-😀"), true);
  assert.equal(publisherAllowed("   ", "   "), false);
});
test("owner email restrictions reject other signed-in accounts", async () => {
  const { ownerAllowed } = await import("../lib/access.mjs");
  assert.equal(ownerAllowed(undefined, "seedy@sites.test"), true);
  assert.equal(ownerAllowed(" Owner@Example.com, second@example.com ", "owner@example.com"), true);
  assert.equal(ownerAllowed("owner@example.com", "other@example.com"), false);
  assert.equal(ownerAllowed("owner@example.com", "owner@example.com.attacker.test"), false);
  assert.equal(ownerAllowed(" , ", "other@example.com"), false);
});
