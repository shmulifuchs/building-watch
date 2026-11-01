import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
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
