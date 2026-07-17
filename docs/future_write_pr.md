# Future Write Design

The MVP has no write endpoint, tool, queue, worker, diff generator, GitHub App
credential, branch logic, or dormant implementation.

A future separately reviewed service may validate a structured finding against a
pinned base commit, render a deterministic patch preview, require explicit
confirmation, and propose a pull request through a narrowly installed GitHub App.
Human review and the normal repository checks remain mandatory. The served snapshot
is never mutated.
