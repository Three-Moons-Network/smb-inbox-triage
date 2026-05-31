# register_gmail_watch.py
# Registers a Gmail Watch so new emails trigger a Pub/Sub notification.
#
# Usage:
#   python register_gmail_watch.py
#
# Prerequisites:
#   pip install google-auth-oauthlib google-api-python-client
#   gmail-token.json must exist in this directory (run get_gmail_token.py first)
#
# NOTE: Watch registrations expire after 7 days.
# Re-run this script weekly to keep the pipeline active.

import json
import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Set GCP_PROJECT_ID in your environment (e.g. export GCP_PROJECT_ID=your-project-id).
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "your-gcp-project-id")
PUBSUB_TOPIC = f"projects/{GCP_PROJECT_ID}/topics/smb-inbox-triage-dev-gmail-inbound"

with open("gmail-token.json") as f:
    t = json.load(f)

creds = Credentials(
    token=t["token"],
    refresh_token=t["refresh_token"],
    token_uri=t["token_uri"],
    client_id=t["client_id"],
    client_secret=t["client_secret"],
    scopes=t["scopes"],
)

service = build("gmail", "v1", credentials=creds)

response = service.users().watch(
    userId="me",
    body={
        "topicName": PUBSUB_TOPIC,
        "labelIds": ["INBOX"],
    },
).e