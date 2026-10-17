# Onboarding a repository

Roughly 30 minutes. Steps marked **(you)** are outward-facing or involve secrets and are done by the repository owner, never by an agent.

## 1. Pick the stack

Use an existing stack (`images/stacks/<name>`). For a new one, copy a stack folder, change the devcontainer features (seed them from the target repo's own `.devcontainer` if it has one), and add it to the `stacks` matrix in `.github/workflows/release.yml`. A role-only extra (browsers, profilers) becomes a toolset under `images/toolsets/`.

## 2. Prepare the target repository (you)

1. For a fork: enable **Issues** and **Actions** in the fork's settings (both are off by default on forks). Do not add an `upstream` remote anywhere the agent runs.
2. Create the base branch the agent targets, e.g. `ai-staging`, from `main`.
3. Add a ruleset on `main`: require a pull request with **your** approval and the CI check; block force pushes and deletion; no bypass for the agent Apps. (Rulesets need a public repo or a paid plan.)
4. Commit a `CLAUDE.md` to the base branch with the exact build and test commands and house rules. Agents read it; they cannot edit it.

## 3. Agent identities (you, once per role — not per repo)

```bash
python3 scripts/provision_apps.py --prefix jk-agent \
  --out <home-ops-hiro>/kubernetes/apps/agents/agent-shared/app \
  --roles planner implementer fixer review-quality
```

Per role: the browser opens, you click **Create GitHub App**, the script writes the App id and private key into `agent-app-<role>.sops.yaml` and encrypts it with sops (it warns loudly if sops is missing). It never prints the key. Then click the printed install link and install the App on the target repository. For a second repository, add it to each existing App's installation — no new Apps.

The script prints `appSlug`, `appUserId` and `appSecret` per role for the HelmRelease values.

Permissions it requests: writer roles (implementer, fixer, merger) get Contents/Issues/Pull requests write; commenter roles (planner, designer, reviewers) get Contents read and Issues/Pull requests write. None gets Workflows. Apps are free; each installation has its own API rate limit.

## 4. Claude token (you, once)

Run `claude setup-token` on your own machine (valid one year) and encrypt it into `agent-shared/app/secret.sops.yaml`. Never paste it into chat, issues or logs.

## 5. Labels

```bash
GH_TOKEN=<a token with Issues write on the repo> AGENT_CONFIG=pipeline.toml agent-tick setup
```

Idempotent: creates missing labels and checks the base branch. Add `--dry-run` to see the writes first.

## 6. Deploy (GitOps)

Add `kubernetes/apps/agents/agent-<repo>/` in home-ops-hiro: `ks.yaml` (dependsOn `agent-shared`), an `OCIRepository` named after the app pointing at `oci://ghcr.io/jasonkoopmans/charts/agent-pipeline`, and a `HelmRelease` with values (see `examples/eshop-values.yaml`). Keep `suspend: true` for the first runs.

## 7. Smoke test

1. Open a small issue (you), label it `ai-ready`.
2. `kubectl -n agents create job --from=cronjob/agent-<repo>-planner planner-1` and read the plan comment.
3. Add `ai-plan-approved` (you), then run the implementer, then the reviewer the same way.
4. When every stage behaves, set `suspend: false`.

## Talking to the agents

An agent that is blocked by a question posts it on the issue or PR, mentions you, and adds `ai-needs-input`. Reply in a comment; the next run of that role picks it up (the label is removed automatically) and sees your reply marked as new. Comments from anyone else are ignored.

## Pausing and stopping

- One issue: remove `ai-ready` (its PR pauses too).
- One role: `roles.<name>.enabled: false` or `suspend: true` in values.
- Everything: `suspend: true`; to be certain, suspend the GitHub Apps in their settings.
