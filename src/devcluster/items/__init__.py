"""One handler for each kind of item in the config."""

from .checkouts import sync_repo, sync_tool
from .others import sync_cli, sync_file, sync_secrets

HANDLERS = {
    "secrets": sync_secrets,
    "repo": sync_repo,
    "tool": sync_tool,
    "cli": sync_cli,
    "file": sync_file,
}
