# Building Watch v2

Revised rules and service core following the twelve-point reliability review. Requires Python 3.9+ on macOS/Linux, standard library only. This is source for a service, not a deployed security system. Camera detection, continuous recording, automatic video extraction, and chat-to-visit approvals still require integration. No live service was started or new WhatsApp messages sent during this revision.

## Quick start in Terminal

From this project folder:

```sh
./watch check
./watch test
./watch demo
```

`check` validates the settings and shows the remaining camera integration work. `test` runs the regression tests. `demo` runs an isolated one-building practice scenario, checks its results, and saves alerts, footage requests, and audit records inside `.local/`. Every practice run gets a new database and explicitly uses local delivery. The simulated scenario covers a first arrival, possible last departure, camera outage/recovery, suspicious activity, conflicting event IDs, and malformed input. Its complete suspicious event is preserved in consecutive recording requests; actual video extraction is still pending.

The pilot template, `buildings.pilot.example.json`, contains one entrance in New York time, Monday–Friday 9 a.m.–5 p.m., and local-only delivery. These are provisional settings to review for the first building. To run the local timer and event service:

```sh
./watch start
```

The first start copies the template to `.local/buildings.json` without replacing existing settings. The event spool and monitoring database also stay in `.local/`, separate from practice runs. Press Control-C to stop. Use `./watch status`, `./watch media`, or `./watch audit` in another Terminal to inspect the local service. Until a real camera connector is installed, the service has no camera input and will report that the camera has never connected after the startup grace period.

## Private dashboard

