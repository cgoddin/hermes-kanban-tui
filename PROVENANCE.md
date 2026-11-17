# Provenance and licensing status

Derived from the original local `herdr-workstation-tools` repository:

- Original commit: `d743fea72546cc0154325289262f15a38d060aec`
- Original tree: `6a4b9bbf37bbe3c6b83393440f18428e5641f241`
- Exported base paths: `bin/herdr-kanban`, `herdr_workstation/{__init__,kanban,safety}.py`,
  `tests/{__init__,test_kanban,test_pty}.py`, `.gitignore`.

The original read-only reader, rendering/sanitization, navigation and terminal
cleanup remain the foundation. This export generalizes board names and default
selection, honors portable HOME paths and board environment inputs, replaces
the workstation README with viewer-only instructions, and adds portable
selection and fresh-install regression tests. Fixture task/project names are
fictional. The Python package keeps its original internal name to avoid an
unnecessary source migration.

The archive intentionally omits the original Git history, deployment evidence,
private governance/project notes, skills, process/transport/layout tools,
Herdr protocol dump and any real databases or logs. The original repository
and history have not been rewritten. This is a new clean source export, not a
full-history clone, upstream release or published remote repository.

## Licensing

The source repository contained no LICENSE file or explicit redistribution
license. This package preserves that status and does not invent an MIT,
Apache or other public license. Its preparation for private sharing does not
establish a general right to publish, sublicense or redistribute it further.
Ask the source owner before broader redistribution or public hosting. Any
future public license choice is a separate owner decision.
