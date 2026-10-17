---
applyTo: '**'
---

# AI Dev Directives — navig-mini (Python Agent)

Optimize for **fast fixes, minimal docs, and reliable automation**.

## Scope & Structure
- Use the existing project structure — no new folder hierarchies without need.
- **This is a Python project**: `pyproject.toml`, `pytest`, standard Python tooling — no Node/npm.
- Standard paths: `agent.py` + supporting modules at root · `tests/` for pytest.

## Execution Loop (Auto-Run)
When debugging, upgrading, or implementing a feature, run without stopping for confirmation:

1. **Analyze** — identify root cause; add minimal diagnostics if needed.
2. **Implement** — fix the issue; complete small connected unfinished parts.
3. **Validate** — run affected code paths; confirm expected behavior.
4. **Test** — add/update tests when logic changes or regressions are plausible.
5. **Document** — update existing docs only if change affects usage. No new docs unless necessary.

**Rule:** If something fails, iterate until resolved.

## Python Rules
- f-strings over `.format()` or `%`.
- Type hints on all public functions.
- Specific exceptions (`ValueError`, `ConnectionError`) — never bare `except:`.
- Logging via `loguru` — never `print()` in production.

## Python Environment (Windows)
`C:\Server\bin\python\python-3.12` is a broken embedded Python. Always use:
```powershell
$env:UV_PYTHON_PREFERENCE = "only-managed"
uv run --no-project scripts/myscript.py
```

## Documentation Discipline
- No walls of text. No duplicate docs. No README spam.

## Dependency Policy
- Use latest stable versions unless compatibility constraints are documented.

---

## What NOT To Do
- Don't create new folder hierarchies without need.
- Don't refactor unrelated code for "cleanliness."

---

## Success Criteria
- Issue fixed and verified.
- Changes are minimal, targeted, and stable.

---

## Plan Sync & Living Documents (Mandatory)

At the start of every session AND after any material change:

1. **Read active phase** — open `CURRENT_PHASE.md` (project root) or `.navig/plans/CURRENT_PHASE.md`; note current phase, blockers, and baseline.
2. **Review the roadmap** — read `DEV_PLAN.md` or `.navig/plans/ROADMAP.md`; mark milestones complete when their work lands; add newly discovered milestones.
3. **Scan open checklists** — find all `- [ ]` items in plan files; mark completed tasks done; add newly discovered tasks.
4. **Update plan docs with new data** — completed features, architecture changes → `CURRENT_PHASE.md` or `DEV_PLAN.md`.
5. **Evolve freely** — you are **expected and authorized** to modify `ROADMAP.md`, `CURRENT_PHASE.md`, `DEV_PLAN.md`, and any plan file to reflect project reality. Documentation lag is technical debt.

**Rule:** Every session ends with plan docs matching actual project state. No silent drift.
