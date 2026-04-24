# EC2 Deploy Relay

A long-lived EC2 box ("the relay") that holds the authoritative production
`.env` and an authenticated `phala` CLI. The GitHub Actions deploy
workflow (`.github/workflows/deploy.yml`) SSHes here and runs
`deploy/phala/deploy.sh` — **GitHub never sees the production secrets**.

Current relay: **13.218.227.123** (us-east-1, `ubuntu@ip-172-31-23-66`).
Connect locally via: `ssh -i ~/Desktop/keys/timeline-tuner-dashboard-dev.pem ubuntu@13.218.227.123`
(once bootstrapped, a dedicated CI key also works — see "SSH keys" below).

## Why a relay instead of GitHub secrets

1. **Single source of truth**: `~/hivemind-core/deploy/phala/.env` on
   the relay is THE production env. Rotating a key means `vi` on one
   file on one box, not re-uploading ~17 individual GH secrets.
2. **`phala` CLI is stateful**: credentials live in
   `~/.phala-cloud/credentials.json`. The relay holds that state so each
   deploy doesn't re-authenticate.
3. **Blast radius**: a compromised GH Actions run only gets SSH access
   to the relay (ephemeral, key rotates on a single `gh secret set`),
   not the actual env file. A compromised relay is bad, but that risk
   already exists independently of the deploy flow.

## Bootstrap (one-time)

From a fresh Ubuntu 24.04 EC2 with passwordless `sudo` for `ubuntu`:

```bash
# 1. Install Node 20 + phala CLI
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g phala

# 2. Authenticate phala CLI (interactive, one-time)
phala login
phala cvms list   # sanity — should show hivemind-core + hivemind-pg

# 3. Add a read-only GitHub deploy key so the relay can pull
ssh-keygen -t ed25519 -C "hivemind-core-ec2-deploy-key@github" \
  -f ~/.ssh/hivemind_core_deploy -N ""
cat ~/.ssh/hivemind_core_deploy.pub
# Paste the pubkey into GitHub → repo → Settings → Deploy keys
# Read-only (the relay should never push)

cat >> ~/.ssh/config <<'EOF'
Host github-hivemind-core
  HostName github.com
  User git
  IdentityFile ~/.ssh/hivemind_core_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
ssh-keyscan github.com >> ~/.ssh/known_hosts

# 4. Clone the repo
cd ~
git clone git@github-hivemind-core:Account-Link/hivemind-core.git
cd hivemind-core

# 5. Populate .env — every ${VAR:?} in BOTH compose files must exist
#    here. deploy.sh's pre-check will fail loudly otherwise.
cp deploy/phala/.env.example deploy/phala/.env  # if an example exists
vi deploy/phala/.env
chmod 600 deploy/phala/.env

# 6. Test locally on the relay
./deploy/phala/deploy.sh core   # or `all` to also do postgres
```

## Add a CI SSH key

The GH Action uses a dedicated key (not the AWS PEM):

```bash
# on your laptop
TMP=$(mktemp -d)
ssh-keygen -t ed25519 -C "hivemind-core-ci-deploy@github" \
  -f "$TMP/id_ed25519" -N ""

# install public key on the relay
ssh ubuntu@13.218.227.123 \
  "echo $(cat $TMP/id_ed25519.pub) >> ~/.ssh/authorized_keys"

# upload private key as a repo secret
gh secret set EC2_SSH_PRIVATE_KEY --body "$(cat $TMP/id_ed25519)"
gh secret set EC2_HOST --body "13.218.227.123"
gh secret set EC2_USER --body "ubuntu"

# (optional) cache the phala API token as a secret too — the workflow
# forwards it as PHALA_CLOUD_API_KEY env, which overrides the relay's
# stored credentials.json for that run.
gh secret set PHALA_CLOUD_API_KEY --body "phak_..."

rm -rf "$TMP"
```

## Deploying

### From GitHub (normal path)

Actions → "Deploy to Phala CVM (via EC2 relay)" → Run workflow:
- `target`: `core`, `postgres`, or `all`
- `ref`: branch / tag / commit to deploy (default `main`)
- `image_sha`: optional override; rewrites `hivemind-core`'s image pin
  in the working tree for that run only (not committed)

### Directly on the relay (ops / debugging)

```bash
ssh ubuntu@13.218.227.123
cd ~/hivemind-core
git fetch origin && git checkout --detach origin/<ref>
./deploy/phala/deploy.sh core
```

## What `deploy.sh` does (summary)

See `deploy/phala/deploy.sh` for the full script. Three guardrails:

1. **Pre-check**: every `${VAR:?}` in the compose file must be in `.env`.
   Fails before touching the CVM. This is what catches the "sealed envs
   silently dropped on `phala deploy -e`" failure mode.
2. **Deploy → envs update → restart**: `phala deploy` replaces sealed
   envs with whatever is in `-e`, so we explicitly `phala envs update`
   afterwards and restart to be sure nothing was lost.
3. **Health poll + serial-log dump**: polls the gateway URL until HTTP
   200, or on timeout dumps the last 40 lines of serial-logs to stderr
   so the operator sees the boot failure (usually an interpolation
   error).

## Rotating the relay

When this EC2 dies or you move providers:

1. Provision a new Ubuntu box, run the bootstrap above.
2. `gh secret set EC2_HOST --body "<new-ip>"`.
3. Re-install the CI public key via the "Add a CI SSH key" section.
4. Test with `gh workflow run "Deploy to Phala CVM (via EC2 relay)"`.

Nothing else changes — the `phala` CLI and `.env` live on the new box,
and the deploy script is self-contained.
