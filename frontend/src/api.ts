// ponytail: hand-written types mirroring backend/app/main.py; generate from /openapi.json once the API settles.
export type Status = "ready" | "needs_attention" | "incompatible" | "unknown";
export type CopyType = "disc" | "digital" | "other" | "unknown";

export interface Game {
  id: string;
  title: string;
  title_id: string | null;
  content_id: string | null;
  metadata_source: string;
  metadata_confidence: "high" | "medium" | "low" | "unknown";
  parser_warnings: string[];
  region: string | null;
  version: string | null;
  format: string;
  image_subtype: string | null;
  source_path: string;
  size_bytes: number;
  compressed_size_bytes: number | null;
  has_cover: boolean;
  description: string | null;
  genre: string | null;
  release_date: string | null;
  owned_copy_type: CopyType;
  backup_date: string | null;
  provenance_notes: string | null;
  favorite: boolean;
  verified: boolean;
  missing: boolean;
  compatibility_status: Status;
  compatibility_reason: string;
  detection_evidence: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface Root {
  id: string;
  path: string;
  enabled: boolean;
  exclude_patterns: string[];
  reachable: boolean;
}

export type ConnectionType = "ftp" | "http" | "custom" | "mock";

export interface Console {
  id: string;
  name: string;
  ip_address: string;
  connection_type: ConnectionType;
  port: number;
  username: string | null;
  has_password: boolean;
  firmware: string | null;
  last_seen_at: string | null;
  enabled: boolean;
}

export interface ScanStatus {
  running: boolean;
  finished_at: string | null;
  report: Record<"added" | "updated" | "missing" | "unknown" | "invalid" | "errors", string[]> | null;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, opts: { method?: string; json?: unknown; body?: BodyInit } = {}): Promise<T> {
  const res = await fetch(`/api/v1${path}`, {
    method: opts.method ?? (opts.json || opts.body ? "POST" : "GET"),
    headers: opts.json ? { "Content-Type": "application/json" } : undefined,
    body: opts.json ? JSON.stringify(opts.json) : opts.body,
    credentials: "same-origin",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // 422 validation errors come back as {detail: [...]}
    const msg = data.message ?? (Array.isArray(data.detail) ? data.detail.map((d: { msg: string }) => d.msg).join("; ") : res.statusText);
    throw new ApiError(res.status, data.error ?? "http_error", msg);
  }
  return data as T;
}

export const coverUrl = (g: Game) => (g.has_cover ? `/api/v1/games/${g.id}/cover?v=${encodeURIComponent(g.updated_at)}` : null);

export function formatBytes(n: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) {
    n /= 1000;
    i++;
  }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

export const STATUS_LABEL: Record<Status, string> = {
  ready: "Ready",
  needs_attention: "Needs attention",
  incompatible: "Incompatible",
  unknown: "Unknown",
};

export const SOURCE_LABEL: Record<string, string> = {
  ps5_cnt: "CNT",
  ps4_cnt: "CNT",
  ps5_param_json: "param.json",
  ps4_param_sfo: "param.sfo",
  sidecar: "Sidecar",
  filename: "Filename",
  manual: "Manual",
  none: "Unknown",
};

export const formatLabel = (g: Game) => (g.image_subtype ? `image · ${g.image_subtype}` : g.format.replace("_", " "));
