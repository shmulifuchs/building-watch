import { z } from "zod";

const timestamp = z.string().datetime({ offset: true });
const hours = z.array(z.tuple([z.string().regex(/^\d{2}:\d{2}$/), z.string().regex(/^\d{2}:\d{2}$/)])).max(24);
const alert = z.object({
  id: z.number().int().positive(), type: z.string().min(1).max(100),
  created_at: timestamp, text: z.string().max(2000),
  delivery_status: z.enum(["pending", "sending", "local_only", "accepted", "delivered", "failed", "delivery_unknown", "delivery_unconfirmed"]),
}).strict();
const recording = z.object({
  id: z.number().int().positive(), type: z.string().min(1).max(100), start: timestamp,
  end: timestamp, duration_seconds: z.number().positive().max(300),
  part: z.number().int().positive().nullable(), final_part: z.boolean(),
  recording_gap_possible: z.boolean(), status: z.enum(["awaiting_recording", "attached", "no_footage", "clip_failed", "ai_cleared", "ai_skipped"]),
}).strict().refine(value => Math.abs(Date.parse(value.end) - Date.parse(value.start) - value.duration_seconds * 1000) < 10,
  "Recording interval must match its duration");
const building = z.object({
  id: z.string().regex(/^[A-Za-z0-9_-]+$/).max(100), name: z.string().min(1).max(200),
  timezone: z.string().min(1).max(100).refine(value => { try { new Intl.DateTimeFormat("en", { timeZone: value }); return true; } catch { return false; } }),
  weekly_hours: z.object({ "0": hours, "1": hours, "2": hours, "3": hours, "4": hours, "5": hours, "6": hours }).strict(),
  hours_today: hours, open_now: z.boolean(), destination_configured: z.boolean(),
  heartbeat_seconds: z.number().int().positive(), offline_after_seconds: z.number().int().positive(),
  last_heartbeat: timestamp.nullable(), service_updated_at: timestamp.nullable(),
  disconnected: z.boolean(), occupancy: z.enum(["person_detected", "motion_detected", "unconfirmed"]),
  suspicious_active: z.boolean(), alerts: z.array(alert).max(20), recordings: z.array(recording).max(20),
}).strict();
export const snapshotSchema = z.object({
  schema_version: z.literal(1), captured_at: timestamp, buildings: z.array(building).min(1).max(32),
}).strict().refine(value => new Set(value.buildings.map(building => building.id)).size === value.buildings.length,
  "Building IDs must be unique");
