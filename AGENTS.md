# AGENTS.md

## Project overview
- This repository hosts the MeshCore PathBot web/admin + guest interfaces.
- Keep changes focused and minimal; prefer small, explicit patches over broad refactors.

## Development workflow
- Run targeted tests for changed behavior first, then run full `pytest` when practical.
- Keep UI behavior consistent between admin and guest pages when the feature applies to both.

## Web/UI conventions
- Templates live in `src/meshcore_pathbot/web/templates/`.
- Guest templates live in `src/meshcore_pathbot/web/templates/guest/`.
- Shared path rendering partials are under `src/meshcore_pathbot/web/templates/partials/`; prefer enhancing shared partials instead of duplicating logic.
- Prefer lightweight, framework-free JavaScript in templates/static files.

## Data retention
- Repeater cleanup is daily and uses a 7-day retention window.
- If touching cleanup logic, keep retention-related UI copy and backend behavior aligned.
