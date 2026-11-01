# Building Watch dashboard

A private, responsive status app for the Building Watch pilot. It is designed for phone and Mac browsers and uses Sites sign-in and owner-only access. The Python monitoring service remains the source of truth.

## What it shows

- Current camera heartbeat and monitoring-service health, with independent freshness checks.
- The latest 20 alerts and recording requests per building, isolated by building ID.
- Provider acceptance and delivery confirmation as different states.
- The configured weekly schedule and the schedule at the snapshot time.
- Explicit setup steps, including the work still needed for cameras and video extraction.
- A clearly labeled demonstration generated from the actual rules engine. Demo mode never updates the stored live snapshot.

Camera and service health become unavailable when source snapshots or cloud receipts are more than 60 seconds old. A current upload cannot make an old camera heartbeat or stopped scheduler look healthy. Occupancy is an observation, never a verified headcount.

## Status connection

The companion Python project provides `dashboard_sync.py` and `./watch publish`. It reads SQLite in read-only mode and publishes a bounded snapshot every 15 seconds. It strips WhatsApp group destinations, provider IDs, credentials, operator grants, and local video paths.

Keep the private connection in the Python project's ignored `.local/dashboard.json`, with owner-only file permissions. Its fields are `url` (the exact deployed HTTPS origin) and `token` (the Sites service credential). No access credential belongs in this source tree or browser code.

The snapshot endpoint is shared within the confirmed owner-private Site. Sites dispatch protects the producer route through its service access boundary; browser reads also require a signed-in identity. Keep this Site private. Adding a different audience or multiple independent owners requires revisiting server-side authorization and data isolation first.

The server validates the snapshot schema, rejects future or old clocks, rejects out-of-order updates, blocks cross-origin writes, and caps request size. Uploaded data is stored in D1. No footage is uploaded, and remote video playback is not implemented.

## Development and checks

Requires Node.js 22.13 or later. Use the Sites lifecycle helpers for setup, dependency installation, preview, build, packaging, and publication. Preserve `.openai/hosting.json`, the starter's Sites integration, and the immutable Drizzle migration history.

```sh
npm ci
npm run dev
node --test tests/status.test.mjs
node node_modules/typescript/bin/tsc --noEmit
npm run build
```

Apply the generated D1 migration to local preview storage before using its status endpoint. Production migrations are applied by Sites during publication. Local sign-in is the starter's loopback-only mock; production authentication is owned by Sites.
