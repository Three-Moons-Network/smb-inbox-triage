# -*- coding: utf-8 -*-
"""
OpenTelemetry instrumentation wired to Datadog.

Architecture (per cloud)
------------------------
Both platforms use the same localhost:4318 target — only the receiver differs.

AWS Lambda:
    Python code
        │  OTLP/HTTP  http://localhost:4318
        ▼
    Datadog Lambda Extension (layer)   ← local OTLP receiver + stdout log capture
        │  HTTPS/443 to Datadog intake
        ▼
    Datadog APM + LLM Observability + Logs

GCP Cloud Run V2:
    Python code (classifier / feedback container)
        │  OTLP/HTTP  http://localhost:4318
        ▼
    Datadog Agent sidecar container    ← local OTLP receiver in shared network ns
        │  HTTPS/443 to Datadog intake
        ▼
    Datadog APM + LLM Observability + Logs

The app container does NOT need DD_API_KEY on either platform — the
Extension / Agent sidecar holds the key and handles Datadog auth.

Environment variables
---------------------
  DD_SERVICE       service name tag     (default: smb-inbox-triage)
  DD_ENV           env tag              (default: dev)
  DD_VERSION       version tag
  DD_SITE          Datadog site         (default: datadoghq.com)
  DD_API_KEY       DD API key — used when sending direct to Datadog OTLP intake
                   (on Lambda, use DD_API_KEY_SECRET_ARN instead and let the
                   extension resolve it; the Python SDK does not need this key)
  DD_AGENT_HOST    Datadog Agent host   (default: localhost)
  OTEL_EXPORTER_OTLP_ENDPOINT
                   Overrides the OTLP endpoint entirely.  Set to
                   https://otlp.datadoghq.com:4317 for direct Datadog intake
                   (Azure / GCP without a sidecar agent).
  OBSERVABILITY_ENABLED
                   Set "false" to disable all instrumentation (unit tests).

Span hierarchy produced by this module
---------------------------------------
  classifier.classify_email          ← handler.py
    └── gen_ai.<provider>.invoke     ← each adapter (Bedrock/AzureOAI/Vertex)
          gen_ai.system, gen_ai.request.model, gen_ai.usage.*  (LLM Obs attrs)

  router.dispatch                    ← destinations.dispatch()
    └── router.<destination>         ← each destination handler

  feedback.correction                ← feedback handler
"""

from __future__ import annotations

import json as _json
import logging
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Generator

logger = logging.getLogger(__name__)

# ── Instrumentation toggle ─────────────────────────────────────────────────────
_ENABLED = (
    os.environ.get("OBSERVABILITY_ENABLED", "true").lower()
    not in ("false", "0", "no")
)

# ── Lazy OTel imports ──────────────────────────────────────────────────────────
_OTEL_AVAILABLE = False
if _ENABLED:
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.trace import StatusCode
        _OTEL_AVAILABLE = True
    except ImportError:
        logger.warning(
            "opentelemetry packages not installed — tracing disabled. "
            "pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http"
        )

_OTEL_LOGS_AVAILABLE = False
if _OTEL_AVAILABLE:
    try:
        # SDK 1.24+ — the logs module is still semantically beta and lives under
        # the underscored ``_logs`` package. The non-underscored ``logs`` import
        # never resolved and silently disabled OTel log export — see
        # https://github.com/open-telemetry/opentelemetry-python/issues/3030.
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry._logs import set_logger_provider
        _OTEL_LOGS_AVAILABLE = True
    except ImportError:
        pass  # log exporter is optional; fall back to stdout-only JSON logs

# Metrics provider — added during the multi-cloud remediation so Datadog receives
# OTLP metrics in addition to traces+logs. Datadog OTLP intake REQUIRES delta
# temporality; cumulative is rejected. We force delta everywhere so the same
# instrumentation works behind the Lambda Extension, the GCP Agent sidecar, and
# direct OTLP intake from Azure.
_OTEL_METRICS_AVAILABLE = False
if _OTEL_AVAILABLE:
    try:
        from opentelemetry import metrics as _metrics
        from opentelemetry.sdk.metrics import MeterProvider
        # preferred_temporality keys MUST be the SDK instrument classes, NOT the
        # opentelemetry.metrics (API) classes. The OTLP exporter rejects API classes
        # with "Invalid instrument class found <class 'opentelemetry.metrics.Counter'>",
        # which silently disabled metrics. Import the SDK instrument types here.
        from opentelemetry.sdk.metrics import (
            Counter as _SDKCounter,
            UpDownCounter as _SDKUpDownCounter,
            Histogram as _SDKHistogram,
            ObservableCounter as _SDKObservableCounter,
            ObservableUpDownCounter as _SDKObservableUpDownCounter,
            ObservableGauge as _SDKObservableGauge,
        )
        from opentelemetry.sdk.metrics.export import (
            AggregationTemporality,
            PeriodicExportingMetricReader,
        )
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        _OTEL_METRICS_AVAILABLE = True
    except ImportError:
        pass  # metrics exporter is optional

