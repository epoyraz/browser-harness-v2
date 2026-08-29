---
id: ats/workday
version: 2026.08.29
description: Workday: account (32/32 employers)
match:
  - host: "*.myworkdayjobs.com"
  - host: "*.myworkdaysite.com"
---

# Workday

Observed on 35 employers in the joblens top-500 map (2026-08-29): {'account': 32}. Typical flow: **account**. The application page paints only in a visible tab — activate it.

```json
{
 "apply": {
  "mode": "account",
  "ats": "Workday",
  "mode_confidence": 1.0,
  "companies_observed": 35,
  "renders_hidden": false
 }
}
```
