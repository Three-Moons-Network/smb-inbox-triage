# ── DynamoDB — classification log ────────────────────────────────────────────

resource "aws_dynamodb_table" "classifications" {
  name         = "${local.name_prefix}-classifications"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "record_id"

  attribute {
    name = "record_id"
    type = "S"
  }

  attribute {
    name = "intent"
    type = "S"
  }

  attribute {
    name = "classified_at"
    type = "S"
  }

  # GSI to query by intent + time (useful for eval harness + dashboard)
  global_secondary_index {
    name = "intent-time-index"

    key_schema {
      attribute_name = "intent"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "classified_at"
      key_type       = "RANGE"
    }

    projection_type = "ALL"
  }

  # Auto-expire old records after 90 days (TTL attribute set by Lambda)
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = var.env == "prod"
  }
}

# ── DynamoDB — feedback store ─────────────────────────────────────────────────

resource "aws_dynamodb_table" "feedback" {
  name         = "${local.name_prefix}-feedback"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "record_id"

  attribute {
    name = "record_id"
    type = "S"
  }

  # TTL — keep feedback records for 1 year for retraining purposes
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}
