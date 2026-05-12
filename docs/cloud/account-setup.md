# Runbook: AWS account setup

One-time manual steps before any `cdk deploy` can run. ~45 minutes total.

## Prerequisites you should already have

- A working email you control (use a project alias, not personal).
- Phone for SMS verification.
- A debit/credit card. Free Tier doesn't charge but AWS requires a card on file.
- Government ID for some regions (KYC).

## Step 1: Create the AWS account

1. Go to https://signup.aws.amazon.com.
2. Account name: `razorpay-checkout-prod` (or `razorpay-checkout-dev` for non-prod).
3. Email: a dedicated alias, e.g. `aws-prod-razorpay@yourdomain.com`.
4. Set a strong root password (≥20 chars, store in 1Password / Bitwarden).
5. Choose **Personal** unless you have a registered company.
6. Add card. Verify phone.
7. Pick **Basic Support — Free**.

You now have an AWS account ID. **Record it; never paste it into chat or logs.**

## Step 2: Lock down the root account

The root user has account-recovery privileges. Never use it for daily work.

1. Sign in as root → IAM → Account settings → enable **MFA** (use a hardware key or authenticator app, not SMS).
2. Generate and **delete** any root access keys. AWS warns about this in red.
3. Delete root login profile if your console supports it (some accounts can't).

## Step 3: Create an IAM Identity Center user for daily work

You won't use the root account again except for billing and account closure.

1. Console → **IAM Identity Center** → Enable.
2. Pick a region for Identity Center (separate from your workload region; this is just for SSO).
3. **Users** → Add user `you@yourdomain.com`. Set password manually or send invite.
4. **Permission sets** → Create one called `RazorpayAdmin`:
   - Predefined: `AdministratorAccess`.
   - Session duration: 4 hours.
5. **AWS accounts** → assign yourself to the new account with the `RazorpayAdmin` permission set.
6. Sign out, sign in via the Identity Center start URL (something like `https://d-XXXX.awsapps.com/start`).

## Step 4: Pick the workload region

Default for this project: **`ap-south-1` (Mumbai)**. Reasons:
- Razorpay is India-based; latency between Lambda and Razorpay's API is < 50ms.
- INR-only billing simplification (most regional services price in USD anyway, but networking and S3 transfer are colocated).
- Compliance: Indian payment data should stay in India by default.

If you have a contractual reason to use a different region, pick one and stick with it across all stacks.

## Step 5: Set the budget alarm immediately

Most people forget this until the first surprise bill.

1. Billing console → **Budgets** → Create budget → **Cost budget**.
2. Threshold: $5 monthly. Notification at 80% of budget.
3. Email: your ops alias.
4. The CDK `FoundationStack` also creates budgets, but this manual one is the safety net for the first deploy. Once `FoundationStack` is up, you can delete this one.

## Step 6: Configure CLI

Install AWS CLI v2 if not already:

```bash
# https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
aws --version  # confirm 2.x
```

Set up SSO config in `~/.aws/config`:

```ini
[sso-session razorpay]
sso_start_url = https://d-XXXX.awsapps.com/start
sso_region = ap-south-1
sso_registration_scopes = sso:account:access

[profile razorpay-prod]
sso_session = razorpay
sso_account_id = 123456789012
sso_role_name = RazorpayAdmin
region = ap-south-1
output = json
```

Log in:

```bash
aws sso login --profile razorpay-prod
aws sts get-caller-identity --profile razorpay-prod
```

You should see your role ARN.

## Step 7: Bootstrap CDK

Once. Per account+region pair.

```bash
cd /c/Users/you/Documents/razorpay-payment-integration/infra
uv sync --extra dev
AWS_PROFILE=razorpay-prod uv run cdk bootstrap aws://<account-id>/ap-south-1
```

CDK provisions an S3 bucket and ECR repo for synth assets. Free Tier covers the storage.

## Step 8: First synth (no deploy)

```bash
AWS_PROFILE=razorpay-prod uv run cdk synth
```

You should see a CloudFormation template stream to stdout. No resources created yet.

## Step 9: First deploy (Phase 17, not yet)

When you're ready:

```bash
AWS_PROFILE=razorpay-prod uv run cdk diff
AWS_PROFILE=razorpay-prod uv run cdk deploy razorpay-free-track-foundation
```

Read the diff carefully. Approve only when it matches expectations.

## What you can ignore

- **Free Tier expiry.** Most line items renew monthly so the 12-month clock only applies to the initial credits. Lambda, SQS, CloudWatch, ECR all have "always free" tiers below our expected volume.
- **Service quotas.** Defaults are generous for a payment integration. We'll hit them only at >10k events/day, which is years away.
- **GovCloud / China partitions.** Not relevant for this project.

## Related

- [`iam-bootstrap.md`](iam-bootstrap.md) — role and policy setup.
- [`cost-controls.md`](cost-controls.md) — budget alarm details.
- `infra/README.md` — CDK operational guide.
