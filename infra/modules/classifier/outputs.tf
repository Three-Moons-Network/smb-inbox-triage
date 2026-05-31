# ── modules/classifier — shared interface contract (T3) ───────────────────────
#
# This directory is NOT a callable Terraform module.
# It is a reference document defining the input/output contract that every
# cloud-specific module (modules/aws, modules/azure, modules/gcp) must satisfy.
#
# Each cloud module independently declares all variables listed in variables.tf
# and produces all outputs listed below.  This keeps the environment root modules
# (environments/*) agnostic to the underlying cloud.
#
# To add a new cloud module: copy variables.tf here as a starting point and
# implement every output below.
#
# ── Required outputs ──────────────────────────────────────────────────────────
#
#   webhook_url     - HTTPS URL to POST inbound email webhook payloads to
#   feedback_url    - HTTPS URL for the human feedback correction webhook
#   datastore_name  - Name of the classification log datastore (table/container/collection)
#
# (no Terraform output blocks — this file is documentation only)
