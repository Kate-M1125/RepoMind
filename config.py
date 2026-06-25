from pathlib import Path
from pydantic import SecretStr, AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    deepseek_api_key: SecretStr
    github_token: SecretStr
    db_path: Path = Path("./repo_knowledge_base")
    clone_base: Path = Path("/tmp/repomind_repos")
    llm_model: str = "deepseek-chat"
    llm_base_url: str = "https://api.deepseek.com"
    repo_agent_url: str = "http://localhost:8001"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
