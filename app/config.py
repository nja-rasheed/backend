import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "CivicAgent Backend"
    SECRET_KEY: str = "default_hackathon_secret_key"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    GEMINI_API_KEYS: str = ""
    GEMINI_API_KEY: str = ""
    DATABASE_URL: str = "sqlite:///./gov_agent.db"
    UPLOAD_DIR: str = "./uploads"
    MAX_UPLOAD_SIZE_MB: int = 5
    GEMINI_DISCOVERY_GROUNDING_ENABLED: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)