"""Transform manager — dispatches to the right transformer by API type.

Vendored from polar.gateway.transform (import prefixes rewritten to capgw).
"""

from capgw.detection import APIType
from capgw.transform.anthropic import AnthropicTransformer
from capgw.transform.base import BaseTransformer
from capgw.transform.google import GoogleTransformer
from capgw.transform.openai_chat import OpenAIChatTransformer
from capgw.transform.openai_responses import OpenAIResponsesTransformer


class TransformManager:
    """Route to the correct transformer based on detected API type."""

    def __init__(self):
        self._transformers: dict[APIType, BaseTransformer] = {
            APIType.ANTHROPIC: AnthropicTransformer(),
            APIType.OPENAI_CHAT: OpenAIChatTransformer(),
            APIType.OPENAI_RESPONSES: OpenAIResponsesTransformer(),
            APIType.GOOGLE: GoogleTransformer(),
        }

    def get(self, api_type: APIType) -> BaseTransformer:
        return self._transformers[api_type]
