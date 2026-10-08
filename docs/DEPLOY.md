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
 https://boundary.…  │  Caddy ──► dashboard (sign in; chat for users,     agent ◄── Prometheus (opt-in) │
                     │    │       dashboard for admins)                     │                          │
                     │    └─────► agent /api (checks the sign-in and role)  ├── Postgres, Redis        │
                     │                                                      └── model cache (volume)   │
                     └──────────────────────────────────────────────────────────────────────────────────┘
```

**Hostname:** a free `sslip.io` name that resolves to the server's IP, so no domain to buy. For IP
`3.91.20.7`: `boundary.3-91-20-7.sslip.io`. Everyone signs in there: user accounts get the chat,
admin accounts get the dashboard (the agent enforces the roles on every API call). Caddy gets a real
HTTPS certificate for it automatically.

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
- **Instance type:** **t4g.large**. ARM (Graviton) is ~20% cheaper than the same-size x86
  (t3.large) and is what these images were built and rehearsed on. `t` instances are *burstable*:
  full CPU in bursts and ~30% sustained, which is plenty for demos. For heavier sustained load (e.g.
  publishable load-test numbers), **m7g.large** (ARM, Graviton3, 8 GB, ~$0.082/h) is the
  non-burstable upgrade, with nothing else changing.
- **Key pair:** `boundary`
- **Network:** existing security group `boundary-web`; auto-assign public IP on
- **Storage:** **30 GiB gp3** (images and models need ~15 GB)
- **Advanced:** leave *Metadata version: V2 only* (the default)

Then *Launch*.

## 5. A fixed IP (Elastic IP)

Your hostname contains the IP, so it must not change when you stop and start the server.

1. *EC2 → Elastic IPs → Allocate Elastic IP address → Allocate*.
2. *Actions → Associate* → choose instance `boundary`.
3. Note the address, e.g. `3.91.20.7`. Your hostname is now `boundary.3-91-20-7.sslip.io`.

## 6. Prepare the server

Your laptop's terminal is **laptop$**; the server's is **server$**. Use your own Elastic IP wherever
you see `3.91.20.7`.

### 6.1 Connect

```bash
laptop$ ssh -i ~/.ssh/boundary.pem ubuntu@3.91.20.7
```

- The first time, SSH says *"The authenticity of host … can't be established. ED25519 key
  fingerprint is SHA256:…"*. Type `yes`. To be thorough first, compare the fingerprint with the one
  in *EC2 → Instances → boundary → Actions → Monitor and troubleshoot → Get system log* (near the end,
  under `BEGIN SSH HOST KEY FINGERPRINTS`).
- The user is always **`ubuntu`** on Ubuntu AMIs.

| If you see | Do this |
|---|---|
| `Permission denied (publickey)` | wrong user (must be `ubuntu`), wrong key file, or the key isn't `chmod 400` |
| `Connection timed out` | the security group's SSH rule doesn't match your current IP (*Edit inbound rules → My IP*), or the instance is still booting (wait a minute) |
| `UNPROTECTED PRIVATE KEY FILE` | `chmod 400 ~/.ssh/boundary.pem` |

**Optional shortcut.** Add this to `~/.ssh/config` on your laptop, and from then on `ssh boundary` is
enough:

```
Host boundary
    HostName 3.91.20.7
    User ubuntu
    IdentityFile ~/.ssh/boundary.pem
