---
paths:
  - "claude-study/tests/**/*.py"
  - "FreelanceScout/freelance_scout/tests/**/*.py"
  - "Investment/investment_support/tests/**/*.py"
  - "PaperWatch/paper_watch/tests/**/*.py"
  - "EnglishDaily/english_daily/tests/**/*.py"
---

# Python Test Rules

- Match the framework already used by the project you are editing.
- `claude-study` uses `pytest`.
- `Investment/investment_support` currently uses `unittest`.
- `FreelanceScout/freelance_scout` currently uses `unittest`.
- Add or update tests when behavior changes, and keep each test focused on one behavior.
- Prefer small, local fixtures and assertions that check observable behavior.
- When documenting or running tests, use the correct project directory and command for that project.
