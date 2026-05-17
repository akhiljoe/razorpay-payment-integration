# Design folder index

Authoritative design documents and the AWS migration plan. Everything here is source-of-truth. Operational docs live in [`../docs/`](../docs/).

## What's in this folder

| File | Purpose | Audience |
|---|---|---|
| [DESIGN_v6.md](DESIGN_v6.md) | **Current formal design.** State machine, webhook routing, idempotency contract, fulfillment hook contract, 13 critical implementation rules. | Engineers working on the codebase |
| [DESIGN_v5.md](DESIGN_v5.md) | Historical previous design. Kept for archaeology. **Do not consult for current behavior.** | Anyone digging through git history |
| [MIGRATION_WRITEUP.md](MIGRATION_WRITEUP.md) | **The why behind the AWS migration plan.** Long-form, conversational, with diagrams. Single coherent plan with upgrade triggers (no multi-stage versioning). | Anyone planning the cloud move |
| [MIGRATION_ARCHITECTURE.md](MIGRATION_ARCHITECTURE.md) | **The what of the AWS migration plan.** Prescriptive, with exact configurations, IAM policies, phased matrix, feature toggles. | Engineers executing the migration |
| [iam-policies/](iam-policies/) | Six IAM execution role policies, one per Lambda. Each has `Allow` + explicit `Deny` co-located. | Anyone reviewing the security model |

## Reading order

| Goal | Order |
|---|---|
| Understand how the application works | DESIGN_v6.md |
| Understand the AWS deployment strategy | MIGRATION_WRITEUP.md (narrative) then MIGRATION_ARCHITECTURE.md (blueprint) |
| Review the security boundaries | MIGRATION_ARCHITECTURE.md Pillar 5 + iam-policies/ |
| Estimate the AWS bill | [../docs/cost-analysis.md](../docs/cost-analysis.md) |
| Find the upgrade trigger to enable WAF (or other deferred services) | MIGRATION_WRITEUP.md §14 + MIGRATION_ARCHITECTURE.md Upgrade Path table |

## What changes here

- **DESIGN_v6.md** changes only when the application's behavior changes (state machine, idempotency, schema, etc.). Update DESIGN_v6.md first, then code, then docs/.
- **MIGRATION_ARCHITECTURE.md and MIGRATION_WRITEUP.md** change when the AWS deployment strategy changes. Update writeup first (the why), then architecture (the what), then code.
- **iam-policies/** files use `${Env}`, `${AWS::AccountId}`, `${SecretsCmkId}`, `${SqsCmkId}` placeholders. They are templates; CDK substitutes the values at synth time.

## Living-document hooks

| File | Update protocol |
|---|---|
| MIGRATION_WRITEUP.md | Pillar 7 lists feature toggles + triggers. Tradeoff Cheat Sheet (§15) is the per-decision audit trail. |
| MIGRATION_ARCHITECTURE.md | Assumptions Checklist at the bottom must be resolved before the corresponding pillar is deployed. Each `Deferred Services Enable N` section is the runbook to flip a feature toggle in `infra/cdk.json` and roll it forward. |
