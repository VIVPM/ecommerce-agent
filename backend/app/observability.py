"""OpenTelemetry LLM tracing through Langfuse OTLP ingestion."""
import base64
import json
import logging
import os
import sys
from contextlib import contextmanager
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_llm_provider = None
_llm_tracer = None
_CLOUD_HOSTS = {"cloud.langfuse.com", "us.cloud.langfuse.com", "eu.cloud.langfuse.com"}
_PROPAGATED = (
    "langfuse.trace.name", "langfuse.user.id", "langfuse.session.id",
    "langfuse.release", "langfuse.version", "langfuse.environment",
    "langfuse.trace.tags",
)


def _have_langfuse() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def _resource():
    from opentelemetry.sdk.resources import Resource

    return Resource.create({
        "service.name": os.getenv("OTEL_SERVICE_NAME", "ecommerce-agent-backend"),
        "service.namespace": "ecommerce-agent",
        "deployment.environment": os.getenv("DEPLOYMENT_ENV", "development"),
    })


def _context_processor():
    """Copy trace-wide attributes onto each child before it is recorded. Reading
    the local parent context avoids sending user/session IDs in HTTP baggage to
    model providers."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import SpanProcessor

    class TraceContextProcessor(SpanProcessor):
        def on_start(self, span, parent_context=None):
            span.set_attribute("langfuse.environment", os.getenv("DEPLOYMENT_ENV", "development"))
            release = os.getenv("APP_RELEASE") or os.getenv("RENDER_GIT_COMMIT")
            if release:
                span.set_attribute("langfuse.release", release)
            parent = trace.get_current_span(parent_context)
            attributes = getattr(parent, "attributes", None) or {}
            for key in _PROPAGATED:
                value = attributes.get(key)
                if value is not None:
                    span.set_attribute(key, value)
            for key, value in attributes.items():
                if key.startswith("langfuse.trace.metadata."):
                    span.set_attribute(key, value)

        def on_end(self, span):
            pass

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    return TraceContextProcessor()


def _otlp_config(host: str) -> dict:
    """Langfuse OTLP trace endpoint and Basic Auth; never log or persist keys."""
    creds = f'{os.environ["LANGFUSE_PUBLIC_KEY"]}:{os.environ["LANGFUSE_SECRET_KEY"]}'
    auth = base64.b64encode(creds.encode()).decode()
    return {"endpoint": f"{host}/api/public/otel/v1/traces",
            "headers": {"Authorization": f"Basic {auth}",
                        "x-langfuse-ingestion-version": "4"}}


def init_observability():
    """Instrument google-genai and LangChain; export LLM spans to Langfuse."""
    global _llm_provider, _llm_tracer
    if not _have_langfuse():
        logger.info("LLM tracing disabled (no Langfuse env).")
        return
    if _llm_provider is not None:
        return
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    if (urlparse(host).hostname not in _CLOUD_HOSTS
            and os.getenv("LANGFUSE_OTLP_HOST_CONFIRMED") != "1"):
        logger.warning("LLM tracing disabled: confirm the custom host supports "
                       "ingestion header version 4 before setting "
                       "LANGFUSE_OTLP_HOST_CONFIRMED=1.")
        return
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
        from openinference.instrumentation.langchain import LangChainInstrumentor

        provider = TracerProvider(resource=_resource())
        provider.add_span_processor(_context_processor())
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**_otlp_config(host))))

        LangChainInstrumentor().instrument(tracer_provider=provider)
        GoogleGenAIInstrumentor().instrument(tracer_provider=provider)
        _llm_provider = provider
        _llm_tracer = provider.get_tracer("chat")
        logger.info("LLM tracing enabled via Langfuse OTLP (%s)", host)
    except Exception:
        logger.exception("LLM tracing init failed — continuing without it.")


@contextmanager
def trace_message(question: str, user_id, session_id, job_id=None, name="chat-message"):
    """One root observation per operation. Children inherit trace-wide fields;
    application exceptions propagate rather than being hidden by the tracer."""
    if _llm_tracer is None:
        yield None
        return
    manager = None
    try:
        manager = _llm_tracer.start_as_current_span(name)
        span = manager.__enter__()
        span.set_attribute("langfuse.trace.name", name)
        if user_id is not None:
            span.set_attribute("langfuse.user.id", str(user_id))
        if session_id is not None:
            span.set_attribute("langfuse.session.id", str(session_id))
        if job_id is not None:
            span.set_attribute("langfuse.trace.metadata.job_id", str(job_id))
        span.set_attribute("langfuse.observation.input", json.dumps(question, ensure_ascii=False))
        span.set_attribute("input.value", question)
    except Exception as e:
        logger.warning("Tracing start failed — continuing untraced: %s", e)
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass
        yield None
        return
    try:
        yield span
    finally:
        try:
            manager.__exit__(*sys.exc_info())
        except Exception as e:
            logger.warning("Tracing end failed — continuing: %s", e)


def set_usage(span, *, provider, tokens_in, tokens_out, cached, cost_usd, ttft_ms, tool):
    """Record whole-job estimates as root metadata, not generation usage/cost:
    instrumented generation children have their own usage and must not be counted twice."""
    if span is None:
        return
    try:
        span.set_attribute("langfuse.observation.metadata.provider", provider or "unknown")
        span.set_attribute("langfuse.observation.metadata.input_tokens_total", tokens_in)
        span.set_attribute("langfuse.observation.metadata.output_tokens_total", tokens_out)
        span.set_attribute("langfuse.observation.metadata.cached_tokens_total", cached)
        span.set_attribute("langfuse.observation.metadata.cost_estimate_usd", cost_usd)
        span.set_attribute("langfuse.observation.metadata.cache_hit", tokens_in == 0)
        span.set_attribute("langfuse.observation.metadata.tool", tool or "unknown")
        if ttft_ms is not None:
            span.set_attribute("langfuse.observation.metadata.ttft_ms", ttft_ms)
    except Exception as e:
        logger.debug("set_usage failed: %s", e)


def set_output(span, text: str):
    """Attach the final shopper answer to the root observation."""
    if span is None:
        return
    try:
        span.set_attribute("langfuse.observation.output", json.dumps(text, ensure_ascii=False))
        span.set_attribute("output.value", text)
    except Exception as e:
        logger.debug("set_output failed: %s", e)


def flush():
    """Force-send buffered spans after a job; Render may suspend the instance."""
    if _llm_provider is None:
        return
    try:
        _llm_provider.force_flush()
    except Exception as e:
        logger.debug("flush failed: %s", e)


def shutdown():
    """Drain the exporter after the worker stops, before the process exits."""
    global _llm_provider, _llm_tracer
    provider, _llm_provider, _llm_tracer = _llm_provider, None, None
    if provider is not None:
        try:
            provider.shutdown()
        except Exception as e:
            logger.debug("tracing shutdown failed: %s", e)
