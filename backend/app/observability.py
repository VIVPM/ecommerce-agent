"""OpenTelemetry tracing and metrics.

LLM spans export to Langfuse and Grafana off one provider; HTTP spans use a
separate provider so they don't also land in Langfuse. Each backend stays off
unless its env vars are set (LANGFUSE_* / GRAFANA_OTLP_*), and nothing here
raises — tracing must never break a request.
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
    """Instrument google-genai; export LLM spans to whichever backends are configured."""
    global _llm_provider, _llm_tracer
    if not (_have_langfuse() or _have_grafana()):
        logger.info("LLM tracing disabled (no Langfuse/Grafana env).")
        return
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
        from openinference.instrumentation.langchain import LangChainInstrumentor

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

        # Two instrumentors, no overlap: LangChain covers the agent — routing,
        # tool spans and every generation call — while google-genai covers the
        # embedding calls, which talk to the SDK directly and never go through
        # LangChain (the Pinecone index is 1024-dim gemini-embedding-001).
        LangChainInstrumentor().instrument(tracer_provider=provider)
        GoogleGenAIInstrumentor().instrument(tracer_provider=provider)
        _llm_provider = provider
        _llm_tracer = provider.get_tracer("chat")
        logger.info("LLM tracing enabled via OTLP: %s", ", ".join(enabled))
    except Exception:
        logger.exception("LLM tracing init failed — continuing without it.")


@contextmanager
def trace_message(question: str, user_id, session_id):
    """Wrap one message so its LLM calls share a trace. Yields the span, or None if off."""
    if _llm_tracer is None:
        yield None
        return
    try:
        with _llm_tracer.start_as_current_span("chat-message") as span:
            span.set_attribute("langfuse.user.id", str(user_id))
            span.set_attribute("langfuse.session.id", str(session_id))
            span.set_attribute("input.value", question)
            yield span
    except Exception as e:
        logger.warning("trace_message failed — continuing untraced: %s", e)
        yield None


def set_usage(span, *, provider, tokens_in, tokens_out, cached, cost_usd, ttft_ms, tool):
    """Attach what a run COST and how fast it felt, not just what it said.

    Tokens and latency come free from the instrumentor; cost, cache-hit and TTFT
    do not — and those are the three you want when traffic climbs. TTFT is kept
    separate from total duration because on a streaming UI it is the number the
    user actually experiences.
    """
    if span is None:
        return
    try:
        span.set_attribute("llm.provider", provider or "unknown")
        span.set_attribute("llm.token_count.prompt", tokens_in)
        span.set_attribute("llm.token_count.completion", tokens_out)
        span.set_attribute("llm.token_count.cache_read", cached)
        span.set_attribute("llm.cost.usd", cost_usd)
        span.set_attribute("llm.cache.hit", tokens_in == 0)
        span.set_attribute("agent.tool", tool or "unknown")
        if ttft_ms is not None:
            span.set_attribute("agent.ttft_ms", ttft_ms)
    except Exception as e:
        logger.debug("set_usage failed: %s", e)


def set_output(span, text: str):
    """Attach the final answer to the message span."""
    if span is None:
        return
    try:
        span.set_attribute("output.value", text)
    except Exception as e:
        logger.debug("set_output failed: %s", e)


def flush():
    """Force-send buffered spans. Render can freeze the instance and drop the last trace."""
    if _llm_provider is None:
        return
    try:
        _llm_provider.force_flush()
    except Exception as e:
        logger.debug("flush failed: %s", e)


def init_http_tracing(app):
    """Trace every HTTP endpoint to Grafana, on its own provider so Langfuse stays LLM-only."""
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
    """Export a chat_messages_total counter. A metric, not traces, so alerts are plain PromQL."""
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
    """Increment the message counter."""
    if _message_counter is None:
        return
    try:
        _message_counter.add(1, {"status": status, "tool": tool})
    except Exception as e:
        logger.debug("record_message failed: %s", e)
