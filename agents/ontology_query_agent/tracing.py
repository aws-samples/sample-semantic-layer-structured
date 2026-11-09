"""OpenInference tracing bootstrap for the ontology query (VKG) agent.

Imported first in main.py so Strands' native spans are translated into
OpenInference-attributed spans (which Triton/Omni can display and evaluate)
before any Strands modules are imported. The OTLP exporter is configured with
NO endpoint — Omni injects OTEL_EXPORTER_OTLP_ENDPOINT at runtime.
"""

import inspect

from opentelemetry.sdk.trace import SpanProcessor, ReadableSpan
from opentelemetry.context import Context
from strands.telemetry import StrandsTelemetry
from openinference.instrumentation.strands_agents import StrandsAgentsToOpenInferenceProcessor


class CodeMetadataSpanProcessor(SpanProcessor):
    """Injects code file, line, and function name into span attributes."""

    def on_start(self, span: ReadableSpan, parent_context: Context = None) -> None:
        frame = inspect.currentframe()
        try:
            for _ in range(100):
                frame = frame.f_back
                if not frame:
                    break
                filename = frame.f_code.co_filename
                if 'telemetry' in filename or 'opentelemetry' in filename or 'instrumentation' in filename:
                    continue
                span.set_attribute('code.filepath', filename)
                span.set_attribute('code.lineno', frame.f_lineno)
                span.set_attribute('code.function', frame.f_code.co_name)
                break
        finally:
            del frame

    def on_end(self, span: ReadableSpan) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


telemetry = StrandsTelemetry()
telemetry.setup_otlp_exporter()
telemetry.tracer_provider.add_span_processor(StrandsAgentsToOpenInferenceProcessor())
telemetry.tracer_provider.add_span_processor(CodeMetadataSpanProcessor())
