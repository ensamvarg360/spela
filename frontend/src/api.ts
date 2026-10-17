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
  kind: Kind;
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
  installed: boolean; // on any console, per its last storage scan
  store: StoreInfo | null; // only when already looked up; the tile asks for the rest
  new: boolean; // added in the last 30 days and not opened yet
}

export interface StoreInfo {
  publisher: string | null;
  release_date: string | null;
  genres: string[];
  rating: number | null;
  rating_count: number;
  age_rating: string | null;
}

export type Kind = "game" | "dlc";

export interface Root {
  id: string;
  kind: Kind;
  path: string;
  enabled: boolean;
  exclude_patterns: string[];
  reachable: boolean;
}

export type ConnectionType = "helper" | "ftp" | "http" | "custom" | "mock";

export interface Console {
  id: string;
  name: string;
  ip_address: string;
  connection_type: ConnectionType;
  port: number;
  username: string | null;
  has_password: boolean;
  firmware: string | null;
  helper_status: Record<string, string>;
  last_seen_at: string | null;
  last_inventory_scan_at: string | null;
  enabled: boolean;
  state: ConsoleState;
  status_message: string;
}

export type ConsoleState = "online" | "standby" | "degraded" | "offline" | "disabled" | "unknown";

export interface Drive {
  id: string;
  external_id: string;
  label: string;
  path: string;
  drive_type: "internal" | "usb" | "network" | "unknown";
  total_bytes: number; // 0 = unknown
  free_bytes: number; // 0 = unknown
  is_install_target: boolean;
  last_seen_at: string | null;
  installed_count: number;
}

export interface InstalledGame {
  id: string;
  game_id: string | null;
  cover_url: string | null;
  title_id: string;
  title: string;
  version: string | null;
  library_version: string | null;
  install_route: "package_installer" | "shadowmountplus" | "unknown";
  drive_id: string | null;
  installed_path: string;
  source_path: string | null;
  size_bytes: number;
  status: "installed" | "update_available" | "partially_installed" | "orphaned_icon" | "unknown";
  last_verified_at: string | null;
  removing: boolean;
}

/** An uninstall preview, then the removal's progress (project.md §13). */
export interface Removal {
  id: string;
  installed_game_id: string;
  title_id: string;
  title: string;
  source_path: string | null;
  orphaned: boolean; // tile only: its files are already gone
  estimated_bytes_freed: number; // 0 = unknown
  warnings: string[];
  allowed: boolean;
  reason: string;
  status: "blocked" | "awaiting_confirmation" | "removing" | "success" | "failed";
  message: string;
  progress: number; // 0..1 while deleting files
  bytes_done: number;
  bytes_total: number;
  tile_left: boolean; // removed, but its home-screen tile must still be deleted on the PS5
}

export type JobStatus = "queued" | "preflight" | "extracting" | "transferring" | "registering" | "success" | "failed" | "canceled" | "interrupted";
export const JOB_ACTIVE: JobStatus[] = ["queued", "preflight", "extracting", "transferring", "registering"];

export interface Job {
  id: string;
  console_id: string;
  game_id: string;
  title: string;
  target_drive_id: string | null;
  status: JobStatus;
  progress: number; // 0..1
  bytes_transferred: number;
  total_bytes: number;
  transfer_rate_bps: number;
  eta_seconds: number | null;
  message: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface AuditEntry {
  id: string;
  action: string;
  result: "success" | "failed" | "blocked" | "canceled";
  console_id: string | null;
  console_name: string | null; // null with a console_id = console since deleted
  title_id: string | null;
  target_path: string | null;
  details: Record<string, unknown>;
  created_at: string;
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

// The console helper payload must report this same version (backend/app/__init__.py).
export const appVersion: Promise<string> = api<{ version: string }>("/health").then((h) => h.version, () => "");

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
  unlocker_ini: "dlc_emu.ini",
  sidecar: "Sidecar",
  filename: "Filename",
  manual: "Manual",
  none: "Unknown",
};

export const formatLabel = (g: Game) =>
  g.image_subtype ? `image · ${g.image_subtype}` : g.format === "unlocker" ? "DLC unlocker" : g.format.replace("_", " ");
