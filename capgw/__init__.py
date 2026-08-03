"""Capture Gateway (capgw).

A transparent, self-contained capturing proxy. Point it at one upstream
OpenAI-compatible endpoint (endpoint + model + key) and it exposes an
Anthropic / OpenAI-Chat / OpenAI-Responses / Gemini compatible surface to any
agent, transparently forwards each call to the upstream model, and records the
full interaction (including chain-of-thought / reasoning) to disk for
trajectory and data collection.
"""

__version__ = "0.1.0"
