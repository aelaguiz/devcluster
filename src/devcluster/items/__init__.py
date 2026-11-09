"""One handler for each kind of item in the config."""

from .checkouts import sync_repo, sync_tool
from .others import sync_cli, sync_file, sync_login, sync_secrets
from .nightly import sync_nightly
from .settings import sync_settings

HANDLERS = {
    "secrets": sync_secrets,
    "repo": sync_repo,
    "tool": sync_tool,
    "cli": sync_cli,
    "login": sync_login,
    "file": sync_file,
    "settings": sync_settings,
    "nightly": sync_nightly,
}
