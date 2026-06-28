import os
import logging
from functools import lru_cache

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.b3 import B3MultiFormat

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def init_tracing() -> TracerProvider:
    endpoint = os.getenv("JAEGER_ENDPOINT", "localhost:4317")

    exporter = OTLPSpanExporter(
        endpoint=f"http://{endpoint}",
        insecure=True,
    )

    resource = Resource.create({
        ResourceAttributes.SERVICE_NAME: "ghost-debugger-agents",
        ResourceAttributes.SERVICE_VERSION: "1.0.0",
        "component": "agent-pipeline",
    })

    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    set_global_textmap(B3MultiFormat())

    logger.info(f"Agent service OpenTelemetry initialized -> {endpoint}")
    return provider


def get_tracer(component: str) -> trace.Tracer:
    init_tracing()
    return trace.get_tracer(f"ghost-debugger/agents/{component}")
