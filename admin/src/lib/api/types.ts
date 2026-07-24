// Wire types, mirroring the CyberFS admin API's response models.
//
// Note what is absent: there is no field here that could hold file content, a
// preview, or key material, because the API exposes none. If a field ever
// appears that could, it should be questioned here first.

export interface UserStorage {
  user_id: string;
  subject: string;
  quota_bytes: number;
  used_bytes: number;
  live_bytes: number;
  trashed_bytes: number;
  version_bytes: number;
  percent_used: number;
  over_quota: boolean;
  file_count: number;
  folder_count: number;
  encrypted_file_count: number;
  encrypted_bytes: number;
  encrypted_share: number;
  grants_given: number;
  grants_received: number;
  is_admin: boolean;
  created_at: string | null;
  last_seen_at: string | null;
}

export interface UserStorageList {
  items: UserStorage[];
}

export interface ContentTypeSlice {
  content_type: string;
  file_count: number;
  bytes: number;
}

export interface GrowthSlice {
  day: string;
  files_added: number;
  bytes_added: number;
}

export interface TenantSummary {
  total_bytes: number;
  live_bytes: number;
  trashed_bytes: number;
  version_bytes: number;
  file_count: number;
  folder_count: number;
  user_count: number;
  active_user_count: number;
  encrypted_file_count: number;
  encrypted_bytes: number;
  encrypted_share: number;
  public_link_count: number;
  grant_count: number;
  content_types: ContentTypeSlice[];
  growth: GrowthSlice[];
  top_consumers: UserStorage[];
}

export interface PublicLink {
  id: string;
  node_id: string;
  created_by: string;
  created_at: string;
  expires_at: string | null;
  revoked: boolean;
  passphrase_protected: boolean;
  access_count: number;
  last_accessed_at: string | null;
}

export interface LinkList {
  items: PublicLink[];
}

export interface AuditEntry {
  action: string;
  occurred_at: string;
  actor_subject: string | null;
  target_id: string | null;
  recipient_subject: string | null;
  reason: string | null;
  source_ip: string | null;
  context: Record<string, unknown>;
}

export interface AuditPage {
  items: AuditEntry[];
  next_cursor: string | null;
}

export interface HealthComponent {
  name: string;
  status: "up" | "down" | "disabled";
  criticality: "required" | "optional";
  latency_ms: number | null;
  detail: string | null;
}

export interface JobSummary {
  name: string;
  last_run_at: string | null;
  outcome: string | null;
  duration_seconds: number | null;
  detail: string | null;
  has_run: boolean;
}

export interface BackupSummary {
  enabled: boolean;
  stale: boolean;
  last_backup_at: string | null;
  last_outcome: string | null;
  last_duration_seconds: number | null;
  last_size_bytes: number | null;
  last_verified: boolean | null;
  last_verified_at: string | null;
  object_count: number | null;
  schema_revision: string | null;
}

export interface OperationsSummary {
  components: HealthComponent[];
  jobs: JobSummary[];
  cache: Record<string, unknown>;
  totals_reconcile: boolean;
  backup: BackupSummary;
}

export interface PurgeResponse {
  dataset: string;
  keys_removed: number;
}

/** The RFC 7807 problem document every CyberFS error uses. */
export interface ProblemDetail {
  type: string;
  title: string;
  status: number;
  code: string;
  detail: string;
  request_id?: string;
}

export interface AuditQuery {
  actor?: string;
  action?: string;
  target?: string;
  since?: string;
  until?: string;
  limit?: number;
  cursor?: string;
}

export interface UserQuery {
  limit?: number;
  sort_by?: string;
  over_quota?: boolean;
}
