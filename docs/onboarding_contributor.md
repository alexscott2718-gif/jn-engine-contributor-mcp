# Contributor Onboarding

Access is available to current collaborators on
`alexscott2718-gif/jn-engine`. GitHub login alone is not sufficient: the gateway checks
current repository collaboration before returning data.

## Claude Web

1. Open Settings, then Connectors, then Add custom connector.
2. Name it `JN Engine AI Gateway`.
3. Set the URL to `https://mcp.example.org/mcp`.
4. Leave OAuth Client ID and OAuth Client Secret blank.
5. Complete GitHub sign-in and consent.

The server supports Dynamic Client Registration. A client callback must match the
deployment allowlist exactly; only loopback callbacks may use a wildcard port.

## ChatGPT Web

1. Confirm that your GitHub account is a current JN Engine repository collaborator.
2. Enable Developer mode for your eligible ChatGPT web account or workspace.
3. In Settings, then Apps, create a custom app with
   `https://mcp.example.org/mcp` as its endpoint.
4. Choose OAuth, scan the tools, and complete GitHub authorization.
5. Select the app from the chat tools menu and verify that all nine tools are available.

The ChatGPT app uses the same GitHub OAuth and fail-closed repository collaborator
check as Claude. No shared gateway credential is distributed to either client.
If a newly deployed tool is absent, refresh the app's tool actions or recreate and
rescan the draft app. ChatGPT retains a reviewed snapshot of tool definitions rather
than automatically applying server-side changes. See OpenAI's current
[developer-mode guidance](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt).

## Live Tool Surface

After connection, `tools/list` contains these nine live tools:

~~~text
search
fetch
list_tasks
claim_task
release_task
project_context
lookup_symbol
check_status
open_pr
~~~

`check_status` accepts exactly one of `pr` or `branch`, plus an optional full commit
SHA. It reads the required `core` and `assets` GitHub Actions contexts for the fixed
JN Engine repository. Missing contexts block the result; the tool never treats
missing status as success. Every invocation writes a durable audit record without
serializing the inbound bearer or outbound GitHub token.

The six read tools, three audited write tools, and five protected REST routes use the
same contributor principal. Advanced clients may reuse their gateway bearer in an
`Authorization: Bearer` header for REST; never put a bearer in a URL or send it to
another person. The public `/health` response reports the active snapshot commit,
and search/fetch/symbol metadata includes commit-pinned repository URLs.

The currently deployed service exposes only the policy-filtered JN Engine repository
snapshot. It does not expose the operator's private vault, infrastructure notes,
working checkout, GitHub Issues, or uncommitted reverse-engineering material.

Access kits contain only the public URL and these instructions. They never contain
OAuth client secrets, collaborator tokens, signing keys, enrollment secrets, or
bearer tokens.

Device mode is an operator-selected headless fallback. It is not enabled alongside
GitHub mode and is not offered as a silent fallback during a GitHub outage.

## Offboarding

Removing repository collaboration revokes new checks immediately and cached
positive decisions within at most five minutes. Existing OAuth login alone does not
override that check. Remove the collaborator in GitHub, wait at least 300 seconds,
and verify that a protected MCP call is denied.