```

### 6.2 Check you got the right machine

```bash
server$ uname -m                  # aarch64        (ARM)
server$ nproc && free -h          # 2 CPUs; Mem total about 7.6Gi
server$ df -h /                   # Size about 29G
server$ lsb_release -ds           # Ubuntu 24.04.x LTS
```

If `uname -m` says `x86_64`, you launched an x86 instance. That works, but it isn't what we tested.
If the disk shows ~8G, the storage setting in step 4 was missed. Fix it with *EC2 → Volumes → Modify
→ 30*, then `sudo growpart /dev/nvme0n1 1 && sudo resize2fs /dev/nvme0n1p1`.

### 6.3 Update the operating system

```bash
server$ sudo apt-get update && sudo apt-get -y upgrade
```

- If a purple *"Daemons using outdated libraries"* screen appears, press **Enter** to accept.
- If `/var/run/reboot-required` now exists (`ls /var/run/reboot-required`), reboot and reconnect
  after about a minute:

```bash
server$ sudo reboot
laptop$ ssh -i ~/.ssh/boundary.pem ubuntu@3.91.20.7
```

Ubuntu installs security updates automatically from now on (`systemctl is-enabled
unattended-upgrades` should print `enabled`).

### 6.4 Install Docker

```bash
server$ curl -fsSL https://get.docker.com -o get-docker.sh
server$ sudo sh get-docker.sh                 # Docker's official installer (~1 minute)
server$ sudo usermod -aG docker ubuntu        # let your user run docker without sudo
server$ exit
laptop$ ssh -i ~/.ssh/boundary.pem ubuntu@3.91.20.7   # log back in so the group applies
server$ docker run --rm hello-world           # prints "Hello from Docker!"
server$ docker compose version                # Docker Compose version v2.x
server$ systemctl is-enabled docker           # enabled: Docker (and the app) start on boot
```

Being in the `docker` group is equivalent to root on this machine. Keep it to your own user.

### 6.5 Add swap (a safety net, not extra memory)

```bash
server$ sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
server$ sudo mkswap /swapfile && sudo swapon /swapfile
server$ echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
server$ echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf && sudo sysctl --system >/dev/null
server$ free -h                               # Swap: 4.0Gi
```

`swappiness=10` tells Linux to keep the guard's models in RAM and only use swap under real
pressure. Models being paged out is exactly what made the laptop slow (see [RESULTS.md](RESULTS.md)).

### 6.6 Get the code

```bash
server$ git clone https://github.com/neeldholiya04/boundary_ai.git ~/boundary_ai
server$ cd ~/boundary_ai && git log -1 --oneline       # the same latest commit GitHub shows
```

Keep it at **`~/boundary_ai`**: the deploy pipeline (Part 3) looks for it there.

**If the repo is private**, give the server a read-only deploy key and clone over SSH instead:

```bash
server$ ssh-keygen -t ed25519 -N "" -f ~/.ssh/github_deploy -C boundary-server
server$ cat ~/.ssh/github_deploy.pub
```

1. Paste the `.pub` output into *GitHub → repo → Settings → Deploy keys → Add deploy key* and leave
   "write access" unticked.
2. Clone with that key:

```bash
server$ GIT_SSH_COMMAND="ssh -i ~/.ssh/github_deploy" git clone git@github.com:neeldholiya04/boundary_ai.git ~/boundary_ai
server$ cd ~/boundary_ai && git config core.sshCommand "ssh -i ~/.ssh/github_deploy"
```

## 7. Configure (`.env` on the server)

### 7.1 Get the keys ready (in your laptop's browser)

**OpenAI.** Use a separate project, so the demo's spending is capped and can be revoked on its own.
1. *platform.openai.com → Settings → Projects → Create project* `boundary-demo`.
2. In that project, under *Limits*, set a monthly budget (e.g. $10).
3. *API keys → Create new secret key* (project `boundary-demo`). Copy it now: it's shown only once.

**Hugging Face** (required):
1. Open `huggingface.co/meta-llama/Llama-Prompt-Guard-2-86M` while logged in. It should say you've
   been granted access. If not, request it and wait for the approval email.
2. *Settings → Access Tokens → Create new token*, type **Read**, name `boundary-server`. Copy it.

**Optional:** an Exa API key (`dashboard.exa.ai`) and Langfuse keys (*project → Settings → API
keys*). You can add them later and run `infra/deploy.sh` again.

### 7.2 Create the file

```bash
server$ cd ~/boundary_ai
server$ cp .env.example .env && chmod 600 .env
server$ openssl rand -hex 24          # copy this: it's your Postgres password
server$ nano .env
```

In `nano`:
- Move with the arrow keys and paste with your terminal's paste (⌘V on a Mac).
- Search with **Ctrl-W**.
- Save with **Ctrl-O** then **Enter**, and exit with **Ctrl-X**.

### 7.3 Set these lines

Change only these. Leave every other line as it is: the deploy stack sets its own database URL, CORS
origin and so on, and ignores the local-development lines.

| Line | Set to |
|---|---|
| `POSTGRES_PASSWORD=` | the `openssl rand -hex 24` output from 7.2 |
| `OPENAI_API_KEY=` | the OpenAI key from 7.1 |
| `HF_TOKEN=` | the Hugging Face token from 7.1 |
| `LLM_DAILY_BUDGET_USD=` | `2` (the most real LLM spend per day; playground built-ins are free replays) |
| `GRAFANA_ADMIN_PASSWORD=` | another `openssl rand -hex 16` (replace the template's default even if you won't use Grafana) |
| `PUBLIC_HOST=` | `boundary.3-91-20-7.sslip.io` (your IP, dots replaced with dashes) |
| `AUTH_USERS=` | the sign-in accounts, from 7.4 |
| `AUTH_SECRET=` | `openssl rand -hex 32` (signs sign-in tokens) |
| `ACME_EMAIL=` | your email (optional; Let's Encrypt only uses it for certificate expiry notices) |
| `EXA_API_KEY=`, `LANGFUSE_*` | optional |

**Faster alternative to editing by hand:** fill the same lines with commands. Secrets are typed at
hidden prompts, so they don't appear on screen or in shell history. This does steps 7.3 and 7.4 in
one go:

```bash
server$ cd ~/boundary_ai && cp .env.example .env && chmod 600 .env
server$ EIP=3.91.20.7                                  # <- your Elastic IP
server$ set_env() { if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi; }
server$ set_env POSTGRES_PASSWORD "$(openssl rand -hex 24)"
server$ set_env GRAFANA_ADMIN_PASSWORD "$(openssl rand -hex 16)"
server$ set_env AUTH_SECRET "$(openssl rand -hex 32)"
server$ set_env LLM_DAILY_BUDGET_USD 2
server$ set_env PUBLIC_HOST "boundary.${EIP//./-}.sslip.io"
server$ read -rs -p "OpenAI key: " K; echo; set_env OPENAI_API_KEY "$K"; unset K
server$ read -rs -p "Hugging Face token: " K; echo; set_env HF_TOKEN "$K"; unset K
server$ read -rs -p "Admin password: " A; echo; read -rs -p "User password: " U; echo
server$ set_env AUTH_USERS "admin:$A:admin,user:$U:user"; unset A U
server$ set_env ACME_EMAIL "you@example.org"           # optional
```

### 7.4 The sign-in accounts

`AUTH_USERS` lists every account as `name:password:role`, comma-separated. The role is `user` (the
chat, with its own history) or `admin` (the dashboard: Guardrails, Approvals, Logs, Tools,
Playground). For example:

```
AUTH_USERS=admin:<admin password>:admin,user:<user password>:user
```

- Choose strong passwords and save them in your password manager. They guard the whole site; the
  local defaults `admin123` / `user123` must never be used here.
- Use letters and digits only (`openssl rand -hex 16` makes a good one). `:` and `,` separate the
  entries, and `|` or `&` would break the `set_env` helper above.
- Add more accounts by appending more entries. Failed sign-ins are throttled per client (10 per 5
  minutes), counted in the database, so a restart doesn't reset them.
- `AUTH_SECRET` signs the sign-in tokens, which expire after 12 hours. Changing it signs everyone out.

### 7.5 Check the file before deploying

```bash
server$ ls -l .env
# -rw------- (only you can read it)
server$ RELEASE=check docker compose --env-file .env -f infra/docker-compose.deploy.yml config -q && echo "config OK"
server$ grep -E '^PUBLIC_HOST=' .env
server$ grep -oE '^AUTH_USERS=|:(user|admin)(,|$)' .env | tr -d '\n'; echo
```

- `RELEASE=check` stands in for the release tag, which `infra/deploy.sh` sets and records in `.env`.
- The `config -q` check prints only errors (never the values). Seeing `config OK` means nothing
  required is missing. Otherwise it names the setting, e.g. `set AUTH_SECRET in .env`.
- The first `grep` shows the hostname; the second shows the account roles without the passwords
  (e.g. `AUTH_USERS=:admin,:user`).

## 8. Build and start

### 8.1 Run the deploy inside `tmux`

The first deploy takes 15–25 minutes. Running it inside `tmux` means a dropped connection doesn't
stop it:

```bash
server$ tmux new -s deploy
server$ cd ~/boundary_ai && infra/deploy.sh
```

- To detach and leave it running, press **Ctrl-B** then **D**.
- To come back to it, run `tmux attach -t deploy`.

### 8.2 What you'll see

| Phase | Roughly | On screen |
|---|---|---|
| Pull base images (Postgres, Redis, Caddy, Python, Node) | 1–2 min | `Pulling …` lines |
| Build the agent image: dependencies incl. PyTorch (~1.5 GB) | 5–10 min | `#… uv sync`, `Downloaded torch` |
| Build the dashboard image | 3–5 min | `npm ci`, `next build`, `Compiled successfully` |
| Start the containers | seconds | `Container … Started` |
| Agent downloads the guard's models (~2 GB) and loads them | 3–8 min | the script waits quietly here |
| Done | | a table of services, `agent … (healthy)`, then `deployed <commit>` |

