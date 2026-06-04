---
name: codebase-knowledgebase
description: "Create, grow, and maintain a hierarchical Markdown knowledge base for large codebases (including multi-repo systems). Use when Codex should persist code understanding as structured docs: initializing a docs tree (for example `docs/kb`), adding/updating leaf topic notes with concrete code pointers (`file:line`), keeping directory README indexes accurate, cross-linking related topics, and capturing current code facts, ongoing implementation progress for prototypes or in-flight changes, and forward-looking research items such as proposed designs, TODOs, experiments, and future directions."
---

# Codebase Knowledge Base

Maintain a living, hierarchical KB for the codebase.

Key idea: the directory tree is the index; leaf Markdown files hold the detailed knowledge.

## Conventions

- Prefer `docs/kb/` as the KB root unless the repo already uses another convention.
- Require a `README.md` in every KB directory. Each README should explain the directory purpose, list immediate subdirectories with one-line summaries, list leaf docs with one-line summaries, and add related links.
- Keep detailed topic knowledge in leaf docs. Non-leaf directories should usually contain only `README.md`.
- Keep grounded facts, ongoing implementation progress, and forward-looking material separate. Prefer dedicated top-level trees such as `docs/kb/implementations/` for current prototype/in-flight behavior and `docs/kb/future-directions/` for substantial proposals, TODOs, experiments, open questions, or design spaces. Use short cross-links across these trees instead of burying all three concerns under the same component subtree.
- Treat implementation docs as first-class knowledge, not changelogs. They should describe the behavior, invariants, assumptions, caveats, feature gates, and known scaffolding of the code we have added or changed so far.
- Treat pitfalls, caveats, hidden invariants, and rejected shortcuts as first-class KB content. If implementation uncovered "the obvious thing was wrong", preserve that explicitly in the canonical leaf doc instead of letting it live only in chat history.
- Record both sides of a correction when it matters: what earlier assumption/design looked reasonable, and what concrete code path, invariant, or behavior showed it was wrong or incomplete.
- Distinguish carefully between current facts, future proposals, and scoped limitations. A useful implementation note should say not only what works, but also what is intentionally still missing, what is temporarily scoped out, and what would break if a hidden invariant were ignored.
- Anchor proposals to the current code. Every design idea should cite the current functions, data structures, call paths, or constraints that motivated it.
- Anchor implementation docs to both the current code and the motivating design note. Every implementation doc should link back to the future-direction note it is advancing and should call out any decisions or improvisations that diverged from the earlier design.
- Avoid duplicating full explanations. Pick one canonical leaf doc and use cross-links elsewhere.

## Workflow

1. Decide placement in the KB tree.
- Reuse an existing directory when it fits.
- If the topic is grounded/current behavior, place it under the factual component tree (for example `docs/kb/citus/...` or `docs/kb/postgres/...`).
- If the topic is about code we are actively implementing or have recently added as part of a prototype, place it under the top-level implementation tree (for example `docs/kb/implementations/citus/...`) and cross-link both to the factual doc that explains the original code path and to the future-direction doc that motivated the work.
- If the topic is forward-looking research or open design, place it under the top-level future-direction tree (for example `docs/kb/future-directions/...`) and cross-link back to the canonical factual doc that grounds it.
- If the topic cuts across multiple areas, choose one canonical leaf doc location and add cross-links from related directories rather than duplicating content.
- If the directory structure no longer matches the exploration pattern, restructure by adding one more layer rather than doing a deep reorg.

