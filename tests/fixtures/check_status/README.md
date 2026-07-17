# `check_status` fixtures

`green_*` records the public GitHub REST responses for engine PR ref 6 and Actions
run `29384925309` on 2026-07-15. `red_*` records PR 7 and run
`29385174905`, including PR ref 7, the deliberate dual-job regression. The captures retain the
upstream envelope and every field consumed by the gateway while omitting
unconsumed public account metadata. `missing_*` is the required synthetic case
derived from the green capture with the `assets` job and artifact removed.
`master_*` records branch-head resolution and its green push run so the default
branch path is exercised without a synthetic SHA substitution.

No credential, request header, or private response field is present. Tests serve
these files through `httpx.MockTransport`; the suite never contacts GitHub.