Later deploys are much faster: cached layers rebuild only what changed, and the models are already
on disk.

### 8.3 Watch it from a second SSH window (optional)

```bash
server$ cd ~/boundary_ai
server$ docker compose --env-file .env -f infra/docker-compose.deploy.yml logs -f agent
#  (model downloads…) then: "Application startup complete."
server$ docker compose --env-file .env -f infra/docker-compose.deploy.yml logs caddy | grep -i "certificate obtained"
#  one line per host when HTTPS is ready
```

### 8.4 Check the machine is comfortable

```bash
server$ docker compose --env-file .env -f infra/docker-compose.deploy.yml ps   # all Up; agent (healthy)
server$ free -h                     # used about 3-4Gi of 7.6Gi; swap mostly unused
server$ docker stats --no-stream    # memory per container: agent is the big one
server$ df -h /                     # comfortably below 80% used
```

### 8.5 If something goes wrong

| What you see | Cause and fix |
|---|---|
| `missing .env` | you're not in `~/boundary_ai`, or step 7.2 was skipped |
| `required variable … is missing a value` | that setting is empty in `.env` (step 7.3) |
| Agent log: `401 … huggingface` or `gated repo` | `HF_TOKEN` is wrong, or the account hasn't been granted access to Llama Prompt Guard 2 (step 7.1) |
| Agent restarts; `docker inspect -f '{{.State.OOMKilled}}' $(docker compose --env-file .env -f infra/docker-compose.deploy.yml ps -q agent)` says `true` | out of memory: check `free -h`; don't run the observability profile on a smaller instance |
| Caddy log: `challenge failed` / `no such host` | ports 80/443 aren't open to *Anywhere* (step 3), or the hostname's IP part doesn't match the Elastic IP |
| `agent not healthy after 15 minutes` | the script prints the last 80 log lines; usually the HF token or memory, see above |
| A build step fails while downloading | network hiccup: run `infra/deploy.sh` again (finished steps are cached) |

