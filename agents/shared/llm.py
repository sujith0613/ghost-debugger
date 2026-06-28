import os
import logging
import subprocess
from functools import lru_cache

logger = logging.getLogger(__name__)


def _get_local_model() -> str:
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.strip().split("\n")[1:]
        models = [line.split()[0] for line in lines if line.strip()]
        if models:
            chosen = os.getenv("OLLAMA_MODEL", models[0])
            if chosen in models:
                return chosen
            return models[0]
        raise RuntimeError("No models found via 'ollama list'")
    except FileNotFoundError:
        raise RuntimeError("Ollama not found in PATH. Install from https://ollama.com")
    except Exception as e:
        raise RuntimeError(f"Failed to detect local Ollama model: {e}")


def get_llm():
    api_key = os.getenv("GOOGLE_API_KEY")
    if api_key:
        return _get_gemini_llm(api_key)
    return _get_ollama_llm()


def get_llm_with_tools(tools: list):
    llm = get_llm()
    if tools:
        return llm.bind_tools(tools)
    return llm


@lru_cache(maxsize=1)
def _get_gemini_llm(api_key: str):
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        google_api_key=api_key,
        temperature=0,
        max_retries=3,
        timeout=60,
    )


@lru_cache(maxsize=1)
def _get_ollama_llm():
    from langchain_ollama import ChatOllama
    model = _get_local_model()
    logger.info(f"Using local Ollama model: {model}")
    return ChatOllama(
        model=model,
        temperature=0,
        num_predict=4096,
    )
