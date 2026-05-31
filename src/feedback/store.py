# -*- coding: utf-8 -*-
"""
Feedback store abstraction.

Writes human correction records to the cloud-appropriate datastore.
The CLOUD env var selects the backend at runtime.

Review fixes applied
--------------------
P4  — DynamoDB update_item now includes a ConditionExpression that raises
      ConditionalCheckFailedException if record_id does not exist.
      Callers should catch this and return HTTP 404.
P9  — datetime.now(timezone.utc) replaces deprecated utcnow().
P22 — CLOUD env var read lazily inside write_feedback(), not at import time.
      Tests can now override os.environ["CLOUD"] between calls.
P23 — Cloud-SDK imports moved to top-level (inside TYPE_CHECKING guard where
      they'd cause import errors in the wrong cloud environment; actual imports
      happen inside the cloud-specific private functions to avoid cold-start
      failures when the SDK is not installed).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _utcnow() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix — P9."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_feedback(record_id: str, corrected_intent: str, reviewer: str) -> None:
    """
    Persist a human correction.

    Updates the existing ClassificationRecord with the corrected intent.
    Raises KeyError / ConditionalCheckFailedException if record_id not found (P4).

    P22: CLOUD is read here, not at import time.
    """
    cloud = os.environ.get("CLOUD", "aws")

    if cloud == "aws":
        _write_dynamodb(record_id, corrected_intent, reviewer)
    elif cloud == "azure":
        _write_cosmos(record_id, corrected_intent, reviewer)
    elif cloud == "gcp":
        _write_firestore(record_id, corrected_intent, reviewer)
    else:
        raise ValueError(f"Unsupported CLOUD value: {cloud!r}")

    logger.info(
        "Feedback written",
        extra={"record_id": record_id, "corrected_intent": corrected_intent},
    )


# ── AWS DynamoDB ──────────────────────────────────────────────────────────────

def _write_dynamodb(record_id: str, corrected_intent: str, reviewer: str) -> None:
    import boto3  # noqa: PLC0415 — optional; not installed on Azure/GCP deploys
    from botocore.exceptions import ClientError

    table_name = os.environ["DYNAMODB_FEEDBACK_TABLE"]
    dynamodb   = boto3.resource(
        "dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1")
    )
    table = dynamodb.Table(table_name)

    try:
        table.update_item(
            Key={"record_id": record_id},
            UpdateExpression=(
                "SET feedback_correction = :intent, "
                "feedback_reviewer = :reviewer, "
                "feedback_at = :ts"
            ),
            ExpressionAttributeValues={
                ":intent":   corrected_intent,
                ":reviewer": reviewer,
                ":ts":       _utcnow(),
            },
            # P4: reject updates for non-existent records
            ConditionExpression="attribute_exists(record_id)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise KeyError(f"record_id {record_id!r} not found in DynamoDB") from exc
        raise


# ── Azure Cosmos DB ───────────────────────────────────────────────────────────

def _write_cosmos(record_id: str, corrected_intent: str, reviewer: str) -> None:
    from azure.cosmos import CosmosClient              # noqa: PLC0415
    from azure.cosmos.exceptions import CosmosResourceNotFoundError  # noqa: PLC0415
    from azure.identity import DefaultAzureCredential  # noqa: PLC0415

    # COSMOS_CONNECTION_STRING holds the account endpoint URL, not a connection
    # string with a key.  Key-based auth is disabled on the Cosmos account
    # (local_authentication_disabled = true in Terraform).  DefaultAzureCredential
    # resolves to the Function App's System-Assigned MSI at runtime.
    endpoint  = os.environ["COSMOS_CONNECTION_STRING"]
    client    = CosmosClient(endpoint, credential=DefaultAzureCredential())
    container = (
        client
        .get_database_client(os.environ["COSMOS_DATABASE"])
        .get_container_client(os.environ["COSMOS_CONTAINER_CLASSIFICATIONS"])
    )

    try:
        container.patch_item(
            item=record_id,
            partition_key=record_id,
            patch_operations=[
                {"op": "add", "path": "/feedback_correction", "value": corrected_intent},
                {"op": "add", "path": "/feedback_reviewer",   "value": reviewer},
                {"op": "add", "path": "/feedback_at",         "value": _utcnow()},
            ],
        )
    except CosmosResourceNotFoundError as exc:
        raise KeyError(f"record_id {record_id!r} not found in Cosmos DB") from exc


# ── GCP Firestore ─────────────────────────────────────────────────────────────

def _write_firestore(record_id: str, corrected_intent: str, reviewer: str) -> None:
    from google.cloud import firestore  # noqa: PLC0415
    from google.api_core.exceptions import NotFound

    db      = firestore.Client(project=os.environ["GOOGLE_CLOUD_PROJECT"])
    doc_ref = db.collection("classifications").document(record_id)

    try:
        doc_ref.update({
            "feedback_correction": corrected_intent,
            "feedback_reviewer":   reviewer,
            "feedback_at":         _utcnow(),
        })
    except NotFound as exc:
        raise KeyError(f"record_id {record_id!r} not found in Firestore") from exc
