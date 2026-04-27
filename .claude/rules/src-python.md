---
paths:
  - "shared/**/*.py"
  - "claude-study/src/**/*.py"
  - "FreelanceScout/freelance_scout/src/**/*.py"
  - "FreelanceScout/freelance_scout/scripts/**/*.py"
  - "Investment/investment_support/src/**/*.py"
  - "Investment/investment_support/scripts/**/*.py"
  - "PaperWatch/paper_watch/src/**/*.py"
  - "PaperWatch/paper_watch/scripts/**/*.py"
  - "EnglishDaily/english_daily/src/**/*.py"
  - "EnglishDaily/english_daily/scripts/**/*.py"
---

# Python Source Rules

- Follow the conventions of the project you are editing instead of copying assumptions across subprojects.
- Prefer clear names, small functions, and straightforward control flow over clever shortcuts.
- Reuse shared helpers when they already fit the job instead of duplicating logic.
- Preserve the import and logging patterns already used in the target project.
- In the app-style projects, `from src.config import CONFIG` and `get_logger(__name__)` are the standard patterns unless that project already does something different.
- Keep scripts runnable from the project directory described in that project's `CLAUDE.md` or `SETUP.md`.
- Avoid unrelated refactors while making a focused change.
