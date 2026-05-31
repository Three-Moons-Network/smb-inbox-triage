# Security Decisions — Accepted Risk Register

This file records infrastructure security findings that have been consciously
deferred or accepted as out-of-scope for the current iteration. Each entry
includes a justification and a review date. Items here will be re-evaluated
at each quarterly review or before a production rollout.

**Format:**  Finding ID | File | Description | Justification | Owner | Review

---

## Accepted (deferred to next iteration)

| ID  | File | Description | Justification | Owner | Review |
|-----|------|-------------|---------------|-------|--------|
| T12 | `modules/aws/dynamodb.tf` | DynamoDB PITR gated on `var.env == "prod"` literal string | Dev/staging PITR adds ~$0.02/GB/month with minimal benefit at low data volumes. Enable when table > 1GB or when staging is used for load tests. | platform-team | 2026-Q3 |
| T13 | `modules/aws/dynamodb.tf` | No explicit CMK SSE block — uses AWS-managed keys | AWS-managed encryption satisfies the threat model for this practice project. CMK required only if compliance framework (SOC2, HIPAA) mandates customer-controlled keys. | platform-team | 2026-Q3 |
| T14 | `modules/azure/main.tf` | Azure storage missing `min_tls_version`, `https_traffic_only_enabled`, blob versioning; `shared_access_key_enabled = false` removed | Storage account is internal-only. `shared_access_key_enabled = false` was removed because azurerm provider v4.x requires key-based auth internally for queue property reads during plan/apply — full provider-side Azure AD storage auth not yet stable. Re-evaluate when provider matures. TLS1_2 and https-only deferred to prod. | platform-team | 2026-Q3 |
| T15 | `modules/azure/main.tf` | Storage replication is LRS (locally redundant) | Practice project; ZRS adds ~2× storage cost with no SLA requirement yet. Upgrade to ZRS when RTO/RPO targets are defined. | platform-team | 2026-Q3 |
| T16 | `modules/azure/cosmos.tf` | CosmosDB `public_network_access_enabled` not set (defaults to true) | No VNet integration in this practice environment. Set `public_network_access_enabled = false` and add private endpoint before production. | platform-team | 2026-Q3 |
| T17 | `modules/gcp/main.tf` | GCS bucket `force_destroy = true` | Acceptable in dev-only environments for easier teardown. Must be `false` or gated to `var.env == "dev"` before staging promotion. | platform-team | 2026-Q3 |
| T18 | `modules/gcp/main.tf` | Secret Manager `version = "latest"` | Convenience for initial development. Pin to a specific version number before production; rotation requires explicit version bump. | platform-team | 2026-Q3 |
| T19 | `modules/gcp/eventarc.tf` | Intent-topic list is module-internal local | No external consumers yet. Promote to variable when a second subscriber is added. | platform-team | 2026-Q4 |
| T20 | `environments/aws-dev/main.tf` | S3 Terraform backend has no `kms_key_id` | State file encrypted with SSE-S3 (AES-256). SSE-KMS with CMK required if state contains regulated data or before production promotion. | platform-team | 2026-Q3 |
| T21 | `environments/azure-dev/main.tf` | No `use_oidc = true` on azurerm backend | Service principal key auth acceptable for local dev. OIDC required for CI/CD pipelines to eliminate long-lived secrets. Add before setting up CI. | platform-team | 2026-Q3 |
| T22 | `environments/gcp-dev/main.tf` | GCS Terraform backend uses Google-managed keys | Same justification as T20 — CMEK adds overhead with no compliance requirement at this stage. | platform-team | 2026-Q3 |

---

## Accepted (low-priority polish, no active security risk)

| ID | File | Description | Justification |
|----|------|-------------|---------------|
| T24 | `modules/aws/main.tf` | API Gateway `auto_deploy = true` | Acceptable for dev/staging; production API GW stages should use explicit deploy resources. |
| T25 | `modules/aws/main.tf` | API Gateway missing access log group | No traffic yet; add CloudWatch access logging resource when first customer traffic is expected. |
| T26 | `modules/aws/main.tf` | SQS DLQ missing KMS encryption | Same CMK rationale as T13. |
| T27 | `modules/aws/main.tf` | DLQ CloudWatch alarm has no action (SNS/PagerDuty) | Alarm exists but fires silently. Wire to notification SNS topic when on-call rotation is set up. |
| T28 | `modules/gcp/main.tf` | GCP service APIs not explicitly enabled via `google_project_service` | APIs are already enabled on the target project. Terraform-managed API enablement is belt-and-suspenders; add when project bootstrap automation is built. |
| T29 | `modules/azure/keyvault.tf` | Key Vault missing `purge_protection_enabled = true` | Required to meet regulatory standards. Enable before production — soft-delete is already on. |
| T30 | `modules/azure/keyvault.tf` | Key Vault missing `enable_rbac_authorization = true` | Access policy model is functional but RBAC model is preferred. Migrate when a second team member needs access. |
| T31 | `modules/gcp/main.tf` | IAM uses additive bindings (`google_project_iam_member`) not authoritative bindings (`google_project_iam_policy`) | Additive bindings allow manual grants to accumulate outside Terraform. Authoritative bindings are safer but require full IAM inventory first. Migrate when the project's full IAM is known. |

---

## Resolved (previously accepted, now fixed)

| ID | Fixed in | Notes |
|----|----------|-------|
| T10/N3 | `modules/gcp/main.tf` (2026-05-24) | Replaced `roles/datastore.user` with `google_project_iam_custom_role` scoped to get/create/update only. Collection-level scoping remains a residual risk documented in the resource comment. |
| T23 | (2026-05-24) | `.terraform.lock.hcl` committed for all three environment directories. |

---

*Last reviewed: 2026-05-24 by platform-team. Next review: 2026-Q3.*