## 9. Check it works

### 9.1 Automated checks (from your laptop)

```bash
laptop$ cd ~/personal/boundary_ai && git pull
laptop$ infra/smoke-test.sh https://boundary.3-91-20-7.sslip.io
```

It asks for the admin account's password (the `admin` entry in `AUTH_USERS`; set `ADMIN_USER` if it
has another name) and checks 11 access rules:
- The sign-in page loads; the chat, admin and playground APIs refuse requests without a sign-in.
- `/metrics` returns 404.
- A wrong password is refused; the admin can sign in and reach the admin and playground APIs.
- The HSTS header is set.

It ends with:

```
ok   /health                                      got 200 (want 200)
ok   sign-in page                                 got 200 (want 200)
…
ok   playground API for the admin                 got 200 (want 200)
ok   HSTS header                                  got 1 (want 1)

all checks passed
```

Any `FAIL` line names the rule that broke; the troubleshooting table at the end of this guide maps
the usual ones.

### 9.2 As a user

Open **https://boundary.3-91-20-7.sslip.io**:
- The browser shows a padlock. It's a real Let's Encrypt certificate.
- It shows the sign-in page. Sign in with the `user` account.
- You land on a full-screen chat with no dashboard links. Opening `/guardrails` by hand sends you
  back to the chat.
- *"In one sentence, what is Prometheus used for?"* gets a completed answer.

### 9.3 As an admin

Sign out, then sign in with the `admin` account:
- You land on **Guardrails**, with the dashboard sidebar and no chat.
- **Guardrails:** the policy file version and its enforced and shadow checks are listed.
- **Tools (MCP):** `local-sandbox` and `exa` both connected.
- **Playground → Attack:** pick `e2e-ind-blog-planted-write` and press *Run with and without the
  guard*. After about 20–40 seconds, *No defence* shows **Hijacked** with a red
  `write_file notes/pwned.md`, and *Guard on* shows **Attack stopped** (waiting for approval).
- **Playground → Scan:** stage *User input*, text `Hi, I'm Jane Roe, email me at
  jane.roe@example.com`. The email is highlighted and replaced with `<EMAIL_1>`.

### 9.4 Load the demo files (for [DEMO.md](DEMO.md))

The agent's sandbox is a Docker volume on the server. Copy the demo fixtures into it:

```bash
server$ cd ~/boundary_ai
server$ for f in github_issue_exfil.md issue_env_paste.md crm_export.csv prompt_injection_explainer.html; do
          docker compose --env-file .env -f infra/docker-compose.deploy.yml \
            cp "packages/eval/datasets/golden/fixtures/$f" "agent:/data/mcp-sandbox/$f"
        done
```

Then sign in as the user and ask the chat *"list files"*: it should show all four.

### 9.5 Write down, then stop for the day

Note these in your password manager:
- the URL
- the account names and passwords (`AUTH_USERS`)
- the instance ID (`i-…`)

