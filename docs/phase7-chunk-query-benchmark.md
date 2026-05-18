# Phase 7 chunk query benchmark decision

`docs/current-code-speed-safety-refactor-plan.md` made the chunk query split a
benchmark-gated experiment. I tested a read-path-only split that queried `blocks`
without joining `block_payloads` and fetched inline payloads only on inline rows.

Baseline on this machine:

- `small-file-read`: 0.205914 s, `sqlite.execute`: 54.222 ms, 13,561 calls.
- `sequential-block-read`: 0.017775 s, `sqlite.execute`: 1.835 ms, 343 calls.
- `random-read`: 0.124095 s, `sqlite.execute`: 13.454 ms, 8,087 calls.
- `repeated-random-read`: 0.122892 s, `sqlite.execute`: 13.313 ms, 8,087 calls.

Experimental split:

- `small-file-read`: 0.212387 s, `sqlite.execute`: 59.638 ms, 14,061 calls.
- `sequential-block-read`: 0.016288 s, `sqlite.execute`: 1.771 ms, 343 calls.
- `random-read`: 0.123862 s, `sqlite.execute`: 12.889 ms, 8,087 calls.
- `repeated-random-read`: 0.122590 s, `sqlite.execute`: 12.702 ms, 8,087 calls.

Decision: do not land the query split now. The tiered read improvements were
small and within noise for random reads, while inline small-file reads regressed
and added extra query-path branching. Keeping the single full `chunk_block(s)`
query is simpler and avoids the dual inline/tiered path until a stronger
profile shows this join is a real bottleneck.
