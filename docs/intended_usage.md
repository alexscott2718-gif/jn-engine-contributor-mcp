# Intended Usage

## Purpose

JN Engine Contributor MCP gives an AI client a narrow, inspectable view of the project
without granting that client a shell, a mutable checkout, or broad GitHub access. It
solves two related problems:

1. A contributor needs grounded JN Engine source, recovered documentation, task data,
   symbols, and CI status inside an AI conversation.
2. A contributor or maintainer needs grounded access to the MCP implementation itself,
   even when ChatGPT or Claude's account-level GitHub connection is not exposed as a
   callable tool in that conversation.

The gateway is a context and verification service. It is not a remote development
machine, deployment control plane, general GitHub proxy, or autonomous pull-request
writer.

## The Two MCP Profiles

### Engine profile

The engine profile serves an immutable snapshot of the JN Engine repository and
registers six tools:

- `search`: locate relevant source or documentation and return commit-bound IDs.
- `fetch`: retrieve bounded text for an ID returned by the current snapshot.
- `list_tasks`: inspect committed task records using structured filters.
- `project_context`: obtain a bounded orientation bundle for a new task.
- `lookup_symbol`: find recovered names, addresses, classes, and FourCC values.
- `check_status`: verify required `core` and `assets` GitHub Actions contexts for a
  branch, pull request, or exact commit.

Use this profile when implementing or reviewing JN Engine changes. A strong workflow
is: request project context, run a fresh search, fetch only the relevant files, perform
the work in a local checkout, then call `check_status` before considering the change
ready. Search again whenever fetch reports that the snapshot changed.

### Source profile

The source profile serves an immutable snapshot of this public repository through:

- `repository_context`: bounded architecture and usage orientation.
- `search`: literal search over application source, tests, docs, and deployment
  examples.
- `fetch`: bounded retrieval using source-profile `jng1_` IDs.

Use this profile when developing the gateway, reviewing authentication or snapshot
logic, or asking a web/app AI client to explain or modify the MCP implementation. Its
IDs and corpus are deliberately separate from the engine profile.

## Contributor Workflow

Each contributor uses their own GitHub identity and their own AI-platform account.
Nobody should distribute a shared bearer token, OAuth browser session, client secret,
or signing key.

1. The operator publishes the MCP endpoint and its public onboarding instructions.
2. The contributor adds that endpoint as a custom connector/app and completes GitHub
   authorization.
3. The service checks current collaboration on the fixed JN Engine repository. GitHub
   sign-in alone does not grant access.
4. The contributor asks the AI client to use the MCP tools for source-grounded work.
5. Code changes happen in the contributor's own checkout or coding workspace.
6. The contributor submits a normal pull request. Human review and repository checks
   remain authoritative.
7. An operator promotes reviewed commits into new immutable snapshots. Existing IDs
   then expire, preventing accidental cross-version fetches.

Contributors who only want to develop the public gateway can run `AUTH_MODE=authless_local`
on loopback. That mode requires no production secrets and cannot bind to a non-loopback
address.

## ChatGPT Web/App

For a deployed HTTPS endpoint:

1. Enable the current developer/custom-app capability available to the account or
   workspace.
2. Create an MCP app using the operator-provided `/mcp` URL.
3. Choose OAuth when prompted, scan the advertised tools, and complete GitHub login.
4. Select the app in a conversation and verify the expected six engine tools or three
   source tools.

Create the engine and source profiles as separate apps because they have different
OAuth resource audiences and content-ID namespaces. If the operator adds or changes
tools, refresh or recreate the app's reviewed tool definitions as required by the
client UI.

## Claude Web/App

For a deployed HTTPS endpoint:

1. Open connector settings and add a custom connector.
2. Enter the operator-provided `/mcp` URL. Leave client credentials blank when the
   server's Dynamic Client Registration flow is being used.
3. Complete GitHub login and the MCP consent screen.
4. Add the connector to the conversation and verify its tool list.

As with ChatGPT, register the engine and source profiles separately. An account-level
GitHub integration is not a substitute for this MCP: the integration may support
repository attachment or synchronization without exposing live repository tools to a
specific conversation.

## Coding Agents and Local Clients

An MCP-capable editor or command-line agent can connect to the same deployed URL. For
local development, point it to `http://127.0.0.1:8788/mcp` while the server runs in
`authless_local` mode. Do not expose authless mode through a tunnel, reverse proxy,
container host wildcard, or LAN address.

The MCP provides evidence; the coding agent still needs a separate local checkout to
edit, build, and commit code. This separation is intentional. A compromised or confused
chat client cannot mutate the snapshot or invoke a shell through this service.

## Operator Workflow

An operator is responsible for:

- creating a GitHub OAuth application for each public MCP origin;
- placing credentials in mode-0600 files under a mode-0700 directory;
- granting the collaborator-check token only the minimum repository metadata access;
- using a separate least-privilege credential for live Actions inspection;
- building snapshots from the fixed public repository/ref;
- mounting snapshots read-only and retaining audit data outside the container;
- terminating TLS and applying suitable edge limits without publishing internal
  infrastructure details; and
- reviewing dependency, secret-scan, and test results before promotion.

The operator should publish only the connector URL, tool descriptions, authentication
expectations, and support/security route. Host paths, tunnel identifiers, LAN addresses,
credential locations, and incident records remain private.

## Privacy and Pseudonymous Contribution

This repository avoids publishing a maintainer's personal email, absolute home path,
device names, or private network topology. Contributors should make their own privacy
choices before participating:

- use GitHub's no-reply commit email;
- remove personal name, location, employer, avatar, and links from a project identity
  if pseudonymity is important;
- inspect author and committer metadata before pushing;
- avoid screenshots containing desktop paths, account names, notifications, or browser
  profiles; and
- never paste credentials, private logs, or personal correspondence into issues or AI
  conversations.

This reduces public doxing risk but does not provide anonymity from GitHub, the AI
provider, the network operator, or legal process. Contributors should not be promised
otherwise.

## Explicit Non-Goals

The public gateway does not:

- expose a private vault or operator filesystem;
- provide arbitrary repository selection;
- execute Git, shell commands, builds, or game binaries;
- write branches, issues, releases, or pull requests;
- distribute shared authentication credentials;
- validate subjective visual/audio parity that requires original hardware; or
- make private identities anonymous to infrastructure providers.

Future write capabilities, if ever introduced, belong in a separately reviewed service
with explicit confirmation, narrowly scoped GitHub App permissions, deterministic patch
previews, replay protection, and mandatory human review.
