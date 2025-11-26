from typing import Literal, Optional

from sqlmodel import Session

from app.util.cache import StringConfigCache


AIConfigKey = Literal[
    "ai_provider",
    "ai_endpoint",
    "ai_model",
    "ai_api_key",
    "ai_ollama_endpoint",
    "ai_ollama_model",
    "ai_openai_endpoint",
    "ai_openai_api_key",
]


class AIConfig(StringConfigCache[AIConfigKey]):
    """Configuration for AI-backed recommendations (Ollama or OpenAI-compatible)."""

    def get_provider(self, session: Session) -> str:
        return self.get(session, "ai_provider") or "ollama"

    def set_provider(self, session: Session, provider: str):
        self.set(session, "ai_provider", provider)

    def get_endpoint(self, session: Session) -> Optional[str]:
        provider = self.get_provider(session)
        if provider == "openai":
            ep = self.get(session, "ai_openai_endpoint") or self.get(session, "ai_endpoint")
            if not ep:
                ep = "https://api.openai.com"
        else:
            ep = (
                self.get(session, "ai_ollama_endpoint")
                or self.get(session, "ai_endpoint")
            )
        if ep:
            return ep.rstrip("/")
        return None

    def set_endpoint(self, session: Session, endpoint: str):
        provider = self.get_provider(session)
        if provider == "openai":
            self.set(session, "ai_openai_endpoint", endpoint)
        else:
            self.set(session, "ai_ollama_endpoint", endpoint)
        # Keep a generic copy for backward compatibility
        self.set(session, "ai_endpoint", endpoint)

    def get_model(self, session: Session) -> Optional[str]:
        return (
            self.get(session, "ai_model")
            or self.get(session, "ai_ollama_model")
        )

    def set_model(self, session: Session, model: str):
        self.set(session, "ai_model", model)
        self.set(session, "ai_ollama_model", model)

    def get_api_key(self, session: Session) -> Optional[str]:
        provider = self.get_provider(session)
        if provider == "openai":
            return self.get(session, "ai_openai_api_key") or self.get(session, "ai_api_key")
        return None

    def set_api_key(self, session: Session, api_key: str):
        provider = self.get_provider(session)
        if provider == "openai":
            self.set(session, "ai_openai_api_key", api_key)
            self.set(session, "ai_api_key", api_key)

    def is_configured(self, session: Session) -> bool:
        endpoint = self.get_endpoint(session)
        model = self.get_model(session)
        if not endpoint or not model:
            return False
        if self.get_provider(session) == "openai":
            return bool(self.get_api_key(session))
        return True


ai_config = AIConfig()
