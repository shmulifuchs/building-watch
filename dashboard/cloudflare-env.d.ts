declare namespace Cloudflare {
  interface Env {
    DB?: D1Database;
    STATUS_PUBLISH_KEY?: string;
    OWNER_EMAILS?: string;
    BUCKET?: R2Bucket;
  }
}
