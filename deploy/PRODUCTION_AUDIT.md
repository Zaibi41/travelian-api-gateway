# Production deployment audit — 2026-10-23

Recommendation: **NO-GO for production launch until the external prerequisites below are completed.** Repository fixes and local validation are complete. No commit, push, PR, merge, deployment, service restart, infrastructure mutation, supplier booking, DNS change, proxy change, or firewall change was performed. SSH inspection was read-only.

## Audit summary

| Area | Status | Issue | Action |
| --- | --- | --- | --- |
| Application | PASS locally | Root operational endpoints were pending local changes; previous tests covered supplier contracts only | Preserved root health/readiness implementation; verified lifecycle and actual Uvicorn HTTP on Python 3.12 |
| CI/CD | PASS locally | Repeated main CI, feature dispatch testing, artifact rerun collision, dependency drift | PR-only CI; main-only production jobs; tests gate archive; dependency lock bundled; artifact overwrite enabled on reruns |
| Deployment | FIXED | SHA-only releases prevented retry; extraction outside lock; unspecified Python; missing production environment | Unique SHA/run/attempt directories; locked extraction; Python 3.12; readable shared environment required |
| Rollback | PASS in mocks | Incomplete targets accepted; already-active rollback skipped health; failed first-deploy stop hidden | Healthy-release marker, health check for active target, explicit stop failure, preservation and verification of original release |
| Security | CONDITIONAL | No booking-route authentication; GitHub environment unprotected | Keep port local; decide upstream authentication before public routing; strict SSH host verification retained; expanded secret-file ignores |
| Lightsail | PREREQUISITES OPEN | Service disabled; shared environment file absent | Administrator must enable boot startup and provision production configuration before merge/deployment |

## Findings and classification

| Classification | Finding | Disposition |
| --- | --- | --- |
| Must Fix | Redeployment of the same SHA or retry after extraction failure could not proceed | Fixed with `<SHA>-<run-ID>-<attempt>`; each workflow attempt creates a new release without deleting old directories |
| Must Fix | Production resolved a fresh dependency set after tests | Fixed by freezing runtime dependencies before installing pytest and bundling `requirements.lock`; deployment installs these versions using binary wheels |
| Must Fix | `python3` did not enforce production Python 3.12 | Fixed with `python3.12` and a virtualenv version check |
| Must Fix | Re-running successful artifact-upload jobs could collide with the original artifact | Fixed with `overwrite: true`; rerunning only a failed deploy consumes the existing tested artifact and gets a new release directory |
| Must Fix | Extraction did not participate in deployment locking | Fixed with the same shared lock for extraction; preparation/activation reacquires it after extraction |
| Must Fix | A partial Python environment was sufficient for manual rollback | Fixed with `.deployment-success` written only after healthy deployment |
| Must Fix | First-deployment cleanup ignored service stop failure | Fixed; failure is now critical and nonzero |
| Must Fix | `current` could be a real directory; symlink helpers could hide an earlier command failure in conditional calls | Fixed with an explicit directory guard and error propagation |
| Must Fix before deployment | `/opt/travelian-api/shared/.env` is absent | Not provisioned: production mutation requires approval. Deployment now refuses activation if it is missing/unreadable |
| Must Fix before launch | `travelian-api.service` is disabled at boot | Administrator must enable it; existing deploy sudo permissions need not expand |
| Must Fix before public traffic | `/v2` routing is unimplemented for this gateway; booking endpoints have no application authentication | Separate approved routing/access-control integration; existing production application untouched |
| Overengineering | Two workflows repeated tests for every main push | Removed standalone CI's main push trigger. PR testing and testing the actual production artifact remain intentional |
| Optional | GitHub environment has no protection rules or branch policy | Confirmed read-only; add policies/reviewers when supported. Workflow guards both jobs to `main` |
| Optional | Branch-protection API returned HTTP 403 requiring GitHub Pro/public repository | Documented plan limitation; required PR checks could not be verified/enforced through this API |
| Optional | Broad dependency ranges can select newer versions on later runs | Same artifact carries the versions tested in that run; a committed dependency lock can be added later if cross-run reproducibility is desired |
| Optional | TestClient reports an httpx deprecation warning | Tests pass; defer client migration until needed |
| Optional | No automated release retention or structured log pipeline | Retain diagnostic releases and monitor disk; existing journal/service and supplier error logging suffice for this deployment audit |

