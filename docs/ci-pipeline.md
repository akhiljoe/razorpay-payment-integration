# CI Pipeline: GitHub Actions Setup

A complete walkthrough of the GitHub Actions configuration in `.github/workflows/pr-checks.yml`, what each job does, why it exists, and how to read its output.

## What is GitHub Actions

**GitHub Actions** is GitHub's built-in CI/CD platform. Every time you push code or open a pull request, GitHub spins up a fresh virtual machine, clones the repo, and runs whatever steps you've defined in a YAML file under `.github/workflows/`. You pay for compute time in private repos (2000 free minutes/month on the free plan), nothing for public repos.

Key building blocks:

- **Workflow**: a YAML file under `.github/workflows/`. Triggered by events (push, pull request, schedule, manual).
- **Job**: a unit of work that runs on a fresh VM. Jobs in the same workflow run in parallel by default; declare `needs:` to make one wait for another.
- **Step**: a single command or pre-packaged "action" inside a job.
- **Action**: a reusable step published by GitHub, vendors, or the community (e.g. `actions/checkout@v4`).
- **`GITHUB_TOKEN`**: a temporary credential GitHub mints at the start of every workflow run and destroys at the end. The workflow uses it to read code, post comments, upload artifacts, etc. The token's scope is controlled by the `permissions:` block.

## Our Workflow at a Glance

File: `.github/workflows/pr-checks.yml`

Triggers on every **push to `main`** and every **pull request** targeting `main`. Five jobs run, four in parallel and one that waits on the cdk synth output.

| Job | Runs in parallel with | Purpose |
|---|---|---|
| **pre-commit** | python-tests, cdk-synth-nag | Fast formatting + secret scan |
| **python-tests** | pre-commit, cdk-synth-nag | Unit tests + type check |
| **cdk-synth-nag** | pre-commit, python-tests | Compile CDK, run cdk-nag security aspect, publish CloudFormation as artifact |
| **checkov** | (waits on cdk-synth-nag) | Second pass over the synthesised CloudFormation |
| **infracost** | (waits on cdk-synth-nag) | Comment on the PR with cost diff vs base branch |

Total wall-clock time: ~3 to 5 minutes.

## Job-by-Job Breakdown

### 1. `pre-commit`

#### What it does

Runs the same hooks listed in `.pre-commit-config.yaml` against every file in the repo. In CI this means re-running all of them on every push, even if no commit triggered the local hook.

**Hooks invoked**:

| Hook | What it catches |
|---|---|
| `trailing-whitespace` | Stray spaces at end of lines |
| `end-of-file-fixer` | Files not ending in a newline |
| `check-yaml` / `check-toml` / `check-json` | Malformed config files |
| `check-added-large-files` (500kb cap) | Accidental binary commits |
| `check-merge-conflict` | Unresolved `<<<<<<<` markers |
| `detect-private-key` | Pasted SSH or PEM keys |
| `mixed-line-ending --fix=lf` | CRLF leaking in from Windows |
| `gitleaks` | Razorpay keys, AWS keys, generic secrets in commit history |
| `ruff lint` + `ruff format` | Python linting and formatting (replaces flake8 + black) |
| `markdownlint` | Markdown structural issues in `designs/` and `docs/` |

#### Why it matters

It's the cheapest line of defence. Catches secrets before they touch GitHub (gitleaks scans full history with `fetch-depth: 0`), catches formatting drift, catches broken YAML before it reaches anything that parses YAML.

#### How to satisfy locally

```bash
pipx install pre-commit
pre-commit install     # one-time: installs git hook
pre-commit run --all-files   # mimics CI exactly
```

### 2. `python-tests`

#### What it does

Spins up a PostgreSQL 16 container as a service, installs the `razorpay_checkout` Python package with dev extras (via `uv`), then:

1. **Ruff lint** on the package source.
2. **mypy** in strict mode (all functions typed).
3. **alembic upgrade head** to apply migrations against the live Postgres.
4. **pytest** on the unit suite, excluding tests marked `requires_localstack` (those need SQS, run only in nightly).

#### Why a real Postgres container

The codebase uses asyncpg-specific behaviour (advisory locks, JSONB ops, `SELECT ... FOR UPDATE`). SQLite stubs would miss state machine + idempotency bugs that only show up under real row locking.

#### How to satisfy locally

```bash
cd razorpay_checkout
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=test -e POSTGRES_DB=razorpay_test postgres:16-alpine
uv sync --extra dev
uv run ruff check . && uv run mypy && uv run alembic upgrade head && uv run pytest -q
```

