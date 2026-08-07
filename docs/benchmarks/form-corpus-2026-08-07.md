# Substantial form corpus benchmark

The corpus contains the 23 application pages whose recorded dry runs filled at least four
fields. Each fixture was captured from an isolated Chrome profile, stripped of executable
site scripts and live form actions, and served from localhost. The smallest fixture has
five detected fields; the largest has 32.

## Result

| Metric | Multi-call baseline | `prepare_application()` | Impact |
|---|---:|---:|---:|
| Public helper calls | 184 | 23 | 87.5% fewer |
| CDP calls | 322 | 92 | 71.4% fewer |
| Preparation time | 29,275.82 ms | 538.27 ms | 54.39x faster |
| Schema mismatches | 0 | 0 | identical on 23/23 |
| External network requests | 0 | 0 | fully offline |

The absolute timing measures local harness overhead, not internet navigation. The speedup
comes from avoiding the fixed OOPIF discovery wait when the main document already exposes
a substantial application form. The CDP and helper-call reductions are structural.

Reproduce with `uv run python tests/bench/form_corpus.py`.