No remaining known repository-level critical blocker was found. The unresolved server configuration and public access requirements prevent declaring production launch ready.

## Files changed or retained in this working tree

The initial checkout already contained changes to `README.md`, `app/main.py`, `app/api/routes/operations.py`, and untracked deployment files. Those changes were preserved and audited; no user changes were discarded.

| File | Change and reason |
| --- | --- |
| `.github/workflows/ci.yml` | PR-only trigger removes duplicate main checks; adds dependency consistency and Bash syntax checks |
| `.github/workflows/deploy-production.yml` | Main-only test/deploy guards, job timeouts, tested SHA assertion, bundled runtime lock, rerun-safe artifact overwrite, fresh release IDs, SSH preflight/connection bounds, private shared uploads, locked safe extraction; fixes drift, retries and extraction races |
| `deploy/deploy.sh` | Requires shared environment and Python 3.12; installs artifact lock without cache/source builds; preserves active releases; propagates activation errors; reports failed stop; marks healthy releases |
| `deploy/rollback.sh` | Accepts run-qualified IDs, rejects incomplete releases, checks already-active target health, propagates symlink errors; preserves restart/health restoration on failed rollback |
| `.gitignore` | Ignores environment variants and common private-key extensions while retaining `.env.example` |
| `tests/test_operations.py` | Tests actual root/versioned routes, successful configuration loading, shutdown readiness and startup failure |
| `tests/test_deployment.py` | Exercises the actual Bash scripts in temporary trees with mocked Python/service/curl/sleep; also tests the workflow's archive validator and SHA-256 corruption detection |
| `README.md` | Documents operational endpoints, actual release/rollback behavior, prerequisites, verified server findings, public-routing boundary and runbook |
| `app/main.py` | Earlier pending change retained: root operational router and readiness lifecycle around configuration loading |
| `app/api/routes/operations.py` | Earlier pending change retained: `/ready` returns 200 only after startup, otherwise 503; health response remains exactly `{"status":"ok"}` |
| `deploy/PRODUCTION_AUDIT.md` | Records findings, evidence, remaining prerequisites and approval boundary |

`requirements.txt`, supplier contracts and infrastructure were not changed. No new infrastructure or orchestration was introduced.

## Actual validation

- Python **3.12.15** isolated environment: **19 tests passed**, one non-failing TestClient deprecation warning. Two existing supplier contract tests, two application lifecycle tests, and fifteen deployment/archive/checksum tests.
- Python 3.14.4: intermediate suite of 12 tests passed before additional cases were added; final production compatibility validation used 3.12.
- Actual Uvicorn startup on localhost using Python 3.12, one worker, demo disabled and SSM disabled for the local smoke test: **PASS**. Real HTTP `/health` returned 200 and `{"status":"ok"}`; `/ready` returned 200 and `{"status":"ready"}`. Local process shut down afterward. No supplier APIs were called.
- `pip check`: **PASS**, no broken requirements. Runtime dependencies installed as Python 3.12 binary wheels.
- Python compilation of `app` and `tests`: **PASS**.
- `bash -n` for both scripts: **PASS**.
- PyYAML parsing of both workflows: **PASS**.
- Official `actionlint` 1.7.7 on both workflows: **PASS**. ShellCheck integration disabled because ShellCheck is not installed; Bash syntax and actual script tests were executed separately.
- Deployment tests: successful first deployment, active release overwrite rejection, unhealthy first deployment cleanup, fresh retry of same commit, unhealthy upgrade rollback, dependency failure before activation, manual rollback failure/restoration and success, unhealthy already-active rollback, incomplete rollback target rejection, deployment/rollback lock contention, service stop failure and missing shared environment rejection: **PASS in mocks**.
- Archive validator: ordinary file accepted; traversal, absolute path, symlink and special device entries rejected. SHA-256 accepts original bytes and rejects corruption: **PASS**.
- `git diff --check`: **PASS**. Final tracked diff and untracked workflow/scripts/tests reviewed. Current tracked environment/key filenames include only `.env.example`; this is not a full-history secret scan.
- GitHub read-only inspection: production environment exists; secret `LIGHTSAIL_SSH_PRIVATE_KEY` and variables `LIGHTSAIL_HOST`, `LIGHTSAIL_SSH_KNOWN_HOSTS` exist. Secret contents were not fetched. No environment protection rules or deployment branch policy. Branch-protection API returned a plan-related HTTP 403.
- Not executed: actual GitHub Actions run, end-to-end production artifact upload/install, real systemd restart/rollback, reboot, production SSM/vendor authentication, supplier allowlist verification, public `/v2` route. Mocks do not prove systemd/pip/SSH behavior on production.

