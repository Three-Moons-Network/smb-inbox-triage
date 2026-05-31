# get_gmail_token.py
# Run once to authorise Gmail access and save the OAuth token to gmail-token.json.
#
# Usage:
#   python get_gmail_token.py
#
# Prerequisites:
#   pip install google-auth-oauthlib google-api-python-client
#   Place gmail-oauth-credentials.json in this directory (downloaded from
#   GCP Console → APIs & Services → Credentials → OAuth 2.0 Client IDs)

from google_auth_oauthlib.flow import InstalledAppFlow
import json

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

flow = InstalledAppFlow.from_client_secrets_file("gmail-oauth-credentials.json", SCOPES)

# run_local_server(port=0) picks a random available port so it won't
# conflict with other tools.  Your browser will open automatically —
# sign in and grant access, then return here.
creds = flow.run_local_server(port=0)

token_data = {
    "token":         creds.token,
    "refresh_token": creds.refresh_token,
    "token_uri":     creds.token_uri,
    "client_id":     creds.client_id,
    "client_secret": creds.client_secret,
    "scopes":        list(creds.scopes),
}

with open("gmail-token.json", "w") as f:
    json.dump(token_data, f, indent=2)

print("Saved gmail-token.json")
print("Next: store it in Secret Manager with:")
print("  gcloud secrets create gmail-oauth-token --data-file=gmail-token.json --project=your-gcp-project-id")
