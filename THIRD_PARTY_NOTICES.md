# Third-party notices

This project implements its own Python code but adapts behaviour and, in a few
places, algorithm structure from the following upstream projects. No source
file is copied wholesale; each adapted file says so at the top.

## Jev Browser

- Upstream: https://github.com/jkudish/jev-browser
- Comparison commit: `913d1a1cbc42f9534e3fbfefae915d1fc941d22d` (v0.8.12)
- License: MIT, Copyright (c) 2026 Joey Kudish
- Adapted in:
  - `src/jev_browser_py/action_space.py` — noise filters, dedupe-by-href,
    submit/search kind selection, Enter-submit twin, element cap.
  - `src/jev_browser_py/questions.py` — the three-question fan-out shape and
    the second-stage select question.
  - `src/jev_browser_py/judgment.py` — distribution tolerance and the
    "choice is the distribution maximum" rule, cross-checked against the
    public validation code in `jkudish/jev-agent-tools`.

## Jev Agent Tools

- Upstream: https://github.com/jkudish/jev-agent-tools
- License: MIT
- Adapted in: strict answer validation rules (answer id set equality, finite
  probabilities, criterion-keyed distribution, usage and model checks).

## TypeSafe Python SDK

- Upstream: https://github.com/typesafe-ai/typesafe-sdk-python
- Version observed: 0.7.5
- License: MIT
- Used as a runtime dependency; `TypeSafeTransport` calls
  `AsyncTypeSafeClient.system_one` with `Choice` and `Noul` questions.

## Scrapling

- Upstream: https://github.com/D4Vinci/Scrapling
- Comparison commit: `30cadb56a407fd631391441709ab792ebca52b08` (v0.5.1)
- License: BSD-3-Clause
- Used as a runtime dependency; `extraction.py` passes already-fetched HTML to
  `scrapling.parser.Selector` and never triggers a second fetch.

## Playwright for Python

- Upstream: https://github.com/microsoft/playwright-python
- License: Apache-2.0
- Used as a runtime dependency for browser ownership and single-action
  execution.
