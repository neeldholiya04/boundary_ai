# Deploying to AWS (one EC2 server)

This takes boundary-ai from a laptop to a public HTTPS site. It runs on one ARM server: Caddy (HTTPS
proxy), the agent, the dashboard, Postgres and Redis, all from `infra/docker-compose.deploy.yml`.
Plan about an hour the first time; most of that is waiting for downloads.

| Part | Steps | Result |
|---|---|---|
| **1. Provision and go live** | 1–9 | the site is up on HTTPS, checked by a smoke test |
| **2. Operate** | 10–14 | monitoring, releases and rollback, stopping to save money, load test, teardown |
| **3. CI/CD** | 15–19 | every PR is gated by the evals; every merge to `main` deploys itself |

```
                     ┌──────────────────────── EC2 t4g.large (Ubuntu 24.04, ARM) ───────────────────────┐
 https://boundary.…  │  Caddy ──► dashboard (Playground page only)       agent ◄── Prometheus (opt-in)  │
 (public playground) │    │   ──► agent: /api/guard/scan, /api/playground/*  │                          │
                     │    │                                                 ├── Postgres, Redis        │
 https://admin.…     │    └──(basic auth)──► full dashboard + full API        └── model cache (volume)   │
 (you only)          └──────────────────────────────────────────────────────────────────────────────────┘
```

**Hostnames:** two free `sslip.io` names that resolve to the server's IP, so no domain to buy. For
IP `3.91.20.7`: `boundary.3-91-20-7.sslip.io` (public) and `admin.3-91-20-7.sslip.io` (admin).
Caddy gets real HTTPS certificates for both automatically.

# Part 1: Provision and go live

## What it costs

Prices are on-demand in us-east-1 and roughly similar in other regions. Check the console for yours.

| Item | Rate | Per month if left on |
|---|---|---|
| t4g.large (2 vCPU, 8 GB) | ~$0.067 / hour | ~$49 |
| 30 GB gp3 disk | ~$0.08 / GB-month | ~$2.40 |
| Public IPv4 (Elastic IP) | ~$0.005 / hour | ~$3.60 |
| LLM calls | capped by `LLM_DAILY_BUDGET_USD` (default $2/day) | depends on use |

**Stop the instance between demos** (step 12). A stopped instance costs only the disk and the IP
(~$6/month). New AWS accounts may also have free-tier credits.

## 1. Account safety first

1. Sign in to your **personal** AWS account in the console, not a work one.
2. Turn on MFA for the root user: *IAM → Dashboard → Add MFA*. Use the root user only for that and
   for billing.
3. Make an admin user for daily use: *IAM Identity Center* (recommended), or *IAM → Users → Create
   user* with `AdministratorAccess` and MFA. Sign in as that user from now on.
4. **Budget alarm:** *Billing and Cost Management → Budgets → Create budget → Monthly cost budget*.
   Set $20 (the course budget) with email alerts at 50%, 80% and 100%.
5. Pick a **region** close to your users (e.g. `ap-south-1` Mumbai, or `us-east-1`), and keep
   every step below in that region.

Optional, only if you also want the CLI: run `aws configure --profile boundary` (or
`aws configure sso --profile boundary`), and always pass `--profile boundary`. That keeps your
personal account apart from the existing `default` profile on this machine.

## 2. SSH key pair

*EC2 → Network & Security → Key Pairs → Create key pair*:
- Name: `boundary`
- Type: **ED25519**
- Format: `.pem`

It downloads once. Move it somewhere safe and lock it down:

```bash
mv ~/Downloads/boundary.pem ~/.ssh/boundary.pem
chmod 400 ~/.ssh/boundary.pem
```

## 3. Security group (the firewall)

*EC2 → Security Groups → Create security group*: name `boundary-web`, default VPC.

| Inbound rule | Port | Source | Why |
|---|---|---|---|
| SSH | 22 | **My IP** | only you can log in |
| HTTP | 80 | Anywhere-IPv4 (+ Anywhere-IPv6) | HTTPS certificates + redirect |
| HTTPS | 443 | Anywhere-IPv4 (+ Anywhere-IPv6) | the site |

