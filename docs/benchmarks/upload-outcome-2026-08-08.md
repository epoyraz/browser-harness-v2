# Upload outcome contract experiment

## Trigger

Five of six form commands from the router ablation treated `upload_file()` like the other
form helpers and read `.ok`, `.observed`, `.failures`, or `.to_json()`. The helper actually
returned a plain mapping, so those otherwise reasonable commands failed at runtime.

## Smallest compatible change

`upload_file()` now returns a mapping-compatible Outcome. Existing indexing, `.get()`,
equality, `json.dumps()`, copying, deep copying, and pickling remain valid. The same object
also exposes the standard Outcome views. A definite `accept` rejection is now
`value_rejected`, including the partially attached multi-file case.

The cost is 52 net runtime lines and 25 bytes in the always-loaded skill. No new process,
service, dependency, decision tree, or browser instance was added.

## Result

| Check | Result |
|---|---:|
| Fresh `gpt-5.6-terra`, medium contexts | 6/6 contract-correct |
| Exact generated snippets replayed unchanged | 6/6 passed |
| Real Chrome instances during replay | 1 |
| Applications submitted | 0 |
| Real-Chrome skill examples | 14/14 passed |
| Focused unit tests | 54 passed |
| Full unit suite | 314 passed |
| Ruff | passed |

The six fresh trials used 106,884 input tokens, 58,368 cached input tokens, and 1,511 output
tokens. This experiment measures executable return handling, not token improvement. Its
impact is avoiding the corrective model/tool decision that followed the common `.ok`
`AttributeError`; it does not make browser primitives faster.

Decision: **keep**. The pre-change API rejected the pattern used by five of six existing
form generations; the compatible adapter makes all six fresh instances executable without
breaking the old mapping surface.

## Limitation

When a page handler clears an otherwise accepted file input immediately, the harness still
reports `consumed_or_rejected`: browser state alone cannot prove whether page code consumed
the file. The Outcome means the CDP upload operation completed and no definite accept-filter
rejection was observed; it is not proof that a server received the file.
