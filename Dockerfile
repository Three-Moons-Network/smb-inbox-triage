# Build context: repo root.
#
# This image is shared by both Cloud Run V2 services (classifier and feedback).
# The FUNCTION_TARGET environment variable selects the entry point:
#
#   classifier service  →  FUNCTION_TARGET=handle_webhook   (default)
#   feedback service    →  FUNCTION_TARGET=handle_feedback
#
# The container layout mirrors the Cloud Functions zip root (src/ contents
# at /app/) so all existing `from classifier.xxx import` paths resolve
# without modification.
#
# Build manually:
#   docker build -t smb-inbox-triage:local .
#
# Build and push via Cloud Build:
#   gcloud builds submit . --config=cloudbuild.yaml \
#     --substitutions=_TAG=$(git rev-parse --short HEAD)

FROM python:3.12-slim

WORKDIR /app

# Install deps as a separate layer for cache reuse
COPY src/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "functions-framework>=3.5,<4"

# Copy application source — same layout as the Cloud Functions zip root
COPY src/ .

# Run as an unprivileged user (CIS Docker Benchmark 4.1). Nothing here needs
# root: functions-framework binds an unprivileged port and deps are installed
# at build time.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser /app
USER appuser

# Cloud Run requires the server to listen on $PORT
EN