CREATE TABLE `status_snapshots` (
	`id` text PRIMARY KEY NOT NULL,
	`payload` text NOT NULL,
	`captured_at` integer NOT NULL,
	`received_at` integer NOT NULL
);
