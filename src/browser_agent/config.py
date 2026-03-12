"""Configuration loader — reads config.yml."""

from pathlib import Path
from dataclasses import dataclass, field

import yaml


CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.yml"


@dataclass
class TelegramConfig:
    bot_token: str = ""
    allowed_users: list[int] = field(default_factory=list)


@dataclass
class LLMConfig:
    model: str = "qwen3.5-27b"
    api_base: str = "http://localhost:1234/v1"
    vision_enabled: bool = True
    max_tokens: int = 512
    temperature: float = 0.3


@dataclass
class BrowserConfig:
    ws_port: int = 7331
    visible: bool = False


@dataclass
class AgentConfig:
    max_steps: int = 30


@dataclass
class Config:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)


def load_config(path: Path | str | None = None) -> Config:
    """Load config from YAML file. Returns defaults if file doesn't exist."""
    path = Path(path) if path else CONFIG_PATH
    if not path.is_file():
        return Config()

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    tg = raw.get("telegram", {}) or {}
    llm = raw.get("llm", {}) or {}
    br = raw.get("browser", {}) or {}
    ag = raw.get("agent", {}) or {}

    return Config(
        telegram=TelegramConfig(
            bot_token=tg.get("bot_token", ""),
            allowed_users=tg.get("allowed_users", []) or [],
        ),
        llm=LLMConfig(
            model=llm.get("model", "qwen3.5-27b"),
            api_base=llm.get("api_base", "http://localhost:1234/v1"),
            vision_enabled=llm.get("vision_enabled", True),
            max_tokens=llm.get("max_tokens", 512),
            temperature=llm.get("temperature", 0.3),
        ),
        browser=BrowserConfig(
            ws_port=br.get("ws_port", 7331),
            visible=br.get("visible", False),
        ),
        agent=AgentConfig(
            max_steps=ag.get("max_steps", 30),
        ),
    )