### 3. `cdk-synth-nag`

#### What it does

Builds the CDK app and runs cdk-nag's `AwsSolutionsChecks` ruleset against every resource. Steps:

1. Install **Node 20** and **AWS CDK CLI v2**.
2. Install **uv** and `uv sync` the `infra/` Python deps.
3. **Ruff lint** the `infra/stacks/` code.
4. **`cdk synth --quiet`** — compiles `infra/app.py` into CloudFormation JSON under `infra/cdk.out/`. The cdk-nag aspect attached in `app.py` runs as part of synth and errors out if any rule fires without a registered suppression.
5. **Upload `infra/cdk.out`** as a build artifact named `cdk-out` so the downstream jobs can consume it.

#### Why cdk-nag

cdk-nag is a community-maintained library that walks the CDK construct tree at synth time and flags 100+ AWS best-practice violations (unencrypted SQS, public Lambdas, IAM wildcards, missing log retention, etc.). Running it at synth means you catch problems before any CloudFormation deploy. Suppressions live in `infra/stacks/nag_suppressions.py` with written justifications, so when a future reviewer asks "why is this allowed?" the answer is in code, not in a Confluence page.

#### Why publish an artifact

`checkov` and `infracost` both need the synthesised CloudFormation. Re-running synth in each job would triple the wall-clock time. Artifacts solve this: synth once, download in the downstream jobs.

#### How to satisfy locally

```bash
cd infra
uv sync --extra dev
uv run ruff check .
uv run cdk synth --quiet
```

### 4. `checkov`

#### What it does

Bridgecrew's Checkov scanner reads the CloudFormation JSON from the `cdk-out` artifact and runs a separate ruleset focused on cloud security posture. Where cdk-nag is opinionated about AWS-specific best practices, Checkov is broader (cross-cloud, runs the same scans on Terraform, K8s manifests, etc.) and often flags things cdk-nag doesn't.

#### Why two scanners

They overlap by maybe 40 percent. The non-overlapping rules are the value:

- **cdk-nag** catches AWS-specific config gaps (KMS key rotation off, SQS without DLQ, X-Ray tracing missing).
- **Checkov** catches policy patterns (wildcard principals, public S3, IMDSv1 on EC2) and has its own scoring + SARIF output that integrates with GitHub's Security tab when enabled.

#### Skip list

The workflow's `skip_check:` parameter excludes 12 rules. Each one corresponds to an architectural decision documented in `designs/MIGRATION_ARCHITECTURE.md`:

| Rule | Why skipped |
|---|---|
| `CKV_AWS_18` | S3 access logging — CloudTrail S3 events cover this |
| `CKV_AWS_50` | Lambda X-Ray active — set conditionally; cdk-nag covers it |
| `CKV_AWS_27` | SQS KMS-CMK — using SSE-SQS (AWS-managed) on free track |
| `CKV_AWS_26` | SNS KMS-CMK — ops topic is metric notifications, non-sensitive |
| `CKV_AWS_158` | CloudWatch Logs KMS-CMK — 14-day retention, no PII |
| `CKV_AWS_136` | ECR KMS-CMK — AES256 default on free track |
| `CKV_AWS_51` | ECR immutable tags — `:latest` is intentionally MUTABLE for CI roll |
| `CKV_AWS_117` | Lambda in VPC — free track is non-VPC by design |
| `CKV_AWS_116` | Lambda DeadLetterConfig — SQS-native DLQs via `maxReceiveCount=5` |
| `CKV_AWS_115` | Lambda reserved concurrency — default works at <1000 webhooks/mo |
| `CKV_AWS_173` | Lambda env var KMS — env vars hold no secrets (Secrets Manager does) |
| `CKV_AWS_258` | Lambda Function URL AuthType NONE — Razorpay HMAC signature is the auth |

The full justifications live as inline comments next to the `skip_check` line so any future reviewer sees them immediately.

#### SARIF upload

The last step tries to upload Checkov's findings as a SARIF file to GitHub's Code Scanning service, which would populate the Security tab. **Private repos need GitHub Advanced Security (paid) for Code Scanning**, so this step has `continue-on-error: true`. It logs a warning, doesn't fail the job. Checkov's findings still print to the job log.

### 5. `infracost`

#### What it does

Estimates the AWS monthly cost from the CloudFormation in the `cdk-out` artifact, diffs it against the cost on the base branch (target of the PR), and posts a comment on the PR with the breakdown.

