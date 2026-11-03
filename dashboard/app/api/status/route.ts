import { env } from "cloudflare:workers";
import { getChatGPTUser } from "../../chatgpt-auth";
import { snapshotSchema } from "../../../lib/status-schema.mjs";
import { publisherAllowed, ownerAllowed } from "../../../lib/access.mjs";

export const dynamic = "force-dynamic";
const headers = { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" };

export async function GET() {
  const user = await getChatGPTUser();
  if (!user) return Response.json({ error: "Sign in to view your building status." }, { status: 401, headers });
  if (!ownerAllowed(env.OWNER_EMAILS, user.email))
    return Response.json({ error: "This account cannot view building status." }, { status: 403, headers });
  if (!env.DB) return Response.json({ error: "Status storage is unavailable." }, { status: 503, headers });
  const row = await env.DB.prepare("SELECT payload,received_at FROM status_snapshots WHERE id=?")
    .bind("pilot").first<{ payload: string; received_at: number }>();
  return Response.json({ snapshot: row ? JSON.parse(row.payload) : null,
    received_at: row ? new Date(row.received_at).toISOString() : null }, { headers });
}

// Sites dispatch protects this shared endpoint on the confirmed owner-private Site.
// The on-site publisher uses the platform service credential; it has no visitor identity.
export async function POST(request: Request) {
  if (!publisherAllowed(env.STATUS_PUBLISH_KEY, request.headers.get("X-Building-Watch-Key")))
    return Response.json({ error: "Publisher authentication required." }, { status: 401, headers });
  const origin = request.headers.get("Origin");
  if (origin && origin !== new URL(request.url).origin) return Response.json({ error: "Origin rejected." }, { status: 403, headers });
  if (!request.headers.get("Content-Type")?.startsWith("application/json"))
    return Response.json({ error: "A JSON status snapshot is required." }, { status: 415, headers });
  if (!env.DB) return Response.json({ error: "Status storage is unavailable." }, { status: 503, headers });
  let raw: unknown;
  try {
    const reader = request.body?.getReader();
    if (!reader) throw new Error("Missing body");
    const chunks: Uint8Array[] = [];
    let size = 0;
    while (true) {
      const item = await reader.read();
      if (item.done) break;
      size += item.value.byteLength;
      if (size > 1_000_000) { await reader.cancel(); return Response.json({ error: "Snapshot is too large." }, { status: 413, headers }); }
      chunks.push(item.value);
    }
    const data = new Uint8Array(size);
    let position = 0;
    for (const chunk of chunks) { data.set(chunk, position); position += chunk.length; }
    raw = JSON.parse(new TextDecoder().decode(data));
  } catch { return Response.json({ error: "Invalid JSON snapshot." }, { status: 400, headers }); }
  const parsed = snapshotSchema.safeParse(raw);
  if (!parsed.success) return Response.json({ error: "Snapshot fields are invalid." }, { status: 422, headers });
  const captured = Date.parse(parsed.data.captured_at);
  const now = Date.now();
  if (captured > now + 30_000 || captured < now - 300_000)
    return Response.json({ error: "Snapshot clock is out of date." }, { status: 422, headers });
  const result = await env.DB.prepare("INSERT INTO status_snapshots(id,payload,captured_at,received_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,captured_at=excluded.captured_at,received_at=excluded.received_at WHERE excluded.captured_at>=status_snapshots.captured_at")
    .bind("pilot", JSON.stringify(parsed.data), captured, now).run();
  if (!result.meta.changes) return Response.json({ error: "A newer status snapshot is already stored." }, { status: 409, headers });
  return Response.json({ ok: true, received_at: new Date(now).toISOString() }, { headers });
}
