# agent-pipeline

Label a GitHub issue `ai-ready`. Agents plan it, implement it, review it independently and (optionally) merge the reviewed commit — one bounded step per Kubernetes CronJob tick, with all state on GitHub.

```
issue ai-ready ─▶ planner ─▶ [designer] ─▶ [owner adds ai-plan-approved] ─▶ implementer ─▶ PR
PR head SHA ─▶ reviewers in parallel (quality, security, e2e, …) ─▶ all required approve ─▶ ai-awaiting-merge ─▶ [merger]
                         └─ any required reviewer requests changes ─▶ fixer ─▶ new SHA ─▶ reviewers again
                            round > review_cap ─▶ ai-blocked (a human takes over)
any role ─▶ needs_input ─▶ questions posted to GitHub (ai-needs-input) ─▶ owner replies in a comment ─▶ next run continues
```

## Design

- **One role, one CronJob, one step.** A tick claims one unit of work for its role, runs one model session, publishes, and exits. Reviewers are separate sessions in separate pods, so they never share the implementer's context.
- **GitHub is the database and the conversation.** Labels hold state; agent comments carry hidden `<!-- agent:v1 {...} -->` markers for claims, plans, verdicts, rounds and questions. No session ever waits on a human: every role ends with `done`, `needs_input` (questions go to GitHub, the owner replies in a comment, a later run continues with the answer) or `cannot_proceed`. A pod dying mid-run leaves only a claim, reclaimed after `stale_claim_minutes`.
- **Each role has its own identity.** One GitHub App per role (`jk-agent-planner[bot]`, `jk-agent-review-quality[bot]`, …): comments, PRs, commits and native PR reviews show who did what. Trust is split: **instructions** come only from the owner's own comments; **pipeline state** is read only from agent identities. Neither can forge the other. (`scripts/provision_apps.py` creates the Apps; a PAT fallback exists for bootstrapping.)
- **Harness-neutral, Claude by default.** The `agent` stage calls an engine (`agent_tick/engines/`) chosen per role; the rest only sees a session request in and a session result out. The `claude` engine runs the unmodified Claude Code binary with `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, which Anthropic documents for CI and scripts on Pro/Max plans. `AskUserQuestion` is denied for every role.
- **Least privilege by stage, image and network.** Each pod runs four containers in order:

| Stage | Image | Holds | Does |
|---|---|---|---|
| `prepare` | core | role's GitHub App key | mint a 1-hour token for the one repo, claim, clone, write `task.json` (read-only afterwards), strip credentials |
| `agent` | role's stack/toolset | its engine's model credential | one bounded session; writes only to the workspace |
| `verify` | role's stack/toolset | nothing | commit leftovers as the role's bot, reject forbidden paths, run the test gate |
| `publish` | core | role's GitHub App key | validate outputs, push from a fresh clean repo, do every GitHub write |

  Model-written code only runs where there is no GitHub credential; the containers holding one carry no compiler or browser. Egress is a Cilium policy **per role** (e.g. the planner reaches GitHub and Anthropic only; the implementer adds NuGet).
- **Repo-locked.** Every GitHub call uses an explicit `repos/<repo>/…` path and the client refuses any other, which matters in forks where `gh` could otherwise resolve to the upstream.
- **Trusted config lives outside the target repo.** Roles, review cap, merge mode, gates, images and egress come from the chart values in the GitOps repo; a PR cannot change them.

## Images

Three layers, each built on the previous one, all tagged `<version>-<variant>`:

| Layer | Built from | Contents | Used by |
|---|---|---|---|
| `core` (~545 MB) | `images/core/Dockerfile` (two-stage; mise only in the builder) | Python 3.14, git, Claude Code native binary, openssl, bubblewrap, `agent-tick` | prepare/publish always; planner, designer, merger |
| stack, e.g. `dotnet10` (~1.3 GB) | `images/stacks/<name>/.devcontainer/` via the devcontainer CLI | core + toolchain via devcontainer features | implementer, fixer, reviewers |
| toolset, e.g. `tester-dotnet10` (~2.5 GB) | `images/toolsets/<name>/.devcontainer/` | stack + role-only tools (Playwright browsers) | only the role that needs them (`review-e2e`) |

Each stack/toolset devcontainer also opens as the human dev environment, so people and agents share a toolchain. The target repo's own dependencies (NuGet, crates, npm) restore at runtime; a new *tool* means a one-line change to the stack and a rebuild.

## Layout

| Path | What |
|---|---|
| `agent_tick/` | the deterministic wrapper (Python 3.14, stdlib only) |
| `agent_tick/engines/` | harness adapters (`claude`); add one module per harness |
| `prompts/<role>.md` | role instructions appended to the harness's system prompt |
| `mcp/<name>.json` | per-role MCP allowlists (`--strict-mcp-config`) |
| `images/` | `core/`, `stacks/<name>/`, `toolsets/<name>/`, `VERSION` |
| `chart/agent-pipeline/` | Helm chart: one CronJob and one egress policy per enabled role, ConfigMap |
| `scripts/provision_apps.py` | creates one GitHub App per role (manifest flow) and writes SOPS secrets |
| `examples/` | pilot values and a local `pipeline.toml` |
| `docs/onboarding.md` | adding a new repository |

## Develop

```bash
python3 -m pytest -q          # wrapper + chart render tests (needs helm)
docker build -f images/core/Dockerfile -t agent-runner:dev-core .
AGENT_CORE_IMAGE=agent-runner:dev-core npx -y @devcontainers/cli@0.80.1 build --workspace-folder images/stacks/dotnet10 --image-name agent-runner:dev-dotnet10
```

A dry run against a real repo (reads GitHub, prints intended writes, makes none):

```bash
docker run --rm -e GH_TOKEN -e AGENT_DRY_RUN=1 -v "$PWD/examples/pipeline.toml:/etc/agent/pipeline.toml:ro" agent-runner:dev-core prepare --role planner
```

## Labels

`ai-ready` (switch: remove to pause, its PR pauses too), `ai-planned`, `ai-plan-approved` (owner only), `needs-design`, `ai-designed`, `ai-needs-input`, `ai-pr-opened`, `ai-fix-needed`, `ai-awaiting-merge`, `ai-merged`, `ai-blocked`. Only issues authored by the owner, whose `ai-ready` (and `ai-plan-approved`) label the owner applied, are picked up.