Leave outbound as *all traffic*: the server downloads images, models and packages, and calls the
LLM. Do **not** open 3000, 3001, 8000, 5432 or 6379; those stay inside the server.

If your home IP changes later, edit the SSH rule back to *My IP*.

## 4. Launch the server

*EC2 → Instances → Launch instances*:
- **Name:** `boundary`
- **AMI:** *Ubuntu Server 24.04 LTS*, architecture **64-bit (Arm)**
- **Instance type:** **t4g.large**
- **Key pair:** `boundary`
- **Network:** existing security group `boundary-web`; auto-assign public IP on
- **Storage:** **30 GiB gp3** (images and models need ~15 GB)
- **Advanced:** leave *Metadata version: V2 only* (the default)

Then *Launch*.

## 5. A fixed IP (Elastic IP)

Your hostnames contain the IP, so it must not change when you stop and start the server.

1. *EC2 → Elastic IPs → Allocate Elastic IP address → Allocate*.
2. *Actions → Associate* → choose instance `boundary`.
3. Note the address, e.g. `3.91.20.7`. Your hostnames are now:
   - `boundary.3-91-20-7.sslip.io` (public playground)
   - `admin.3-91-20-7.sslip.io` (admin)

## 6. Prepare the server

```bash
ssh -i ~/.ssh/boundary.pem ubuntu@3.91.20.7
```

On the server:

```bash
# Updates, Docker (official repo) and git
sudo apt-get update && sudo apt-get -y upgrade
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
# A 4 GB swap file as a safety net for memory spikes (the models stay in RAM; this only helps peaks)
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
exit   # log out and back in so the docker group applies
```

```bash
ssh -i ~/.ssh/boundary.pem ubuntu@3.91.20.7
docker run --rm hello-world   # Docker works without sudo
git clone https://github.com/neeldholiya04/boundary_ai.git
cd boundary_ai
```

The clone above assumes the repo is public. If it's private, create a read-only deploy key
(`ssh-keygen -t ed25519` on the server, then add the `.pub` under *GitHub → repo → Settings →
Deploy keys*) and clone with `git@github.com:...`.

## 7. Configure (`.env` on the server)

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Set these and leave the rest as they are.

| Setting | Value |
|---|---|
| `POSTGRES_PASSWORD` | a long random string: `openssl rand -hex 24` |
| `OPENAI_API_KEY` | **a separate key just for this server**, from an OpenAI project with a monthly spend limit |
| `HF_TOKEN` | a Hugging Face *read* token, from the account that accepted the Llama Prompt Guard 2 licence (required) |
| `LLM_DAILY_BUDGET_USD` | e.g. `2`: the public playground can't spend more than this per day |
| `PUBLIC_HOST` | `boundary.3-91-20-7.sslip.io` |
| `ADMIN_HOST` | `admin.3-91-20-7.sslip.io` |
| `ADMIN_USER` | `admin` (or anything) |
| `ADMIN_PASSWORD_HASH` | see below |
| `ACME_EMAIL` | optional: your email, for certificate notices |
| `EXA_API_KEY`, `LANGFUSE_*` | optional |
| `GRAFANA_ADMIN_PASSWORD` | only if you'll use the observability profile (step 10) |

Make the admin password hash. The password itself is never stored or kept in shell history:

```bash
read -rs -p "Admin password: " PW; echo
docker run --rm caddy:2-alpine caddy hash-password --plaintext "$PW"; unset PW
```

Paste the output into `.env` **in single quotes**. The hash contains `$` signs that Docker Compose
would otherwise expand:

```
ADMIN_PASSWORD_HASH='$2a$14$...'
```

Secrets live only in this `.env` (mode 600). They are never baked into an image; `.dockerignore`
excludes every `.env`.

## 8. Build and start

```bash
infra/deploy.sh
```

This builds the images, tags them with the current commit, starts everything, and waits until the
agent reports healthy. Part 3's pipeline later runs the same script. To watch the agent while it
starts, use a second SSH session:

```bash
docker compose --env-file .env -f infra/docker-compose.deploy.yml logs -f agent
```

