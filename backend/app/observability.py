"""Observability — OTLP tracing (Langfuse + Grafana) + a counter + structured logs.

Same procedure as the leads-coordinator (backend/worker.py), adapted only where
the LLM SDK differs (google-genai here vs CrewAI/LiteLLM there):

  * LLM spans — the openinference google-genai instrumentor emits spans onto ONE
    unified tracer provider that exports to BOTH Langfuse's OTLP endpoint and
    Grafana Cloud (two BatchSpanProcessors). No `langfuse` package: we build the
    Langfuse Basic-auth header from public:secret and POST straight to its OTLP
    API, exactly as the coordinator does.
  * HTTP spans — a SEPARATE tracer provider -> Grafana only (FastAPIInstrumentor),
    so endpoint spans don't get double-exported to Langfuse.
  * chat_messages_total counter -> Grafana /v1/metrics, for alerting.

Every backend is OFF unless its env vars are set (LANGFUSE_* / GRAFANA_OTLP_*),
and init is non-fatal — a tracing/network problem can never break a request.
"""
import base64
import logging
import os
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_llm_provider = None   # unified TracerProvider for LLM spans, or None when disabled
_llm_tracer = None     # tracer from that provider (for the per-message parent span)
_message_counter = None


def _have_langfuse() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def _have_grafana() -> bool:
    return bool(os.getenv("GRAFANA_OTLP_ENDPOINT") and os.getenv("GRAFANA_OTLP_AUTH"))


def _resource():
    from opentelemetry.sdk.resources import Resource

    return Resource.create({
        "service.name": os.getenv("OTEL_SERVICE_NAME", "ecommerce-agent-backend"),
        "service.namespace": "ecommerce-agent",
        "deployment.environment": os.getenv("DEPLOYMENT_ENV", "development"),
    })


def init_observability():
    """LLM tracing via OTLP to Langfuse and/or Grafana on one unified provider —
    the coordinator's pattern. No-op unless a backend is configured. Never fatal."""
    global _llm_provider, _llm_tracer
    if not (_have_langfuse() or _have_grafana()):
        logger.info("LLM tracing disabled (no Langfuse/Grafana env).")
        return
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor

        provider = TracerProvider(resource=_resource())
        enabled = []

        if _have_langfuse():
            host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
            creds = f'{os.environ["LANGFUSE_PUBLIC_KEY"]}:{os.environ["LANGFUSE_SECRET_KEY"]}'
            auth = base64.b64encode(creds.encode()).decode()
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
                endpoint=f"{host}/api/public/otel/v1/traces",
                headers={"Authorization": f"Basic {auth}"},
            )))
            enabled.append(f"Langfuse ({host})")

        if _have_grafana():
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
                endpoint=f"{os.environ['GRAFANA_OTLP_ENDPOINT'].rstrip('/')}/v1/traces",
                headers={"Authorization": os.environ["GRAFANA_OTLP_AUTH"]},
            )))
            enabled.append("Grafana Cloud")

        GoogleGenAIInstrumentor().instrument(tracer_provider=provider)
        _llm_provider = provider
        _llm_tracer = provider.get_tracer("chat")
        logger.info("LLM tracing enabled via OTLP: %s", ", ".join(enabled))
    except Exception:
        logger.exception("LLM tracing init failed — continuing without it.")


@contextmanager
def trace_message(question: str, user_id, session_id):
    """Group one message's LLM calls into a single trace, tagged with user /
    session / question. google-genai has no natural root span (unlike CrewAI,
    which gives the coordinator one per crew run), so we open one here. Yields the
    span for setting output, or None when disabled. Never raises."""
    if _llm_tracer is None:
        yield None
        return
    try:
        with _llm_tracer.start_as_current_span("chat-message") as span:
            # Langfuse reads these OTel attributes off the span.
            span.set_attribute("langfuse.user.id", str(user_id))
            span.set_attribute("langfuse.session.id", str(session_id))
            span.set_attribute("input.value", question)
            yield span
    except Exception as e:
        logger.warning("trace_message failed — continuing untraced: %s", e)
        yield None


def set_output(span, text: str):
    """Attach the final answer to the message span (Langfuse output). No-op if None."""
    if span is None:
        return
    try:
        span.set_attribute("output.value", text)
    except Exception as e:
        logger.debug("set_output failed: %s", e)


def flush():
    """Force-send buffered spans. Render can freeze the instance between requests,
    so BatchSpanProcessor's periodic flush could otherwise drop the last trace."""
    if _llm_provider is None:
        return
    try:
        _llm_provider.force_flush()
    except Exception as e:
        logger.debug("flush failed: %s", e)


# ---------------------------------------------------------------------------
# Grafana Cloud (OTLP) — HTTP-layer tracing + a metric for alerting.
# ---------------------------------------------------------------------------

def init_http_tracing(app):
    """Trace every HTTP endpoint (auth, chat CRUD, saved products) to Grafana
    Cloud on a SEPARATE tracer provider, so HTTP spans don't also hit Langfuse.
    No-op unless GRAFANA_* env vars are set. Never fatal."""
    if not _have_grafana():
        logger.info("Grafana HTTP tracing disabled (GRAFANA_OTLP_* not set).")
        return
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        provider = TracerProvider(resource=_resource())
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=f"{os.environ['GRAFANA_OTLP_ENDPOINT'].rstrip('/')}/v1/traces",
            headers={"Authorization": os.environ["GRAFANA_OTLP_AUTH"]},
        )))
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
        logger.info("Grafana HTTP tracing enabled via OTLP.")
    except Exception:
        logger.exception("Grafana HTTP tracing init failed — continuing without it.")


def init_metrics():
    """A chat_messages_total counter exported to Grafana Cloud. A counter is what
    alerting actually wants: 'no messages in N minutes' and 'error rate high' are
    both trivial PromQL, where the same signal from traces needs TraceQL alerting.
    Mirrors the coordinator's jobs_processed_total. No-op unless GRAFANA_* set."""
    global _message_counter
    if not _have_grafana():
        return
    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(
                endpoint=f"{os.environ['GRAFANA_OTLP_ENDPOINT'].rstrip('/')}/v1/metrics",
                headers={"Authorization": os.environ["GRAFANA_OTLP_AUTH"]},
            ),
            export_interval_millis=15000,
        )
        provider = MeterProvider(resource=_resource(), metric_readers=[reader])
        _message_counter = provider.get_meter("chat").create_counter(
            "chat_messages_total",
            description="Chat messages handled, by status (ok/error) and routed tool",
        )
        logger.info("Grafana metrics enabled via OTLP.")
    except Exception:
        logger.exception("Grafana metrics init failed — continuing without it.")


def record_message(status: str, tool: str = "unknown"):
    """Increment the message counter. No-op when metrics are disabled. Never raises."""
    if _message_counter is None:
        return
    try:
        _message_counter.add(1, {"status": status, "tool": tool})
    except Exception as e:
        logger.debug("record_message failed: %s", e)
