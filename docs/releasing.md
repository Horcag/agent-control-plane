# Release checklist

This checklist is for maintainers. It does not authorize a worker or routine upgrade
to tag, push, publish, or create a GitHub Release.

- [ ] Run the full test suite and affected-test selector where applicable.
- [ ] Run Ruff check and format, both mypy platform checks, and Bandit.
- [ ] Confirm pyproject.toml version, Python classifiers, and CHANGELOG.md agree.
- [ ] Build a clean source distribution and wheel; inspect contents and metadata.
- [ ] Install the artifact in a fresh environment and run agent-control smoke.
- [ ] Exercise backup, migration, reconciliation, and verification-artifact smoke paths
      against disposable state.
- [ ] Have the root reviewer inspect the complete diff, links, artifacts, and impact.
- [ ] As explicit future maintainer actions only: create and verify the Git tag, push
      the tag, create the GitHub Release, and publish packages to the package index.

Publication is complete only after the maintainer verifies remote tag, GitHub Release,
and package-index metadata. Root review and acceptance remain separate from publication.
