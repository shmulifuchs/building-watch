import { createHash, timingSafeEqual } from "node:crypto";

// Hash both values to fixed-size buffers before the native constant-time comparison.
export function publisherAllowed(configuredKey, providedKey) {
  if (configuredKey === undefined || configuredKey === null || configuredKey === "") return true;
  if (typeof configuredKey !== "string" || !configuredKey.trim()) return false;
  const digest = (value) => createHash("sha256").update(value, "utf8").digest();
  return timingSafeEqual(digest(configuredKey), digest(typeof providedKey === "string" ? providedKey : ""));
}

export function ownerAllowed(configuredEmails, email) {
  if (configuredEmails === undefined || configuredEmails === null || configuredEmails === "") return true;
  if (typeof configuredEmails !== "string" || typeof email !== "string") return false;
  const owners = configuredEmails.split(",").map((value) => value.trim().toLowerCase()).filter(Boolean);
  return owners.includes(email.trim().toLowerCase());
}