Open [Building Watch](https://building-watch-status.horizon-hmc-1229.chatgpt.site) on your phone or Mac and sign in with the owning ChatGPT account. The app shows camera health, service health, the latest alerts and footage requests, and setup progress. Explore demo is explicitly simulated and does not change live records.

The dashboard source is backed up in `dashboard/`. It has its own Node.js dependencies for website development; the Python monitoring service still uses the standard library only. The deployed Site has its own source repository; `dashboard/` is the matching source backup in this GitHub repository. Start any future web edits from the active Site checkout and use the Sites publishing workflow, then refresh this backup.

The Mac publishes a read-only status snapshot every 15 seconds while this command is running:

```sh
./watch publish
```

For a single update, use `./watch publish --once`. Its private connection lives in ignored `.local/dashboard.json`; it is excluded from GitHub. Camera footage, WhatsApp group IDs, provider IDs, operator credentials, and local media paths are excluded from snapshots. Keep the Site owner-private.

The connection file must be configured on the Mac before publishing. It is a JSON object with `url` (the exact private Site HTTPS origin) and `token` (its Sites service credential). This repository does not create the file automatically. Obtain the credential through the supported Sites workflow, keep the file owner-readable only, and never commit or paste the credential into shared messages.

Status publication and monitoring are separate processes. Starting the publisher does not start the camera connector or the monitoring service. To run the existing local timer/event service, use `./watch start` in another Terminal. No camera connector is installed yet. Current access is a responsive website; remote video playback and automatic recording/extraction remain unfinished.

If the publisher stops or the Mac sleeps or disconnects, the dashboard marks its last update as stale after 60 seconds. This is a visible freshness warning, not an external outage notification service. Continuous process supervision and remote outage alerts remain deployment work.

## GitHub backup

The source code is kept in Git. `.gitignore` excludes local settings, credentials, monitoring databases, event spools, practice results, and video files. Store all future runtime settings and secrets in `.local/` or outside this project. Source backup does not include footage or monitoring history.

After changing source code, save another backup with:

```sh
git add .
git commit -m "Update Building Watch"
git push
```

Review `git diff --cached` before committing if you have added files outside the private runtime directory. A restored clone can run `./watch test` and `./watch demo` with Python 3.9 or later; no additional Python packages are needed.

## Test without sending messages

From this folder:

```sh
python3 -m unittest -v
python3 building_watch.py --db demo-v2.sqlite3 replay demo.jsonl --until 2026-11-10T15:00:00-05:00
python3 building_watch.py --db demo-v2.sqlite3 media-jobs
python3 building_watch.py --db demo-v2.sqlite3 audit
```

The sample configuration uses `destination: null` to explicitly select local-only operation. Replay makes no network calls, sorts buffered input, and advances simulated time to `--until`. The demonstration includes identical duplicates, conflicting IDs, and a malformed line; valid events continue processing. The `./watch demo` command writes its results to a new practice directory inside `.local/`.

## Architecture

| Module | Responsibility |
|---|---|
| `buildingwatch/config.py` | Validate configuration, calendars, overnight windows |
| `buildingwatch/intake.py` | Durable event buffer, deduplication, collisions, quarantine |
| `buildingwatch/store.py` | SQLite state, audit, visits, media jobs, delivery queue |
| `buildingwatch/rules.py` | Person/departure rules, health, suspicious events, check-ins |
| `buildingwatch/visits.py` | Operator authentication, building grants, visit audit and expiry |
| `buildingwatch/service.py` | Autonomous scheduler and local append-only camera spool |
| `buildingwatch/delivery.py` | Whapi text/video worker, retry/backoff, status confirmation |
| `building_watch.py` | Explicit commands for the separate workers and trusted operations |

## Changes addressing the review

1. **Late events:** a durable five-second reorder buffer applies live events in timestamp order. Heartbeats precede other events at equal timestamps. Events older than the committed rules watermark are quarantined with their content retained and an audit entry. They do not rewind state or kill the run. Future timestamps beyond 30 seconds are quarantined. Camera clocks must be synchronized.
2. **Reused IDs:** normalized event content is hashed. Same ID plus same content is idempotent; different content produces an `id_collision` audit entry with both hashes. The conflicting source input remains in the spool.
3. **Timers:** `serve` runs its own one-second loop. No `tick` event is needed. Delivery runs separately so slow network requests cannot block timer processing.
4. **Overnight hours:** `["20:00","02:00"]` crosses midnight. Windows belong to their starting date and can continue into a closed following day. Holidays/overrides control windows starting on that date. Ends are exclusive; equal start/end is rejected. Explicit IANA timezones handle daylight-saving transitions.
5. **Strict config:** timezone, destination, and all weekday keys `0`–`6` are required. Unknown keys, invalid weekdays, bad hours/timezones, invalid group IDs, and bad numeric values fail on startup. The template explicitly sets Monday–Friday 09:00–17:00. `null` selects local-only delivery. This version supports one entrance per building.
6. **Startup grace:** a camera with no first heartbeat produces `camera_never_connected` after a separate five-minute grace. Only a previously connected camera produces `camera_offline`. State and recovery reporting survive restarts.
7. **Exits:** valid exits are always recorded and audited, including after hours and during unhealthy coverage. A broken coverage interval produces `departure_unverified`; it never establishes an empty building. Returning person activity cancels the pending check. Fifteen minutes remains an inactivity heuristic and can also trigger after a quiet midday departure.
8. **First person:** the first entry, detected person activity, exit, or explicit person-related suspicious flag on each local date counts. Presence or exit cannot establish the exact arrival time, so those messages say it is unconfirmed. Each produces a ten-second footage request.
9. **Visits:** camera input cannot approve a visit, even with an actor field. A separate local operation verifies an operator token hash and building grants. Approvals/denials are audited. The end time is explicitly exclusive; configurable end grace defaults to zero. Visits expire, then are purged after 30 days by default; audit entries remain. This is a trusted local operator interface, not a public login server.
10. **Long suspicious events:** automatic flags close after a quiet period. Manual flags have a five-minute inactivity lease renewed by continuing person observations/flags. Timeout closure is labeled. Active long events produce contiguous numbered requests of at most 60 seconds by default, preserving the full observed duration without a total event cap. Missing coverage is flagged. These are footage requests until a recorder supplies real files.
11. **Holiday weekends:** Friday check-ins look ahead four days, including Monday holidays. Pre-holiday check-ins remain enabled. The timer catches up missed reminders across restarts for up to four days and deduplicates by scheduled local date. Initial installation catches up only today's reminder.
12. **Delivery:** actual group IDs route alerts to a durable queue. A separate Whapi worker sends text and attached MP4 files, stores provider message IDs, retries known rate-limit rejections and confirmed failures with bounded attempts, and polls delivery status. Provider acceptance is `accepted`, not `delivered`. Ambiguous network results, 5xx responses, or a crash during sending become `delivery_unknown` and are not blindly resent. Acceptance without confirmation for 24 hours becomes `delivery_unconfirmed`. Investigate these states manually; exactly-once delivery across an ambiguous provider failure is not promised.

Regression coverage includes late/malformed/oversized inputs, persistent buffering, ID collisions, timer autonomy, overnight/DST windows, startup grace, exit retention, authorization, holiday catch-up, routing, retry exhaustion, worker restarts, and delivery confirmation. A long-event test verifies all 8,106 seconds remain covered by contiguous chunks. Delivery tests use a fake provider; the revised worker has not been rerun against the live account.

## Input contract

Append one JSON object plus a newline per event. Required fields: `id`, `building`, `type`, `at`; optional: `reason`. Times need UTC offsets. Types: `entry`, `exit`, `activity`, `heartbeat`, `offline`, `suspicious_start`, `suspicious_end`. `activity` means detected person presence, including stationary people, not arbitrary pixel motion.

```json
{"id":"camera-event-001","building":"office-1","type":"entry","at":"2026-11-02T09:00:01-05:00"}
```

The spool is append-only. Byte offsets and buffered events survive restarts; partial trailing lines wait for completion. Bad/oversized lines, including timestamps that overflow during conversion, are quarantined and skipped. Truncation is logged; rotation by replacing the file is not supported. A future camera connector must supply stable IDs, usable-frame heartbeats, and calibrated directional events. Configure it to report genuine usable-frame heartbeats at least twice within `heartbeat_seconds` (by default, one every 30 seconds or faster for a 60-second threshold), with enough extra margin for expected jitter. Coverage becomes unverified when a heartbeat is older than that threshold; the separate offline alert defaults to 120 seconds. Heartbeats must represent actual usable frames, not merely a running connector. RTSP person detection is not implemented here.

Spool catch-up uses bounded reads of a fixed file prefix. Its timer horizon is saved with the offsets and timers wait until the whole prefix has been read, so older events cannot be overtaken by the clock between batches. Newly appended bytes form the next prefix. The scheduler reads successive backlog batches without the usual one-second delay; a large backlog can still delay timer alerts, and the dashboard's scheduler freshness reflects that delay. Events already older than the committed watermark are still quarantined. Event timestamps must use years 1900 through 9997, including buffered events created by older versions. If settings remove a building while it has buffered events, those events are retained in quarantine with a `building_removed` audit entry, allowing the remaining buildings to continue.

## Autonomous scheduler

Use a new v2 database. V2 uses new prefixed tables but does not migrate old occupancy/delivery state.

```sh
python3 building_watch.py --config buildings.example.json --db watch-v2.sqlite3 serve --input camera-events.jsonl
```

Timers run even without camera events. Ctrl+C stops gracefully. Only one worker of each role may own the database. Install a process supervisor to restart workers after host reboots. For whole-office power/internet failure alerts, the supervisor must run outside the office and receive authenticated on-site health reports. That remote agent/transport is still integration work.

## Real WhatsApp delivery

Copy the example to a private runtime location and set each destination to the intended WhatsApp group ID ending in `@g.us`, not the sender phone number. No real group IDs or credentials are included in these files. Run the scheduler with the private configuration, then start the independent delivery worker against the same database/configuration:

To save the Whapi channel token without showing it on screen, run `python3 whatsapp_setup.py --file /path/to/private/whapi.json` and enter the token at the hidden prompt. Choose a private directory outside the project. The helper creates a new owner-only credential file and does not modify existing ignore files or directory permissions. It does not contact Whapi or verify the connection.

```sh
python3 building_watch.py --config /path/to/private/buildings.json --db watch-v2.sqlite3 deliver --credentials /path/to/private/whapi.json
```

This command really sends queued messages. Keep demo and live databases separate. `outbox` shows queue state, attempt count, provider IDs, and errors. Local-only rows remain local-only; routing is recorded when an alert is created.

Confirmation follows [Whapi's status semantics](https://support.whapi.cloud/help-desk/receiving/webhooks/incoming-webhooks-format/sent-message) and [GET message](https://whapi.readme.io/reference/getmessage). Provider-reported group delivery does not prove every member read the message. Polling consumes API quota; an authenticated webhook receiver is preferable at higher volume. Sandbox limits still apply.

## Operator-approved visits

Create a private operator grant outside the shared project:

```sh
python3 operator_setup.py --config buildings.example.json --file /path/to/private/operators.json --actor manager --buildings office-1
```

Choose and retain a strong access token at the hidden prompt. Only its SHA-256 digest and building grants are saved. Protect the grant file with OS permissions. Approval requires that token:

```sh
python3 building_watch.py --db watch-v2.sqlite3 approve-visit --operators /path/to/private/operators.json --building office-1 --start 2026-11-07T10:00:00-05:00 --end 2026-11-07T12:00:00-05:00
```

The result identifies `ends_at_exclusive`. With zero grace, activity at noon is outside that example. A five-minute configured grace extends it until 12:05, exclusive. Group replies still need a future authenticated receiver and explicit confirmation; arbitrary text never becomes an approval in this version.

## Recorded media

Inspect `media-jobs`. A trusted recorder must supply footage matching each absolute start/end interval, encoded as a compatible H.264 MP4. Attach from a private media directory:

```sh
python3 building_watch.py --db watch-v2.sqlite3 attach-media --job 1 --file /path/to/media/clip-1.mp4 --media-root /path/to/media
```

This trusted local operation queues the file with its building route and numbered caption; camera input cannot invoke it. Files must be at most 90 MB. Reduce bitrate or split at the recorder when needed. The adapter validates containment, extension and size; it does not validate that the bytes match the requested footage or transcode media. Automatic extraction, byte-aware splitting, missing-recording retries, and retention/disk-space handling remain recorder integration work.

## Remaining deployment work

Use Reolink's existing iPhone/Android app for live viewing. Before deployment, finish real camera input/recording, automatic clip extraction, continuous inbound chat handling, remote health-agent authentication, service installation/supervision, failed-delivery monitoring, credential rotation and access management. Suspicious rules currently mean off-hours person activity and explicit flags, not general behavior recognition.

US holidays follow nationwide dates and observed-day rules from [OPM](https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/). Other calendars use explicit custom dates; regional Inauguration Day and ad hoc future closures are not automatically added. The initial equipment budget remains approximately $1,000; this revision made no purchases or installations.

## Reviewed reliability fixes

Malformed Unicode IDs and deeply nested input are quarantined. Interrupted HTTP responses no longer stop delivery or publication. Equal-timestamp events are processed before their deadline timers, and person-related suspicious flags cancel departure checks. Credential setup preserves existing ignore rules and directory permissions. Group delivery means at least one matching participant receipt from GET /statuses/{MessageID}; it does not establish delivery to every member. Live provider validation remains required.
