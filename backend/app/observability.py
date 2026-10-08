"""Fail-open LLM tracing to Langfuse when configured."""
import base64
import logging
import os
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_llm_provider = None
_llm_tracer = None


def _have_langfuse() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def _resource():
    from opentelemetry.sdk.resources import Resource

    return Resource.create({
        "service.name": os.getenv("OTEL_SERVICE_NAME", "ecommerce-agent-backend"),
        "service.namespace": "ecommerce-agent",
        "deployment.environment": os.getenv("DEPLOYMENT_ENV", "development"),
    })


def init_observability():
    """Instrument google-genai; export LLM spans to Langfuse when configured."""
    global _llm_provider, _llm_tracer
    if not _have_langfuse():
        logger.info("LLM tracing disabled (no Langfuse env).")
        return
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor

        provider = TracerProvider(resource=_resource())
        host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
        creds = f'{os.environ["LANGFUSE_PUBLIC_KEY"]}:{os.environ["LANGFUSE_SECRET_KEY"]}'
        auth = base64.b64encode(creds.encode()).decode()
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=f"{host}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {auth}"},
        )))

        GoogleGenAIInstrumentor().instrument(tracer_provider=provider)
        _llm_provider = provider
        _llm_tracer = provider.get_tracer("chat")
        logger.info("LLM tracing enabled via Langfuse OTLP: %s", host)
    except Exception:
        logger.exception("LLM tracing init failed — continuing without it.")


@contextmanager
def trace_message(question: str, user_id, session_id):
    """Wrap one message so its LLM calls share a trace. Yields the span, or None if off."""
    if _llm_tracer is None:
        yield None
        return
    started = False
    try:
        with _llm_tracer.start_as_current_span("chat-message") as span:
            span.set_attribute("langfuse.user.id", str(user_id))
            span.set_attribute("langfuse.session.id", str(session_id))
            span.set_attribute("input.value", question)
            started = True
            yield span
    except Exception as e:
        if started:
            raise
        logger.warning("trace_message failed — continuing untraced: %s", e)
        yield None


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