2. Update or create the leaf doc.
- Use `references/topic_template.md` as the default shape.
- Capture behavior, invariants, lifetimes, workflows, edge cases, and concrete code pointers.
- Explicitly capture important pitfalls, caveats, hidden assumptions, ordering constraints, ownership/lifetime rules, and "why a tempting approach was wrong" when implementation or code reading revealed them.
- When a design or prototype changed direction during implementation, record the correction in the canonical doc: what changed, why it changed, and which code paths or constraints forced the change.
- Prefer code pointers in `path/to/file.c:123` form.
- If the doc is an implementation-progress note, capture the exact current behavior of the prototype, feature flags, scaffolding, assumptions, known gaps, shortcuts, and any changes from the motivating future-direction note.
- If the doc covers original upstream/project behavior, also record non-obvious invariants or assumptions that downstream prototype work must preserve, especially when violating them would silently change semantics.
- If the user is doing systems research, also capture candidate designs, TODOs, open questions, and experimental directions, but keep them explicitly separate from established behavior and from implementation-progress facts.
- If future-direction content becomes substantial, create or update a corresponding leaf doc under the top-level future-direction tree instead of bloating the factual note or indexing it under the same factual subtree.
- In factual docs, replace substantial future-design sections with short cross-links to the canonical future-direction doc when needed.
- In implementation docs, replace substantial background/current-code explanations with short cross-links to the canonical factual doc when needed.
- In future-direction docs, add short "implementation progress" cross-links when code lands that validates or changes part of the design.

3. Update README indexes up the tree.
- Ensure the local directory README links to the doc and summarizes it in one line.
- Ensure parent READMEs reflect any new subdirectories.
- Keep factual, implementation, and future-direction indexes separate. A factual README should usually link out to implementation/future-direction content via `Related`, not list them as peer factual documents. The implementation and future-direction trees should each have their own README hierarchy and indexes.

4. Add cross-references instead of duplicating content.
- When shared code belongs to multiple topics, keep one canonical explanation and link to it from the other areas.
- Add back-links in the canonical doc when helpful.
- When a future-direction decision changes during implementation, update both sides: record the implemented decision in the implementation doc and patch the future-direction doc with a short note or revised design section.
- When one leaf doc becomes the canonical place for a pitfall or hidden invariant, prefer linking to that section from neighboring notes instead of re-explaining it everywhere.

5. Keep the index accurate with automation when useful.
- Run `scripts/kb_sync.py` to create missing READMEs, sync subdir/doc lists, and warn about topic docs in non-leaf directories.

## Multi-repo systems

- Create a KB root in each repo when helpful.
- Cross-link between repos using relative paths when possible; otherwise use stable repo-relative paths with a short relationship note.
- Keep a repo-level KB `README.md` that explains how that repo fits into the wider system.
- Apply the same separation between factual trees, implementation trees, and future-direction trees within each repo.

## Restructuring heuristics

Restructure when a directory becomes hard to scan, mixes unrelated themes, repeatedly receives cross-sibling questions, or contains a topic doc that now covers multiple distinct workflows.

Also restructure when forward-looking research starts polluting a factual subtree index. In that case, move the exploratory material into the top-level future-direction tree and leave short stub pointers or related links from the factual docs.

Also restructure when incremental prototype details start polluting either the factual subtree or the future-direction subtree. In that case, move the "what we have actually implemented so far" material into the top-level implementation tree and leave short stub pointers or related links from the factual/future-direction docs.

Prefer splitting by workflow, interface, or component boundary. Preserve discoverability by leaving short stub pointers when you move canonical content.

## High-signal content to preserve

When working in rapidly evolving systems or research prototypes, bias toward preserving these findings explicitly in the KB:

- semantic mismatches between the original codebase and the prototype
- hidden invariants, lifetime rules, ownership boundaries, and ordering constraints
- reasons a plausible design was rejected or narrowed
- places where a prototype intentionally scopes out a rarer path or edge case
- caveats that are easy to miss when reading only the final code
- assumptions imported from upstream code that the new implementation still relies on
- temporary scaffolding that should not be mistaken for the target architecture

## Bundled resources

- `scripts/kb_sync.py`: Synchronize README indexes with the KB filesystem tree.
- `references/readme_template.md`: README template for directory index files.
- `references/topic_template.md`: Default structure for detailed leaf docs, including future-direction sections.