if TYPE_CHECKING:
    from opentelemetry.trace import Span
    from src.classifier.models import ClassificationRecord
    from src.router.rules import RoutingDecision


# ── OTLP endpoint resolution ───────────────────────────────────────────────────

def _otlp_endpoint() -> str:
    """
    Resolve the OTLP/HTTP endpoint.

    Priority:
      1. OTEL_EXPORTER_OTLP_ENDPOINT env var (explicit override)
      2. http://localhost:4318  (default — Datadog Lambda Extension on AWS,
         or Datadog Agent sidecar on GCP Cloud Run)

    Both platforms set OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
    explicitly in their infrastructure config so this fallback rarely fires,
    but it is correct for local development too (requires a local DD Agent).
    """
    return os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or (
        f"http://{os.environ.get('DD_AGENT_HOST', 'localhost')}:4318"
    )


def _otlp_headers() -> dict[str, str]:
    """Return OTLP auth headers for the HTTP exporter."""
    # Strip whitespace/newlines — Secret Manager may inject a trailing \n.
    api_key = os.environ.get("DD_API_KEY", "").strip()
    return {"DD-API-KEY": api_key} if api_key else {}


def _otel_resource() -> "Resource":
    return Resource.create({
        "service.name":           os.environ.get("DD_SERVICE", "smb-inbox-triage"),
        "service.version":        os.environ.get("DD_VERSION", "unknown"),
        "deployment.environment": os.environ.get("DD_ENV",     "dev"),
        "cloud.provider":         os.environ.get("CLOUD",      "unknown"),
    })


# ── TracerProvider setup ───────────────────────────────────────────────────────

def _bsp_schedule_delay_ms() -> int:
    """
    BatchSpanProcessor flush cadence. Default OTel value is 5000ms; on a FaaS
    container that's CPU-throttled or frozen between invocations the timer
    rarely fires, so we lower it to keep more recent batches ready when
    force_flush() runs at the end of each handler. Override via env if needed.
    """
    try:
        return int(os.environ.get("OTEL_BSP_SCHEDULE_DELAY", "500"))
    except ValueError:
        return 500


def _setup_tracer() -> "trace.Tracer | None":
    if not _OTEL_AVAILABLE:
        return None
    try:
        resource = _otel_resource()
        headers  = _otlp_headers()

        # Do NOT pass endpoint here — when endpoint=None the SDK reads
        # OTEL_EXPORTER_OTLP_ENDPOINT from env and appends /v1/traces.
        # Passing it explicitly bypasses that path-append logic entirely.
        provider = TracerProvider(resource=resource)
        # timeout=8 so export attempt fails fast and is visible within flush()
        exporter = OTLPSpanExporter(headers=headers, timeout=8)
        logger.info(
            "OTel tracer initialised: endpoint=%s/v1/traces has_api_key=%s",
            _otlp_endpoint(),
            bool(headers.get("DD-API-KEY")),
        )
        # BatchSpanProcessor buffers spans and flushes in a background thread.
        # Cloud Functions containers are frozen between invocations so the
        # background thread never auto-flushes — callers MUST call flush()
        # (which calls force_flush) at the end of each handler before returning.
        # This keeps span export off the hot path so it does not add per-span
        # HTTPS latency to handler response time.
        provider.add_span_processor(
            BatchSpanProcessor(exporter, schedule_delay_millis=_bsp_schedule_delay_ms())
        )
        trace.set_tracer_provider(provider)
        return trace.get_tracer("smb-inbox-triage")
    except Exception as exc:
        logger.warning("OTel tracer setup failed (%s) — running without tracing", exc)
        return None


# ── MeterProvider setup (OTLP metrics → Datadog, delta temporality) ───────────

_meter_provider: "MeterProvider | None" = None
meter: "_metrics.Meter | None" = None


