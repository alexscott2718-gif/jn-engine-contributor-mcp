# Security Policy

## Reporting a Vulnerability

Do not open a public issue for a suspected vulnerability, credential exposure, or
privacy leak. Use GitHub's private vulnerability reporting feature for this repository.
Include the affected commit, impact, reproduction conditions, and any suggested
mitigation. Do not include real credentials or personal data in the report.

If private vulnerability reporting is temporarily unavailable, contact a maintainer
through a private channel already published on the maintainer's GitHub profile. This
repository intentionally does not publish a personal email address.

## Supported Version

Security fixes are applied to the current `main` branch. Operators should deploy an
immutable commit and update deliberately after reviewing release notes and test results.

## Secrets and Privacy

Never commit `.env` files, OAuth credentials, bearer tokens, signing keys, tunnel
credentials, audit logs, personal contact information, private hostnames, or absolute
operator paths. Use the example configuration files and mode-0600 secret files. If a
secret is committed, revoke it first; deleting the file in a later commit is not
sufficient.