#### Why

A `cdk diff` shows what infra is changing. Infracost shows what your AWS bill will change. Catches "I added a NAT gateway and didn't realise it costs $32/mo" before merge.

#### Setup required

This job only runs on **pull requests** (not on push to main). It needs a free API key from infracost.io, stored as a repo secret named `INFRACOST_API_KEY`. Without that secret the job will fail open — currently it's set up but won't actually run until you open a PR.

## The `permissions:` block

```yaml
permissions:
  contents: read
  pull-requests: write
  security-events: write
  actions: read
```

This controls the scope of `GITHUB_TOKEN` for every job in this workflow. Key principle: **GitHub Actions defaults are restrictive after you write a `permissions:` block** — any scope you don't list is set to `none`. So you have to enumerate everything.

| Scope | Why we need it |
|---|---|
| `contents: read` | Clone the repo via `actions/checkout`. Required by every job. |
| `pull-requests: write` | The infracost job posts a comment on the PR via the GitHub API. |
| `security-events: write` | The Checkov SARIF upload writes to the Code Scanning API. |
| `actions: read` | The SARIF uploader reads workflow run metadata to attach context to findings. |

If a job fails with `Resource not accessible by integration`, that means it tried to call an API endpoint outside the granted scopes. The fix is always to add the missing scope here, not to mint a new token.

## Workflow Triggers and Concurrency

```yaml
on:
  pull_request:
    branches: [main]
  push:
    branches: [main]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

- Runs on **every PR targeting main** and **every push to main**.
- The `concurrency` block means if you push three commits to a branch within a minute, GitHub cancels the first two runs and only keeps the latest. Saves CI minutes and avoids stale results blocking merges.

## Order of Events on a Typical Push

1. You run `git push origin main`.
2. GitHub triggers the workflow.
3. **In parallel**: `pre-commit`, `python-tests`, `cdk-synth-nag` all start on fresh Ubuntu 24 VMs.
4. As soon as `cdk-synth-nag` uploads its artifact, `checkov` and `infracost` (PR only) start.
5. All 5 jobs report status back to the commit. GitHub merges branch-protection rules require all configured ones to be green before merge.

## How to Debug a Failing Job

1. **Open the run** in the Actions tab: `https://github.com/akhiljoe/razorpay-payment-integration/actions`
2. Click the failing job in the left sidebar.
3. Expand the failing step. Errors are wrapped in `##[error]` markers.
4. Reproduce locally with the commands in the relevant "How to satisfy locally" section above.
5. Push the fix. The concurrency block cancels any in-flight runs on the same ref.

For a fast loop, use the **GitHub CLI**:

```bash
gh run list --workflow=pr-checks.yml --limit=5
gh run view <run-id> --log-failed
gh run watch   # live-tail the current run
```

## What's NOT in This Workflow

Things you might expect that we deliberately don't run here:

- **CDK deploy**: deploys belong in a separate workflow with OIDC auth and environment gates. This file is read-only.
- **End-to-end tests against real AWS**: too slow and too expensive for every PR. Reserved for nightly + manual triggers.
- **Container image builds**: handled in a separate workflow that pushes to ECR, gated on PR merge.
- **Renovate**: dependency updates are handled by Renovate Bot (`renovate.json` at the repo root), which runs on its own schedule and opens PRs that go through this same workflow.

## Quick Reference: Common Operations

| Task | Command |
|---|---|
| Run the same lint hooks locally | `pre-commit run --all-files` |
| Re-run just the failing job | Push an empty commit: `git commit --allow-empty -m "ci: retry" && git push` |
| Skip CI on a docs-only commit | Append `[skip ci]` to the commit message subject |
| Make a workflow change without running other jobs | Most jobs ignore path filters here, but you can add `paths:` to the trigger if needed |
| View the current GITHUB_TOKEN scopes | Top of any job log, look for `##[group]GITHUB_TOKEN Permissions` |

## Where to Look Next

- **`.pre-commit-config.yaml`** — the source of truth for the hooks the `pre-commit` job runs.
- **`infra/stacks/nag_suppressions.py`** — every cdk-nag suppression with its justification.
- **`renovate.json`** — dependency update policy.
- **`docs/devops-tooling-setup.md`** — the original setup notes for these tools.
- **`designs/MIGRATION_ARCHITECTURE.md`** — the architectural decisions the Checkov skip list points back to.