def _setup_meter() -> "_metrics.Meter | None":
    """
    Configure OTel metrics with delta temporality (required by Datadog OTLP intake).

    Datadog rejects cumulative metrics — sending cumulative produces no errors at
    the OTel level but the data never appears in Datadog. Setting the temporality
    preference on every instrument kind is the only reliable fix.
    """
    global _meter_provider
    if not _OTEL_METRICS_AVAILABLE:
        return None
    try:
        resource = _otel_resource()
        headers  = _otlp_headers()
        os.environ.setdefault("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "delta")

        exporter = OTLPMetricExporter(
            headers=headers,
            timeout=8,
            preferred_temporality={
                _SDKCounter:                 AggregationTemporality.DELTA,
                _SDKUpDownCounter:           AggregationTemporality.DELTA,
                _SDKHistogram:               AggregationTemporality.DELTA,
                _SDKObservableCounter:       AggregationTemporality.DELTA,
                _SDKObservableUpDownCounter: AggregationTemporality.DELTA,
                _SDKObservableGauge:         AggregationTemporality.DELTA,
            },
        )
        reader = PeriodicExportingMetricReader(
            exporter,
            export_interval_millis=_bsp_schedule_delay_ms(),
        )
        _meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        _metrics.set_meter_provider(_meter_provider)
        return _metrics.get_meter("smb-inbox-triage")
    except Exception as exc:
        logger.warning("OTel meter setup failed (%s) — running without metrics", exc)
        return None


# ── OTel LoggerProvider setup (sends logs via OTLP alongside traces) ───────────

def _setup_otlp_log_handler() -> bool:
    """Wire the OTel OTLP log exporter into the Python root logger."""
    if not _OTEL_LOGS_AVAILABLE:
        return False
    try:
        resource  = _otel_resource()
        headers   = _otlp_headers()

        # Same reasoning as _setup_tracer — omit endpoint so SDK appends /v1/logs
        log_provider = LoggerProvider(resource=resource)
        log_exporter = OTLPLogExporter(headers=headers, timeout=8)
        log_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                log_exporter, schedule_delay_millis=_bsp_schedule_delay_ms()
            )
        )
        set_logger_provider(log_provider)

        # Attach OTel handler at NOTSET so every Python log record is exported
        otel_log_handler = LoggingHandler(
            level=logging.NOTSET,
            logger_provider=log_provider,
        )
        logging.getLogger().addHandler(otel_log_handler)
        return True
    except Exception as exc:
        logger.warning("OTel log handler setup failed: %s", exc)
        return False


# ── JSON stdout formatter (direct Datadog log shipping) ───────────────────────
#
# Datadog Lambda Extension captures stdout and ships logs directly to Datadog
# without going through CloudWatch.  This formatter produces structured JSON
# that Datadog can parse natively, including trace/span correlation.
#
# Required env vars on the Lambda:
#   DD_LOGS_ENABLED = "true"
#   DD_LOGS_INJECTION = "true"   (extension injects trace context into JSON logs)

class _DDJsonFormatter(logging.Formatter):
    """
    Structured JSON log formatter with Datadog trace correlation.

    Outputs one JSON object per line to stdout.  The Datadog Lambda Extension
    picks this up and ships it directly to Datadog Logs (bypassing CloudWatch).

    Fields added to every record:
      ts       - ISO-8601 timestamp
      level    - log level name
      logger   - logger name
      message  - formatted log message
      dd       - Datadog metadata (service, env, version, trace_id, span_id)
      error    - exception info (only when exc_info is set)
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts":      self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%03dZ"),
            "level":   record.levelname,
            "logger":  record.name,
            "message": record.getMessage(),
            "dd": {
                "service": os.environ.get("DD_SERVICE", "smb-inbox-triage"),
                "env":     os.environ.get("DD_ENV",     "dev"),
                "version": os.environ.get("DD_VERSION", "unknown"),
            },
        }

        # Inject OTel trace context for log ↔ trace correlation in Datadog
        if _OTEL_AVAILABLE:
            try:
                from opentelemetry import trace as _t
                ctx = _t.get_current_span().get_span_context()
                if ctx.is_valid:
                    # Datadog expects the LOW 64 bits of the 128-bit trace_id
                    payload["dd"]["trace_id"] = str(ctx.trace_id & 0xFFFF_FFFF_FFFF_FFFF)
                    payload["dd"]["span_id"]  = str(ctx.span_id)
            except Exception:
                pass

        # Carry any extra={} kwargs from log calls into the JSON payload
        _RESERVED = frozenset(logging.LogRecord.__dict__.keys()) | {
            "message", "asctime", "levelname", "name", "msg", "args",
            "exc_info", "exc_text", "stack_info",
        }
        for key, val in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = val

        if record.exc_info:
            payload["error"] = {
                "kind":    record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "stack":   self.formatException(record.exc_info),
            }

        return _json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """
    Configure the root logger for direct Datadog log shipping.

    On AWS Lambda, the Datadog Extension layer captures this stdout JSON and
    forwards it directly to Datadog Logs — no CloudWatch → Datadog integration
    needed (which saves cost and eliminates the forwarding latency).

    On Azure Functions and GCP Cloud Functions the JSON goes to the platform's
    stdout collector; configure the Datadog Forwarder or log sink from there,
    OR use the OTLP log exporter path (set up via _setup_otlp_log_handler).

    Call once at Lambda/Function cold start before any other log statements.
    The module calls this automatically when OBSERVABILITY_ENABLED=true.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(_DDJsonFormatter())
    root = logging.getLogger()
    root.setLevel(level)
    # Replace existing handlers to avoid duplicate output; the OTel handler
    # (_setup_otlp_log_handler) is re-added separately.
    root.handlers = [handler]


