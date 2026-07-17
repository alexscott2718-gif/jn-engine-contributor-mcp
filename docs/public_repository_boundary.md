# Public Repository Boundary

This repository is a clean public distribution, not a history rewrite of an operator
repository. Its initial history begins with a reviewed source snapshot so old commit
emails, pull-request metadata, workflow logs, and deleted operational files cannot be
reintroduced accidentally.

## Included

- Complete REST and MCP application source.
- GitHub OAuth, device OAuth, and loopback-only local authentication modes.
- Immutable snapshot exporters and validators.
- The nine-tool engine profile and three-tool source profile.
- Unit, integration, protocol, and container-contract tests.
- Generic Docker, Compose, CI, environment, and tunnel examples.
- Contributor, deployment, security, API, and intended-usage documentation.

## Excluded

- Production `.env` and secret files.
- OAuth state, access tokens, signing keys, and tunnel credentials.
- Audit records, logs, generated access kits, caches, and build output.
- Personal email addresses, real names, machine usernames, and absolute operator paths.
- Private hostnames, LAN addresses, device names, and exact infrastructure inventory.
- Historical run reports, incident notes, private pull-request metadata, and workflow
  artifacts.
- Images or other files derived from third-party game assets.

## Information That Remains Public Deliberately

The target repository name, public GitHub URLs, source code, API contracts, security
design, and generic deployment model are necessary for review and operation. They are
not credentials. Publishing security controls supports review; deployment-specific
values and secrets remain outside the repository.

## Release Rule

Before every push, inspect the complete diff and run secret, email, absolute-path,
private-address, and high-entropy scans. A later deletion does not erase data from Git
history. Revoke a leaked secret immediately and follow GitHub's sensitive-data removal
process before continuing.