## Read-only Lightsail evidence

- `travelian-api.service`: **inactive, disabled**, expected on an undeployed server except boot enablement must be completed.
- Unit uses `User=deploy`, `WorkingDirectory=/opt/travelian-api/current`, optional shared `.env`, current virtualenv Python, `app.main:app`, `127.0.0.1:8000`, one worker, restart-on-failure, 30-second stop timeout, `NoNewPrivileges`, `PrivateTmp`.
- Deployment user can passwordlessly start/restart/stop **only** this service; no broader sudo requested.
- Application/releases/shared directories owned by `deploy`, mode `750`; releases and shared confirmed writable.
- Shared `.env` **missing**. No active release was reported.
- Python **3.12.3**; venv module and curl/flock/sha256sum/tar/gzip present. Actual virtualenv package bootstrap on the server was not run.
- Root disk: 38 GB, 3.2 GB used, 35 GB available. Memory: 909 MB total, 555 MB available; no swap. One Uvicorn worker and no source builds are appropriate; runtime load was not tested.
- Listening TCP ports observed: SSH on 22 and local DNS on 53. Port 8000 currently unbound. Unit binds it to localhost when started; firewall state was not independently audited.
- Actual outbound IP from the server: **52.76.26.224**. No address/server/networking changes made.

## Remaining deployment plan

Before merging, obtain approval for the first deployment, have an administrator enable this service at boot, and provision the shared environment securely (mode 600, deploy-readable). Set `ENABLE_DEMO_VENDOR=false`; supply confirmed supplier configuration or functioning SSM access in `ap-southeast-1`. Configuration and successful supplier authentication are separate from the health probe. Do not print secrets. A missing/incompatible shared configuration affects all retained releases because it is not rolled back with code.

Then:

1. Commit all reviewed files on `feature/production-deployment`, including the currently untracked workflow, scripts and tests. Example: `git add .github/workflows/ci.yml .github/workflows/deploy-production.yml .gitignore README.md app/main.py app/api/routes/operations.py deploy tests/test_operations.py tests/test_deployment.py`, then `git commit -m "Harden production deployment and rollback"`.
2. After approval, `git push -u origin feature/production-deployment`.
3. Create a pull request into `main`; `gh pr create --base main --head feature/production-deployment`.
4. Verify PR CI is green and review the diff. Confirm production variables point at the existing server and the known-host entry matches it.
5. After explicit deployment approval and prerequisites, merge into `main`. **Merging triggers deployment immediately.**
6. Observe Deploy Production: successful tests, artifact/checksum verification, SSH upload, install, restart, and healthy completion. A failed deployment must stay red even if rollback succeeds.
7. Over SSH verify `systemctl is-active travelian-api.service`, `systemctl is-enabled travelian-api.service`, localhost `/health` and `/ready`, active symlink, and service journal. Public `/v2` verification waits for its separately approved integration.
8. Confirm the active release's `.deployment-success` and retain its full release ID. First deployment has no previous application to restore; failure stops/removes it. After a subsequent healthy release, manual rollback uses `bash /opt/travelian-api/current/deploy/rollback.sh <previous-release-ID>`. A real rollback rehearsal needs separate approval.

No deployment success is claimed.

## Reference checks

Workflow guards and concurrency were checked against [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax). Concurrency prevents simultaneous workflow executions but does not provide a FIFO queue of every pending push. Archive validation rejects links and special files in addition to traversal, consistent with the risks described in [Python's tarfile security guidance](https://docs.python.org/3.12/library/tarfile.html#extraction-filters).
