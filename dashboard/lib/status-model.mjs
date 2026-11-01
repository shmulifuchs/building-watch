export function ageSeconds(value, now = Date.now()) {
  if (!value) return Infinity;
  const age = (now - Date.parse(value)) / 1000;
  return Number.isFinite(age) && age >= -30 ? Math.max(0, age) : Infinity;
}

export function freshness(snapshot, receivedAt, now = Date.now()) {
  if (!snapshot || !receivedAt) return "missing";
  return ageSeconds(snapshot.captured_at, now) <= 60 && ageSeconds(receivedAt, now) <= 60 ? "fresh" : "stale";
}

export function cameraState(building, fresh, now = Date.now()) {
  if (!building?.last_heartbeat) return "not_connected";
  if (fresh !== "fresh") return "stale";
  if (building.disconnected || ageSeconds(building.last_heartbeat, now) >= building.offline_after_seconds) return "offline";
  if (ageSeconds(building.last_heartbeat, now) > building.heartbeat_seconds) return "delayed";
  return "online";
}

export function serviceState(building, fresh, now = Date.now()) {
  if (!building?.service_updated_at) return "not_started";
  if (fresh !== "fresh") return "stale";
  return ageSeconds(building.service_updated_at, now) <= 30 ? "running" : "stopped";
}
