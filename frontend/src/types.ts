export type Platform = "bilibili" | "weibo" | "douyin" | "xiaohongshu";
export type AccountStatus = "pending" | "healthy" | "polling" | "error" | "blocked" | "paused";
export type CompletenessStatus = "unknown" | "complete" | "pending_retry" | "gap_detected";

export interface AuthResponse { username: string; csrf_token: string }
export interface PageResponse<T> {
  items: T[];
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
}
export interface Account {
  id: number;
  platform: Platform;
  display_name: string;
  slug: string;
  source_url: string;
  enabled: boolean;
  interval_minutes: number;
  baseline_established: boolean;
  completeness_status?: CompletenessStatus;
  gap_detected_at?: string | null;
  status: AccountStatus;
  consecutive_failures: number;
  last_error: string | null;
  last_polled_at: string | null;
  next_poll_at: string | null;
  created_at: string;
}
export interface AccountTestResult { ok: boolean; found: number; latest_ids: string[] }
export interface Content {
  id: number;
  account_id: number;
  platform: Platform;
  remote_id: string;
  title: string;
  author: string;
  content_type: string;
  source_url: string;
  published_at: string;
  collected_at: string;
  summary: string;
  media_count: number;
  expected_media_count?: number;
  verified_media_count?: number;
  integrity_status?: CompletenessStatus;
  status: string;
  error: string | null;
}
export interface MediaRecord {
  kind: string;
  source_url: string;
  local_path: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
}
export interface ContentDetail extends Content {
  markdown: string;
  metadata: { text: string; media: MediaRecord[]; [key: string]: unknown };
}
export interface CrawlRun {
  id: number;
  account_id: number;
  started_at: string;
  finished_at: string | null;
  status: string;
  discovered_count: number;
  archived_count: number;
  error: string | null;
  details: Record<string, unknown>;
}
export interface StorageInfo {
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  archive_bytes: number;
  minimum_free_bytes: number;
  downloads_paused: boolean;
}
export interface Summary { accounts: number; healthy_accounts: number; contents: number; failed_runs: number }
export type PlatformSessionStatus = "logged_out" | "starting" | "qr_ready" | "authenticated" | "expired" | "manual_verification_required" | "error";
export interface PlatformSession {
  platform: Platform;
  status: PlatformSessionStatus;
  updated_at: string | null;
  message: string | null;
  manual_verification_url: string;
  image_data_url?: string;
}
