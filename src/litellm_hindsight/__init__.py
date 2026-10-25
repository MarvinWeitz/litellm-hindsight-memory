"""Route LiteLLM proxy requests through Hindsight memory banks by request tags."""

from .handler import HindsightMemoryHandler

# LiteLLM's config points at a module-level instance:
#   litellm_settings:
#     callbacks: litellm_hindsight.proxy_handler_instance
proxy_handler_instance = HindsightMemoryHandler()

__all__ = ["HindsightMemoryHandler", "proxy_handler_instance"]
