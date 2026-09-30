# Changelog

## 0.2.0 — 2026-09-29

### Added
- **Runs on its own.** `pip install navig-contacts` gives a `navig-contacts` command with no
  navig installed: the same app navig mounts as `navig contacts`. The contact store moved here
  from core (`navig.store.contacts` is now an alias of `navig_contacts.store`: the same module,
  the same `contacts.db`, the same schema), and `Contact`/`Route` come from navig-sdk, so a
  contact read standalone is the object navig's message routing resolves.
- `--space` and importing a Telegram export need navig and say so in one line instead of a
  traceback. Hints name the command you actually typed (`navig-contacts …` on its own).

## 0.1.0 — 2026-09-07

`navig contacts` becomes one command over one store.

Core carried an alias→route table for message dispatch; nothing carried the
people themselves. Both halves now live here, on the schema core defines in
`navig.store.contacts`, because they were always the same question at two
depths: a route is a transport, an identifier is the address itself, and an
address belongs to exactly one person.

- Absorbs core's `list / add / show / remove / route / import`; core no longer
  registers the `contacts` verb and hard-depends on this package instead, so the
  command cannot go missing.
- `import` detects its input: a .vcf file, a folder of them, or a Telegram
  contacts.json / export ZIP.
- Merges on hard identifiers (phone, email, skype, telegram, facebook, vk).
  Names are never a merge key.
- Four reachability tiers; only `verified` means a phone number validated.
- **An imported identifier also becomes a route**, so `navig dispatch send`
  reaches everyone the import found.
- `merge` / `merges` / `undo` / `split` — every one logged and reversible.
- `--space` reads a space's own book; the default stays the global one.
- `migrate` brings a pre-merge space book onto the shared schema.
- Reports: merges, suspicious merges, rejected phones, assumed regions,
  interests, and everything dropped.
