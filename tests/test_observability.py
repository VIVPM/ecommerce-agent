"""Langfuse OTLP contract without network calls or project credentials."""
import base64
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app import observability  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402


class LangfuseTracingTest(unittest.TestCase):
    def test_export_config_uses_current_ingestion_header_without_exposing_keys(self):
        keys = {"LANGFUSE_PUBLIC_KEY": "pk-test", "LANGFUSE_SECRET_KEY": "sk-test"}
        with mock.patch.dict(os.environ, keys):
            config = observability._otlp_config("https://us.cloud.langfuse.com")
        self.assertEqual(config["endpoint"],
                         "https://us.cloud.langfuse.com/api/public/otel/v1/traces")
        self.assertEqual(config["headers"]["x-langfuse-ingestion-version"], "4")
        self.assertEqual(config["headers"]["Authorization"],
                         "Basic " + base64.b64encode(b"pk-test:sk-test").decode())
        self.assertNotIn("sk-test", str(config))

    def test_root_and_generation_share_filter_attributes_without_duplicate_cost(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(observability._context_processor())
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")
        with mock.patch.object(observability, "_llm_tracer", tracer), \
             mock.patch.dict(os.environ, {"DEPLOYMENT_ENV": "test", "APP_RELEASE": "sha-test"}):
            with observability.trace_message("Nike shoes", 42, "chat-1", job_id="job-1") as root:
                with tracer.start_as_current_span("generation") as generation:
                    generation.set_attribute("gen_ai.request.model", "gemini-2.5-flash")
                    generation.set_attribute("gen_ai.usage.input_tokens", 11)
                    with tracer.start_as_current_span("tool"):
                        pass
                observability.set_usage(root, provider="GEMINI", tokens_in=11, tokens_out=3,
                                        cached=0, cost_usd=0.00001, ttft_ms=100, tool="search")
                observability.set_output(root, "Here are shoes")
        spans = {s.name: s for s in exporter.get_finished_spans()}
        self.assertEqual(set(spans), {"chat-message", "generation", "tool"})
        root = spans["chat-message"].attributes
        self.assertEqual(json.loads(root["langfuse.observation.input"]), "Nike shoes")
        self.assertEqual(json.loads(root["langfuse.observation.output"]), "Here are shoes")
        self.assertEqual(root["langfuse.observation.metadata.input_tokens_total"], 11)
        self.assertNotIn("langfuse.observation.cost_details", root)
        self.assertNotIn("llm.token_count.prompt", root)
        self.assertNotIn("gen_ai.usage.cost", root)
        for name in ("generation", "tool"):
            attrs = spans[name].attributes
            self.assertEqual(attrs["langfuse.user.id"], "42")
            self.assertEqual(attrs["langfuse.session.id"], "chat-1")
            self.assertEqual(attrs["langfuse.trace.name"], "chat-message")
            self.assertEqual(attrs["langfuse.trace.metadata.job_id"], "job-1")
            self.assertEqual(attrs["langfuse.environment"], "test")
            self.assertEqual(attrs["langfuse.release"], "sha-test")
            self.assertNotIn("langfuse.observation.metadata.cost_estimate_usd", attrs)
        self.assertEqual(spans["generation"].attributes["gen_ai.usage.input_tokens"], 11)
        provider.shutdown()

    def test_vision_preprocessing_has_its_own_session_scoped_root(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(observability._context_processor())
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")
        with mock.patch.object(observability, "_llm_tracer", tracer):
            with observability.trace_message("search this shoe", 7, "chat-7",
                                               name="image-recognition") as root:
                with tracer.start_as_current_span("vision-model") as generation:
                    generation.set_attribute("llm.model_name", "gemini-2.5-flash")
                observability.set_output(root, "running shoes")
        spans = {s.name: s for s in exporter.get_finished_spans()}
        self.assertEqual(spans["vision-model"].attributes["langfuse.session.id"], "chat-7")
        self.assertEqual(spans["vision-model"].attributes["langfuse.trace.name"], "image-recognition")
        self.assertEqual(json.loads(spans["image-recognition"].attributes[
            "langfuse.observation.output"]), "running shoes")
        provider.shutdown()

    def test_application_error_is_not_hidden_by_trace_context(self):
        provider = TracerProvider()
        provider.add_span_processor(observability._context_processor())
        with mock.patch.object(observability, "_llm_tracer", provider.get_tracer("test")):
            with self.assertRaisesRegex(ValueError, "application failed"):
                with observability.trace_message("question", 1, "chat"):
                    raise ValueError("application failed")
        provider.shutdown()

    def test_tracing_failure_does_not_interrupt_application(self):
        broken = mock.Mock()
        broken.start_as_current_span.side_effect = RuntimeError("tracer unavailable")
        with mock.patch.object(observability, "_llm_tracer", broken):
            with observability.trace_message("question", 1, "chat") as span:
                self.assertIsNone(span)
                result = "application continued"
        self.assertEqual(result, "application continued")

    def test_cloud_init_configures_v4_exporter_without_network(self):
        keys = {"LANGFUSE_PUBLIC_KEY": "pk-test", "LANGFUSE_SECRET_KEY": "sk-test",
                "LANGFUSE_HOST": "https://us.cloud.langfuse.com"}
        with mock.patch.dict(os.environ, keys), \
             mock.patch.object(observability, "_llm_provider", None), \
             mock.patch.object(observability, "_llm_tracer", None), \
             mock.patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter") as exporter, \
             mock.patch("openinference.instrumentation.langchain.LangChainInstrumentor"), \
             mock.patch("openinference.instrumentation.google_genai.GoogleGenAIInstrumentor"):
            observability.init_observability()
            self.assertIsNotNone(observability._llm_tracer)
            self.assertEqual(exporter.call_args.kwargs["headers"]["x-langfuse-ingestion-version"], "4")
            observability.shutdown()
            self.assertIsNone(observability._llm_provider)

    def test_custom_host_requires_ingestion_confirmation(self):
        with mock.patch.dict(os.environ, {"LANGFUSE_PUBLIC_KEY": "pk-test",
                                       "LANGFUSE_SECRET_KEY": "sk-test",
                                       "LANGFUSE_HOST": "https://self-hosted.example.com"}, clear=False), \
             mock.patch.object(observability, "_llm_provider", None), \
             mock.patch.object(observability, "_llm_tracer", None):
            os.environ.pop("LANGFUSE_OTLP_HOST_CONFIRMED", None)
            observability.init_observability()
            self.assertIsNone(observability._llm_tracer)


if __name__ == "__main__":
    unittest.main()