# ── Module-level setup ─────────────────────────────────────────────────────────

tracer: "trace.Tracer | None" = None

# OTLP log export is opt-in and OFF by default. Datadog's OTLP receiver — both the
# Lambda Extension and the Agent sidecar — does NOT accept OTLP logs unless logs
# ingestion is explicitly enabled (DD_LOGS_ENABLED + DD_OTLP_CONFIG_LOGS_ENABLED);
# otherwise POSTs to /v1/logs return 404 and the exporter logs an ERROR on every
# flush. On AWS, logs already ship via the stdout JSON path captured by the
# Extension, so OTLP log export is redundant. Enable it only where the receiver is
# configured for logs, by setting OTEL_LOGS_EXPORTER_ENABLED=true.
_OTLP_LOGS_EXPORT_ENABLED = (
    os.environ.get("OTEL_LOGS_EXPORTER_ENABLED", "false").lower()
    in ("true", "1", "yes")
)

if _ENABLED:
    configure_logging()          # JSON stdout → DD Lambda extension (no CWL)
    tracer = _setup_tracer()     # OTel traces  → OTLP → Datadog APM
    meter  = _setup_meter()      # OTel metrics → OTLP → Datadog (delta temporality)
    if _OTLP_LOGS_EXPORT_ENABLED:
        _setup_otlp_log_handler()  # OTel logs → OTLP → Datadog Logs (opt-in only)


# ── Explicit flush — must be called at the end of each handler ───────────────
#
# BatchSpanProcessor/BatchLogRecordProcessor buffer spans and flush via a
# background thread.  Cloud Functions containers are *frozen* between
# invocations, so that thread never wakes unless we force it here.
# Call flush() before every return path in the handler to ensure all spans
# and logs are shipped to Datadog before the container is suspended.

def flush(timeout_ms: int = 5000) -> None:
    """
    Force-flush all pending OTel spans and log records to Datadog.

    Must be called before returning from each Cloud Functions handler.
    Safe to call when OTel is disabled — returns immediately.

    Args:
        timeout_ms: Maximum milliseconds to wait for the flush to complete.
                    Default 5 s leaves headroom inside the function timeout.
    """
    if not _OTEL_AVAILABLE:
        return
    try:
        from opentelemetry import trace as _t
        provider = _t.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            ok = provider.force_flush(timeout_millis=timeout_ms)
            if ok:
                logger.info("OTel trace flush succeeded")
            else:
                logger.warning(
                    "OTel trace flush timed out after %dms — spans may be lost", timeout_ms
                )
    except Exception as exc:
        logger.warning("OTel trace flush error: %s", exc)

    if _OTLP_LOGS_EXPORT_ENABLED and _OTEL_LOGS_AVAILABLE:
        try:
            from opentelemetry._logs import get_logger_provider
            log_provider = get_logger_provider()
            if hasattr(log_provider, "force_flush"):
                ok = log_provider.force_flush(timeout_millis=timeout_ms)
                if not ok:
                    logger.warning(
                        "OTel log flush timed out after %dms", timeout_ms
                    )
        except Exception as exc:
            logger.warning("OTel log flush error: %s", exc)

    # Metrics flush — same FaaS freeze problem applies to PeriodicExportingMetricReader.
    if _OTEL_METRICS_AVAILABLE and _meter_provider is not None:
        try:
            ok = _meter_provider.force_flush(timeout_millis=timeout_ms)
            if not ok:
                logger.warning(
                    "OTel metric flush timed out after %dms", timeout_ms
                )
        except Exception as exc:
            logger.warning("OTel metric flush error: %s", exc)


