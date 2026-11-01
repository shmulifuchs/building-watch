import { integer, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const statusSnapshots = sqliteTable("status_snapshots", {
  id: text("id").primaryKey(),
  payload: text("payload").notNull(),
  capturedAt: integer("captured_at").notNull(),
  receivedAt: integer("received_at").notNull(),
});
