# Contributing

Thank you for helping improve SeeU.

## Project boundaries

Contributions must keep the project limited to a single administrator
monitoring public creator pages for personal, non-commercial learning and
research. Do not add:

- password, Cookie, or browser-storage import/export;
- automated CAPTCHA, slider, SMS, or other verification bypasses;
- proxy pools, account farms, DRM bypasses, or access-control bypasses;
- collection of private content or large-scale crawling behavior.

The MediaCrawler provider and the compatibility backport under
`crawler/upstream_overrides` remain governed by MediaCrawler's
NON-COMMERCIAL LEARNING LICENSE 1.1.

## Development

Create a focused branch and keep unrelated changes out of the pull request.
Use synthetic or redacted fixtures only. Never commit `.env`, `data/`, database
files, browser profiles, Cookies, login tokens, QR codes, SMS codes, signed
media URLs, or downloaded content.

Run the relevant checks before opening a pull request:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest backend/tests
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend test
pnpm --dir frontend build
docker compose config --quiet
```

Parsing, URL normalization, provider-contract, and adapter changes require
fixture-based tests. Preserve typed API models, atomic archive promotion,
keyboard access, visible focus, responsive layouts, and explicit
loading/empty/error states.

## Pull requests

Explain the problem, the chosen approach, safety implications, and test
results. Small, reviewable pull requests are preferred. By contributing, you
confirm that you have the right to submit the code and that it can be
distributed under the license applicable to the files you changed.
