"""
Config management for Delta cloud integration.
Reads/writes ~/.delta/config.toml
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import tomllib          # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib   # pip install tomli
    except ImportError:
        tomllib = None

CONFIG_DIR = Path.home() / ".delta"
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class CloudConfig:
    api_key: str
    api_url: str = "https://api.deltatest.dev"
    repo_id: Optional[str] = None
    branch: str = "main"
    test_dir: str = "tests"


@dataclass
class Config:
    cloud: Optional[CloudConfig] = field(default=None)

    @classmethod
    def load(cls) -> "Config":
        """Load config from ~/.delta/config.toml. Returns empty config if not found."""
        if not CONFIG_FILE.exists():
            return cls()

        # Manual TOML parser (minimal, avoids requiring tomllib/tomli on older Python)
        if tomllib:
            with open(CONFIG_FILE, "rb") as f:
                data = tomllib.load(f)
        else:
            data = _parse_simple_toml(CONFIG_FILE)

        cloud_data = data.get("cloud", {})
        if cloud_data.get("api_key"):
            cloud = CloudConfig(
                api_key=cloud_data["api_key"],
                api_url=cloud_data.get("api_url", "https://api.deltatest.dev"),
                repo_id=cloud_data.get("repo_id"),
                branch=cloud_data.get("branch", "main"),
                test_dir=cloud_data.get("test_dir", "tests"),
            )
        else:
            cloud = None

        return cls(cloud=cloud)

    def save(self):
        """Write config to ~/.delta/config.toml."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.chmod(0o600) if CONFIG_FILE.exists() else None

        lines = []
        if self.cloud:
            lines.append("[cloud]")
            lines.append(f'api_key = "{self.cloud.api_key}"')
            lines.append(f'api_url = "{self.cloud.api_url}"')
            if self.cloud.repo_id:
                lines.append(f'repo_id = "{self.cloud.repo_id}"')
            lines.append(f'branch  = "{self.cloud.branch}"')
            lines.append(f'test_dir = "{self.cloud.test_dir}"')
            lines.append("")

        CONFIG_FILE.write_text("\n".join(lines))
        CONFIG_FILE.chmod(0o600)   # owner read/write only — key is sensitive

    @property
    def is_cloud_enabled(self) -> bool:
        return self.cloud is not None and bool(self.cloud.api_key)


def _parse_simple_toml(path: Path) -> dict:
    """Minimal TOML parser for [section] + key = "value" files (no tomllib fallback)."""
    result: dict = {}
    current_section: dict = result

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            current_section = {}
            result[section] = current_section
        elif "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            current_section[key] = value

    return result
