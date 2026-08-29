---
id: company/wd502-myworkdaysite-com
version: 2026.08.29
description: Medbase: Account required (login wall)
match:
  - host: "wd502.myworkdaysite.com"
---

# Medbase

Account required (login wall) via Workday (chain: [start] wd502.myworkdaysite.com/de-CH/recruiting/medbase/Medbase_jobs/j -> [href] wd502.myworkdaysite.com/de-CH/recruiting/medbase/Medbase_jobs/j -> [href] wd502.myworkdaysite.com/de-CH/recruiting/med).

```json
{
 "apply": {
  "mode": "account",
  "ats": "Workday",
  "company": "Medbase",
  "renders_hidden": false
 }
}
```