- **First build:** about 10–15 minutes (PyTorch and the rest, for ARM).
- **First start:** the agent downloads the guard's models (~2 GB) into the `hf_cache` volume, then
  loads them. That takes a few minutes; later starts take about a minute.
- **Ready** when the log shows `Application startup complete` (Ctrl-C stops following the log; the
  app keeps running).

Check status:

```bash
docker compose --env-file .env -f infra/docker-compose.deploy.yml ps   # agent: healthy
free -h                                                                  # memory headroom
```

## 9. Check it works

From your laptop, in the repo, swapping in your hostnames:

```bash
infra/smoke-test.sh https://boundary.3-91-20-7.sslip.io https://admin.3-91-20-7.sslip.io
```

It asks for the admin password and checks 15 access rules:
- The public host serves only the playground; its admin pages redirect away.
- The admin API and `/metrics` return 404 on the public host.
- The admin host needs the password.
- `/metrics` is hidden on the admin host too.
- HTTPS headers are set.

It should end with `all checks passed`. (The same script passed against a local rehearsal of this
stack before release.)

In a browser:
- `https://boundary.…`: the Playground. The sidebar shows **only** the Playground. Run the built-in
  attack `e2e-ind-blog-planted-write` (replayed, free), then a scan.
- `https://admin.…`: log in; the full sidebar appears. Send a chat, then check *Guardrails*, *Logs*
  and *Approvals*. Setting up the sandbox files for the full demo is in [DEMO.md](DEMO.md).

# Part 2: Operate

## 10. Monitoring

**Traces:** put `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST` in `.env`, then run
`up -d` again (step 8). Chat responses link to their trace.

**Metrics:** run Prometheus and Grafana on the server. They're only reachable through an SSH tunnel:

```bash
docker compose --env-file .env -f infra/docker-compose.deploy.yml --profile observability up -d
```

Then, on your laptop:

```bash
ssh -i ~/.ssh/boundary.pem -N -L 3001:127.0.0.1:3001 ubuntu@3.91.20.7
```

Open http://localhost:3001 for the *boundary-ai: guard & agent* dashboard.

**Health from AWS:** *EC2 → Instances → boundary → Monitoring* shows CPU. If memory or the disk get
tight, `docker stats` and `df -h` on the server tell you which.

## 11. Releases and rollback

Once Part 3 is set up, merging to `main` deploys automatically, and the *deploy* workflow's *Run
workflow* button rolls back (pass an older commit). By hand, on the server:

```bash
cd ~/boundary_ai
git fetch origin && git checkout --detach origin/main && infra/deploy.sh   # deploy the latest main
git checkout --detach <older-sha> && infra/deploy.sh                       # roll back
```

Every release keeps its images, tagged with the commit (`docker image ls boundary-agent`), so a
rollback to a recent commit starts in seconds without rebuilding.

A guard policy can also be rolled back live, without a deploy: switch a policy's mode on the admin
*Guardrails* page.

## 12. Stop and start (to save money)

- **Stop:** *EC2 → Instances → boundary → Instance state → Stop*. Nothing is lost: the database,
  models and certificates are on the disk, and the Elastic IP keeps your hostnames valid.
- **Start:** *Start instance*. Docker brings everything back up by itself (`restart:
  unless-stopped`), in about a minute.

## 13. Load test on the server (Phase 12 numbers)

Stop the live agent first; the load test starts its own, and two copies of the models won't fit:

```bash
cd ~/boundary_ai
C="docker compose --env-file .env -f infra/docker-compose.deploy.yml"
$C stop agent
$C run --rm --user root \
  -v "$PWD/infra:/app/infra:ro" -v "$PWD/packages/eval/results:/app/packages/eval/results" \
  agent uv run --no-sync --with locust boundary-eval loadtest --users 1,2,4 --duration 60 \
  --out packages/eval/results/loadtest-aws.json
$C start agent
cat packages/eval/results/loadtest-aws.md
```

It uses its own database (`boundary_loadtest`) and the stub planner, so it costs nothing and leaves
the app's data alone. Copy the JSON back to your laptop (`scp`) to compare it with the laptop run.

## 14. Tear everything down

