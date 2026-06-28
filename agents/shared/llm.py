import os
import logging
from functools import lru_cache
from langchain_google_genai import ChatGoogleGenerativeAI

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_llm() -> ChatGoogleGenerativeAI:
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError(
            "GOOGLE_API_KEY environment variable not set. "
            "Get a key from https://aistudio.google.com/app/apikey"
        )

    return ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-1.5-flash"),
        google_api_key=api_key,
        temperature=0,
        max_retries=3,
        timeout=60,
    )


def get_llm_with_tools(tools: list) -> ChatGoogleGenerativeAI:
    llm = get_llm()
    if tools:
        return llm.bind_tools(tools)
    return llm