When you're done, **stop the instance** (step 12). Starting it again brings everything back,
including HTTPS.

# Part 2: Operate

## 10. Monitoring

**Traces:** put `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST` in `.env`, then run
`up -d` again (step 8). Every request then shows up in your Langfuse project, and Playground attack
runs link straight to their trace. To share one trace with someone without a Langfuse account, open
it in Langfuse and switch it to public with the share toggle.

**Metrics:** start Prometheus and Grafana on the server. `infra/deploy.sh` leaves them running but
doesn't update them, so run this again after a deploy that changes their settings:

```bash
server$ cd ~/boundary_ai
server$ docker compose --env-file .env -f infra/docker-compose.deploy.yml --profile observability up -d
```

Both are then public and read-only on the same hostname:
- **Grafana:** https://boundary.3-91-20-7.sslip.io/grafana/ opens the *boundary-ai: guard & agent*
  dashboard. Visitors view it without signing in. There is no login form, so the dashboard can't be
  edited from the web (change it in `infra/observability/grafana/dashboards/` and redeploy).
- **Prometheus:** https://boundary.3-91-20-7.sslip.io/prometheus/ for ad-hoc queries, e.g.
  `sum by (action) (rate(guard_checks_total[5m]))`. Its admin and shutdown APIs are off, and queries
  are capped at 20 s and 4 at a time.

The metrics hold counts, latencies, model names and policy names, never prompts, accounts or keys.
While the profile is stopped, both paths return 502. To make them private again, stop the profile:

```bash
server$ docker compose --env-file .env -f infra/docker-compose.deploy.yml --profile observability stop prometheus grafana
```

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

Always deploy (and roll back) with `infra/deploy.sh`. It records the release as `RELEASE=` in `.env`,
and every other `docker compose ... up` (like starting the observability profile in step 10) then
reuses exactly those images. The compose file has no default tag on purpose: once, a plain `up`
fell back to an old image and replaced the live agent with a build from before sign-in.

A guard policy can also be rolled back live, without a deploy: switch a policy's mode on the admin
*Guardrails* page.

### Upgrading from the two-host setup

Releases before sign-in served a public playground on `boundary.…` and the dashboard on `admin.…`
behind basic auth. A server set up that way needs its `.env` and GitHub settings changed once,
before the first deploy of the sign-in release (until then that deploy stops at the config check and
the old release keeps running):

1. On the server, edit `.env` (step 7.4 explains the accounts). An older `.env` has no `AUTH_*`
   lines yet, so add them rather than looking for them:
   - add `AUTH_USERS=admin:<admin password>:admin,user:<user password>:user`
   - add `AUTH_SECRET=` the output of `openssl rand -hex 32`
   - delete the `ADMIN_HOST`, `ADMIN_USER` and `ADMIN_PASSWORD_HASH` lines
   - keep `PUBLIC_HOST` as it is: it's now the only URL
2. Check it: `RELEASE=check docker compose --env-file .env -f infra/docker-compose.deploy.yml config -q && echo "config OK"`.
3. On GitHub, in the `production` environment (step 19): set the `ADMIN_PASSWORD` secret to the new
   admin password, and delete the `ADMIN_URL` variable.
4. Deploy (*Actions → deploy → Run workflow*, or `infra/deploy.sh` on the server).

The `admin.…` address stops working: Caddy no longer has a certificate or site for it. Rolling back
to a release from before sign-in needs the deleted lines back in `.env`.

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

- **Environment secret** `ADMIN_PASSWORD`: the admin account's password from `AUTH_USERS`, which
  the smoke test uses for its sign-in checks.

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
| Deploy succeeds, smoke test fails | run `infra/smoke-test.sh` from your laptop to see which rule; `ADMIN_PASSWORD` may not match the admin entry in the server's `AUTH_USERS` |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Browser warns about the certificate, or Caddy logs `challenge failed` | ports 80/443 not open to *Anywhere*, or the hostname's IP part doesn't match the Elastic IP |
| `401` from Hugging Face in the agent log | `HF_TOKEN` missing, or the account hasn't accepted the Llama Prompt Guard 2 licence |
| The sign-in never works | `AUTH_USERS` malformed (`name:password:role`, comma-separated, no `:` or `,` in passwords); the agent log says which entry |
| Agent restarts, `Killed` in the log | out of memory: check `free -h`; stop the observability profile, or use a bigger instance |
| Guard checks time out / benign requests blocked | memory pressure (see [RESULTS.md](RESULTS.md) caveats); on t4g.large this should be rare |
| Can't SSH any more | your IP changed: update the security group's SSH rule to *My IP* |
