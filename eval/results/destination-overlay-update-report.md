# Destination overlay and selection scenarios — 2026-10-10

- Main canvas remains full width; assistant and dock stay centered. Compact translucent overlay does not reserve a column. Hide/reopen persists per request; cancel is separate.
- Added typed `reject_candidates` to the shared refine contract. Rejection clears old candidates, increments revision, requests a new name, and preserves later task steps.
- Parser supports ordinal aliases, detects out-of-list selection and duplicate names, distinguishes single-result acceptance from ambiguous agreement. Prompt semantic-unified-13 documents the same transitions for model providers.
- Added a “Tìm địa điểm khác” button using the same typed event.

Validation:
- 82 targeted backend cases passed (new matrix at 24 cases plus trip/refinement/charging 58 cases).
- After extending ordinals and independent cloud/local mocks, all 36 cases in `tests/test_destination_dialogue_scenarios.py` passed.
- 12 frontend navigation cases passed after UI changes.
- Chromium fixture passed at 1440×900, 1024×768, 390×844, 390×568: full-width canvas, stable control positions, select second place, reject list, hide/reopen and map scrolling.
- Ruff, JS syntax and diff whitespace checks passed.

Scope: mocked map/backend in browser; parser and mock cloud/local in backend. No live model/VietMap or full STT/TTS/3D end-to-end validation. Backend was not restarted and production DB was not edited. Browser dependencies and screenshots remain under /tmp/vivi-ui-check.
