# DevOps Tooling Setup

One-time bootstrap for the three safety nets added in this branch: **pre-commit hooks**, **PR pipeline checks**, **Renovate**. After this is done once, everything runs automatically.

## 1. Pre-commit (local, every developer)

```bash
pipx install pre-commit       # or: brew install pre-commit
cd <repo-root>
pre-commit install            # writes .git/hooks/pre-commit
pre-commit run --all-files    # one-time sweep on existing code
```

Hooks: trailing-whitespace, end-of-file-fixer, YAML/TOML/JSON validity, large-file guard, merge-conflict detector, private-key detector, **gitleaks** (with Razorpay-specific rules in `.gitleaks.toml`), **ruff** lint + format, **markdownlint** for `designs/` and `docs/`.

To skip a hook in a true emergency: `git commit --no-verify`. Do not make a habit of this.

## 2. PR Pipeline (`.github/workflows/pr-checks.yml`)

Five jobs run on every PR:

| Job | Purpose | Fails PR? |
|---|---|---|
| `pre-commit` | Mirrors local pre-commit | Yes |
| `python-tests` | ruff + mypy + alembic upgrade + pytest | Yes |
| `cdk-synth-nag` | `cdk synth` with `AwsSolutionsChecks` aspect | Yes (on cdk-nag errors) |
| `checkov` | Scan synthesised CloudFormation | Yes (on hard fails) |
| `infracost` | Posts a cost diff comment on the PR | No (advisory) |

### Required GitHub repo secret

| Secret | Where to get it |
|---|---|
| `INFRACOST_API_KEY` | Free at https://www.infracost.io/. `infracost auth login` prints it. Add at **Settings -> Secrets and variables -> Actions -> New repository secret**. |

No AWS keys needed. Real deploys use OIDC (Pillar 8 of `designs/MIGRATION_ARCHITECTURE.md`), which is a separate workflow not yet wired.

### Running checks locally

```bash
# Full pre-commit sweep
pre-commit run --all-files

# Python lint + tests
cd razorpay_checkout && uv run ruff check . && uv run mypy && uv run pytest

# CDK synth with nag enabled
cd infra && uv sync --extra dev && uv run cdk synth

# Skip cdk-nag temporarily (CI always runs it)
RAZORPAY_NAG_DISABLED=1 uv run cdk synth
```

### Acting on cdk-nag findings

Every finding is one of:

1. **Real issue.** Fix the construct (add encryption, scope IAM, etc.).
2. **Knowingly accepted.** Add an entry to `infra/stacks/nag_suppressions.py` with a reason that survives code review. Entries starting with `TODO` are provisional and must be revisited.

Never add a blanket suppression at app level. Suppressions are per-resource-path with justification.

### Acting on Checkov findings

Checkov runs against the synthesised CloudFormation, so it sees what AWS sees. Two skip patterns are documented in the workflow (`CKV_AWS_18`, `CKV_AWS_50`) with reasons inline. Add new skips only with a comment.

### Acting on Infracost diff

The bot updates a single comment on the PR. A red diff (cost rises) is a discussion, not a block. If the rise is justified (e.g., adding a VPC endpoint per Pillar 2), say so in the PR description.

## 3. Renovate

### Install (one-time)

Install the **Renovate** GitHub App from https://github.com/apps/renovate, scoped to this repo.

On first install, Renovate opens an **Onboarding PR** with a sample config. Close it. The committed `renovate.json` at repo root is the real config.

### What Renovate does

| Behaviour | Setting |
|---|---|
| Schedule | Weekly run on Monday morning IST; vulnerability alerts run immediately |
| Auto-merge | Patch, pin, digest updates only; major versions wait for human review |
| Grouping | OpenTelemetry, AWS CDK, boto3 family, pytest, FastAPI/Pydantic each ship as one PR |
| Pinning | GitHub Actions pinned to commit SHA (supply-chain safety) |
| Hold | `razorpay` SDK and `asyncpg` always require manual review |
| Dependency Dashboard | A single GitHub Issue lists pending updates |
| Lock-file maintenance | Weekly `uv.lock` refresh PR |

### Tuning Renovate

If PR volume becomes noisy:

- Increase `prConcurrentLimit` to slow Renovate down further (currently 5).
- Move groups into `schedule:monthly` if weekly is excessive.
- Add packages to the "manual review" rules if auto-merge surfaces incompatibilities.

## Layering With the Migration Plan

These three tools satisfy gaps called out in `designs/MIGRATION_ARCHITECTURE.md` Pillar 8.5 (artifact strategy) and Pillar 7 (compliance/audit). They do not replace the OIDC deploy workflow or the canary alias rollout, which are separate workflows to be added at Phase 6 of the migration plan.

## Verification Checklist

After bootstrap:

- [ ] `pre-commit run --all-files` passes on a clean checkout.
- [ ] A trivial PR (e.g., README typo) shows all five PR-checks jobs in GitHub Actions.
- [ ] Infracost posts a comment on a PR that touches `infra/`.
- [ ] Renovate Dependency Dashboard issue exists.
- [ ] A test commit containing a fake Razorpay key (`rzp_live_AAAAAAAAAAAAAA`) is blocked by gitleaks locally.
