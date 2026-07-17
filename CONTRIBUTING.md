# Contributing

Contributions to the bounded gateway, snapshot tooling, tests, and documentation are
welcome.

1. Fork or clone the repository using a GitHub identity you are comfortable making
   public.
2. Configure Git to use GitHub's no-reply email if you do not want your personal email
   embedded in commit metadata.
3. Create a focused branch and run the full test suite against an immutable snapshot.
4. Run the privacy and secret checks described below.
5. Open a pull request describing behavior, tests, and security implications.

Do not add write tools, shell execution, mutable working-tree access, or a broader
repository allowlist as incidental changes. Those alter the security model and require
separate review.

## Local Checks

```sh
JN_TEST_SNAPSHOT_PATH=/path/to/immutable/snapshot \
  .venv/bin/python -m pytest -q

git grep -nEi '(BEGIN [A-Z ]*PRIVATE KEY|github_pat_|gh[pousr]_[A-Za-z0-9_]{20,})'
.venv/bin/python -m scripts.check_public_tree
```

Hits in explicit test fixtures or documentation must still be reviewed. The goal is
not merely a clean scanner result; every added path must be safe to publish.
