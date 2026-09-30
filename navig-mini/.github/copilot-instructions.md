---
applyTo: '**'
---

# navig-mini — Lightweight NAVIG Build

Minimal, zero-bloat NAVIG variant optimized for fast startup and small bundle size.

## Stack

- TypeScript strict
- Minimal dependencies — justify every addition
- Shared types from `navig-shared`

## Critical Rules

- Bundle size is a hard constraint — run `vite build --report` after every dependency change
- No barrel imports that pull in transitive heavy modules
- Lazy-load anything >10KB
- `tsc --noEmit` must exit 0
- Feature parity with core UX for supported features
