# Public release checklist

The repository must not be made public until every blocking item is complete.

## Completed repository safeguards

- Git history, tracked files, Actions logs, repository secrets, and variables
  were reviewed for committed credentials and private runtime data.
- Runtime data, browser profiles, archives, `.env`, databases, logs, and
  generated login tokens are excluded from Git and Docker build contexts.
- CI actions are pinned to immutable commit SHAs and run with read-only
  repository permissions.
- Production and development Python dependencies are separated.
- Python and frontend dependency audits run in CI.
- Dependabot covers Python, pnpm, Docker, and GitHub Actions dependencies.
- GitHub dependency vulnerability alerts and automated security fixes are
  enabled.
- Security reporting, contribution rules, ownership, and issue templates are
  documented.
- SeeU source code is licensed under the MIT License.
- No MediaCrawler source, derived override, license copy, build instruction, or
  prebuilt provider image is present in the release tree.
- External providers are clearly documented as separately installed and
  separately licensed programs.

## Immediately before changing visibility

1. Re-run the full tests, image builds, dependency audits, and Git-history
   secret scan on the exact commit that will become public.
2. Confirm no test account, monitoring URL, QR code, Cookie, browser profile,
   `.env`, database, log, archive, or generated content is tracked.
3. Confirm no provider source or provider-derived file is present in the full
   reachable Git history; removing a file only from the latest tree is not
   sufficient.
4. Reconfirm the dependency graph and security updates, then enable private
   vulnerability reporting, secret scanning, push protection, and CodeQL
   default setup where GitHub exposes them.
5. Configure a `main` ruleset requiring the CI checks and pull-request review.
6. Require approval before workflows from first-time or outside contributors
   run.
7. Treat public exposure as irreversible: forks and cached copies may remain
   public even if the repository is later made private.