# ── Null-safe span context manager ────────────────────────────────────────────

class _NullSpan:
    """No-op span returned when OTel is disabled — all methods are safe to call."""
    def set_attribute(self, key: str, value: Any) -> None: ...
    def set_status(self, *args: Any, **kwargs: Any) -> None: ...
    def record_exception(self, *args: Any, **kwargs: Any) -> None: ...
    def get_span_context(self) -> Any:
        class _NullCtx:
            is_valid = False
        return _NullCtx()


@contextmanager
def span(
    name: str,
    **attrs: str | int | float | bool,
) -> Generator["Span | _NullSpan", None, None]:
    """
    Context manager that starts an OTel span and yields it.

    The span is yielded so callers can set attributes discovered AFTER the
    wrapped call completes (e.g. token counts, finish reasons):

        with span("gen_ai.bedrock.invoke", **{"gen_ai.system": "aws.bedrock"}) as s:
            response = client.converse(...)
            s.set_attribute("gen_ai.usage.input_tokens", response["usage"]["inputTokens"])

    When OTel is disabled or unavailable, a no-op _NullSpan is yielded —
    all set_attribute() calls are safe and silent.
    """
    if tracer is None:
        yield _NullSpan()
        return

    with tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            s.set_attribute(k, v)
        try:
            yield s
        except Exception as exc:
            s.set_status(StatusCode.ERROR, str(exc))
            raise


# ── Semantic span helpers ──────────────────────────────────────────────────────

def record_llm_call(record: "ClassificationRecord") -> None:
    """
    Annotate the CURRENT ACTIVE SPAN with classification outcome attributes.

    Call this from inside an active span context (e.g. inside handler.py's
    ``with span("classifier.classify_email")`` block).  The adapter-level span
    (gen_ai.<provider>.invoke) sets the ``gen_ai.*`` LLM Observability attrs;
    this helper adds the application-layer classification result attrs to the
    parent span so both appear in Datadog APM.
    """
    if not _OTEL_AVAILABLE:
        return
    try:
        from opentelemetry import trace as _t
        s = _t.get_current_span()
        s.set_attribute("classification.intent",        record.result.intent.value)
        s.set_attribute("classification.urgency",       record.result.urgency.value)
        s.set_attribute("classification.confidence",    record.result.confidence)
        s.set_attribute("classification.requires_human", record.result.requires_human)
        s.set_attribute("classification.latency_ms",    record.latency_ms)
        s.set_attribute("classification.record_id",     record.record_id)
        s.set_attribute("email.message_id",             record.email.message_id)
        s.set_attribute("email.source",                 record.email.source)
    except Exception as exc:
        logger.debug("record_llm_call annotation failed: %s", exc)


def record_routing(record: "ClassificationRecord", decision: "RoutingDecision") -> None:
    """
    Annotate the CURRENT ACTIVE SPAN with routing decision attributes.

    Call from inside ``with span("router.dispatch")`` in destinations.py.
    """
    if not _OTEL_AVAILABLE:
        return
    try:
        from opentelemetry import trace as _t
        s = _t.get_current_span()
        s.set_attribute("routing.destination",    decision.destination)
        s.set_attribute("routing.channel",        decision.channel_or_queue)
        s.set_attribute("routing.create_ticket",  decision.create_ticket)
        s.set_attribute("routing.notify_owner",   decision.notify_owner)
        s.set_attribute("classification.intent",  record.result.intent.value)
        s.set_attribute("classification.record_id", record.record_id)
    except Exception as exc:
        logger.debug("record_routing annotation failed: %s", exc)


def record_feedback(record_id: str, corrected_intent: str, reviewer: str) -> None:
    """Emit a feedback-correction span for the human review loop."""
    if tracer is None:
        return
    try:
        with tracer.start_as_current_span("feedback.correction") as s:
            s.set_attribute("feedback.record_id",        record_id)
            s.set_attribute("feedback.corrected_intent", corrected_intent)
            s.set_attribute("feedback.reviewer",         reviewer)
    except Exception as exc:
        logger.debug("record_feedback span failed: %s", exc)