When the project is over, do these in this order:
1. *EC2 → Instances → boundary → Terminate*. This also deletes its disk.
2. *Elastic IPs → Release* the address. An unattached Elastic IP still costs money.
3. Delete the key pair `boundary` and the security group `boundary-web`.
4. If you did Part 3: delete the IAM roles `boundary-github-deploy` and `boundary-ec2-ssm`, the
   GitHub OIDC identity provider, and the repo's `production` environment.
5. Revoke the server's OpenAI key and Hugging Face token.
6. Check *Billing → Bills* the next day: nothing should be running.

# Part 3: CI/CD

What runs, once this part is set up:

| Workflow | When | What it does |
|---|---|---|
| `ci` | every PR and every push to `main` | lint, tests, dataset validation, the detector eval gate, the end-to-end gate (cassette replay), the dashboard build; posts the eval as one PR comment |
| `deploy` | after `ci` passes on `main`, or by hand | deploys that commit to the server through AWS SSM, then smoke-tests the live site |
| `nightly-live` | daily | re-runs the end-to-end scenarios against the real model; opens a PR if the cassette changed |

## 15. CI: secrets and settings

On github.com, in your repo:

1. **Settings → Secrets and variables → Actions → New repository secret**:
   - `HF_TOKEN`: a Hugging Face **read** token from the account that accepted the Llama Prompt Guard
     2 licence. **Required**: the eval gates download the guard's models.
   - `OPENAI_API_KEY`: only for `nightly-live`. Use a separate key with a low monthly limit. Optional:
     without it, the nightly run skips cleanly.
2. **Settings → Actions → General**:
   - *Actions permissions*: allow GitHub actions and these: `astral-sh/setup-uv`,
     `peter-evans/create-pull-request`, `aws-actions/configure-aws-credentials` (or allow all).
   - *Workflow permissions*: keep **Read repository contents**, and tick **Allow GitHub Actions to
     create and approve pull requests** (the nightly job opens a PR).
3. **Check it:** *Actions → ci → the latest run → Re-run all jobs*.
   - `test` takes ~15–25 minutes the first time (it downloads the models) and ~8 minutes once they're
     cached.
   - `web` takes ~2 minutes.
   - Both should go green.

## 16. CI: protect `main`

**Settings → Rules → Rulesets → New branch ruleset**:
- **Name** `main`, **Enforcement** *Active*, **Target branches**: *Include default branch*.
- **Rules:** *Restrict deletions*, *Block force pushes*, and *Require a pull request before merging*
  (0 approvals is fine when you work alone).
- **Require status checks to pass:** add `test` and `web` (they're listed once CI has run once), and
  tick *Require branches to be up to date*.
- **Bypass list:** *Repository admin*, for emergencies only.

From now on, changes reach `main` through pull requests, and a PR whose eval gets worse can't merge.

## 17. CI: the "blocked PR" screenshot (for the README)

1. Make a branch that deliberately weakens the guard, for example by deleting one rule from
   `policies/rules/secrets.v1.yaml`, then push it and open a PR.
2. CI fails the detector gate, and the PR comment shows the `secrets` catch rate dropping with ❌
   ([CI.md](CI.md) has the expected output).
3. Screenshot the PR's checks and the comment, save it as `docs/img/blocked-pr.png`, then **close the
   PR without merging** and delete the branch.
4. Add the image to the README (or send it to me and I'll add it).

## 18. CD: the AWS side (once)

The deploy uses no SSH key and no open port. GitHub proves who it is with a short-lived OIDC token,
AWS lets that token assume one narrow role, and that role can only run a command on this one
instance through Systems Manager. Use the same region as the server throughout.

**a) Let Systems Manager manage the server.**
1. *IAM → Roles → Create role*: trusted entity *AWS service*, use case **EC2**. Add the permission
   **AmazonSSMManagedInstanceCore** and name it `boundary-ec2-ssm`.
2. *EC2 → Instances → boundary → Actions → Security → Modify IAM role*: choose `boundary-ec2-ssm`.
3. After a few minutes, *Systems Manager → Fleet Manager* lists the instance as *Online*. Ubuntu
   AMIs already include the SSM agent.

**b) Trust GitHub's OIDC tokens.** *IAM → Identity providers → Add provider*:
- *OpenID Connect*, provider URL `https://token.actions.githubusercontent.com`
- audience `sts.amazonaws.com`

