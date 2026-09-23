"""One place that knows which model we are talking to."""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "google": "gemini-2.0-flash",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-5",
}


def _key(name: str) -> str:
    """Read an API key, stripping whitespace.

    Hosting dashboards often carry a trailing newline or space through a paste.
    That makes the Authorization header invalid, and the SDK reports it as a
    generic connection error rather than an auth failure — so strip it here.
    """
    return (os.environ.get(name) or "").strip()


@lru_cache(maxsize=4)
def get_llm(temperature: float = 0.0):
    provider = (os.environ.get("LLM_PROVIDER") or "groq").strip().lower()
    model = (os.environ.get("LLM_MODEL") or "").strip() or DEFAULT_MODELS.get(provider)

    if provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=model, temperature=temperature,
                        api_key=_key("GROQ_API_KEY"))
    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model, temperature=temperature,
                                      google_api_key=_key("GOOGLE_API_KEY"))
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, temperature=temperature,
                          api_key=_key("OPENAI_API_KEY"))
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, temperature=temperature,
                             api_key=_key("ANTHROPIC_API_KEY"))

    raise ValueError(f"unknown LLM_PROVIDER: {provider}")

