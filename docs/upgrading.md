# Upgrading 0.1.x

Use the same install mode that created the current command. Work from a clean tree
with the coordinator stopped.

1. Back up the configured SQLite database and runs/. Copy
   config/workspaces.toml separately; it is intentionally ignored.
2. Update the checkout, inspect CHANGELOG.md, then run:
   agent-control smoke --config .\config\workspaces.toml
3. Reconcile only after smoke succeeds. Review agent-control inbox list and
   agent-control plan summary for unfinished work.

## pipx

    pipx upgrade agent-control-plane
    agent-control smoke --config .\config\workspaces.toml

## uv tool

    uv tool upgrade agent-control-plane
    agent-control smoke --config .\config\workspaces.toml

## Editable checkout

    git pull --ff-only
    python -m pip install -e ".[mcp]"
    agent-control smoke --config .\config\workspaces.toml

After smoke, run a non-destructive local status/job smoke test and verify the database
schema and recent artifacts are readable. Reconcile interrupted jobs explicitly; do
not assume a downgrade works. On migration or checksum failure, restore the backup and
stop for maintainer review.