**c) The deploy role.**
1. *IAM → Roles → Create role*: trusted entity **Web identity**, provider
   `token.actions.githubusercontent.com`, audience `sts.amazonaws.com`.
2. GitHub organization `neeldholiya04`, repository `boundary_ai`.
3. Skip permissions for now and name it `boundary-github-deploy`.
4. Open the role and check its **Trust relationships** match this. Edit the `sub` line so only the
   repo's `production` environment can use it:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:neeldholiya04/boundary_ai:environment:production"
      }
    }
  }]
}
```

5. **Permissions → Add permissions → Create inline policy → JSON**. Fill in your region, account ID
   and instance ID, then name it `deploy-to-boundary-only`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RunTheDeployOnThisInstanceOnly",
      "Effect": "Allow",
      "Action": "ssm:SendCommand",
      "Resource": [
        "arn:aws:ec2:<REGION>:<ACCOUNT_ID>:instance/<INSTANCE_ID>",
        "arn:aws:ssm:<REGION>::document/AWS-RunShellScript"
      ]
    },
    {
      "Sid": "ReadTheCommandResult",
      "Effect": "Allow",
      "Action": ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"],
      "Resource": "*"
    }
  ]
}
```

6. Copy the role's **ARN**, e.g. `arn:aws:iam::123456789012:role/boundary-github-deploy`.

## 19. CD: the GitHub side, then the first automatic deploy

**Settings → Environments → New environment** `production`:
- **Deployment branches and tags:** *Selected branches and tags* → `main`.
- Optional, **Required reviewers**: you. Each deploy then waits for your click.
- **Environment variables:**

  | Name | Value |
  |---|---|
  | `AWS_DEPLOY_ROLE_ARN` | the role ARN from step 18c |
  | `AWS_REGION` | e.g. `ap-south-1` |
  | `EC2_INSTANCE_ID` | e.g. `i-0abc123…` |
  | `PUBLIC_URL` | `https://boundary.3-91-20-7.sslip.io` |
  | `ADMIN_URL` | `https://admin.3-91-20-7.sslip.io` |

- **Environment secret** `ADMIN_PASSWORD`: the admin password, which the smoke test uses for its
  login checks.

**First run:** *Actions → deploy → Run workflow* (leave *ref* empty).
1. It assumes the role and runs `infra/deploy.sh` on the server at the latest `main`.
2. It shows the last lines of the deploy output, then runs `infra/smoke-test.sh` against the live
   site.
3. Green means deployed and checked.

From then on: **merge a PR → `ci` passes on `main` → `deploy` runs by itself.** To roll back, run
*deploy* with an older commit SHA as *ref*.

| CD problem | Fix |
|---|---|
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | the trust policy's `sub` doesn't match: the repo name, or the environment isn't exactly `production` |
| `AccessDenied ... ssm:SendCommand` | region, account or instance ID in the inline policy |
| `InvalidInstanceId` | the instance isn't managed by SSM yet: check step 18a and Fleet Manager |
| Deploy succeeds, smoke test fails | run `infra/smoke-test.sh` from your laptop to see which rule; `ADMIN_PASSWORD` may not match the hash in the server's `.env` |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Browser warns about the certificate, or Caddy logs `challenge failed` | ports 80/443 not open to *Anywhere*, or the hostname's IP part doesn't match the Elastic IP |
| `401` from Hugging Face in the agent log | `HF_TOKEN` missing, or the account hasn't accepted the Llama Prompt Guard 2 licence |
| `invalid ADMIN_PASSWORD_HASH` / the login never works | the hash isn't in single quotes in `.env` |
| Agent restarts, `Killed` in the log | out of memory: check `free -h`; stop the observability profile, or use a bigger instance |
| Guard checks time out / benign requests blocked | memory pressure (see [RESULTS.md](RESULTS.md) caveats); on t4g.large this should be rare |
| Can't SSH any more | your IP changed: update the security group's SSH rule to *My IP* |
