# Project Codex Skills

This directory vendors Codex skills that are useful for this repository.

Codex may not automatically load project-local skills in every environment. If
the `codebase-knowledgebase` skill is not available in a coworker's session,
install it into their Codex skills directory:

```bash
mkdir -p "$HOME/.codex/skills"
cp -a .codex/skills/codebase-knowledgebase "$HOME/.codex/skills/"
```

After installation, ask Codex to use `$codebase-knowledgebase` when reading or
maintaining `docs/kb`.
