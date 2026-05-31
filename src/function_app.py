# Azure Functions v2 entry point — shim that selects the right FunctionApp
# based on the FUNCTION_TARGET env var.
#
# Background: Azure Functions Python v2 model requires a file literally named
# `function_app.py` at /home/site/wwwroot/. The repo intentionally has two
# separate entrypoints — `azure_function_app.py` (classifier) and
# `azure_feedback_function_app.py` (feedback) — because each Container App
# loads only one. The Flex Consumption deploy script (AZURE-DEPLOY.md) renames
# one of those to function_app.py at zip-build time. Container Apps share a
# single image, so we dispatch at runtime instead.
import os

_target = (os.environ.get("FUNCTION_TARGET") or "classifier").lower()

if _target in ("feedback", "feedback_app", "azure_feedback", "azure_feedback_function_app"):
    from azure_feedback_function_app import app  # noqa: F401
else:
    from azure_function_app import app           # noqa: F401
