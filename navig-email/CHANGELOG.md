# Changelog — navig-email

## 0.4.0 — unreleased

### Fixed
- **The briefing read transactional mail.** Found on a real mailbox: password resets and account
  notices were picked as "newsletters" because any machine-sent mail qualified. A source must now
  carry a list header (`List-Unsubscribe` / `List-Id`), must not touch security, account recovery,
  payments or other protected subjects, and may be limited with `mailroom.brief.sources`. Senders a
  parser understands (Product Hunt, Free-Work, GitHub, event platforms) are read first.
- **Over IMAP, list and authentication headers were invisible before a body was read.** The IMAP
  backend's metadata fetch now returns the headers the mailroom decides on (`List-*`,
  `Auto-Submitted`, `Precedence`, `Authentication-Results`, `Reply-To`, …), as Gmail's API does.
- `policy list` pointed at `policy test <id> <message>`; the command is `policy test <message-id> --space …`.

### Added
- **The outbox: every message the mailroom sends is one recorded job.** `navig email outbox`
  lists, shows, approves, edits, delays, cancels and sends queued replies. A job moves through
  `DRAFT → PENDING_APPROVAL → SCHEDULED → SENDING → SENT` (or `CANCELLED`, `FAILED`,
  `NEEDS_REVIEW`, `OUTCOME_UNKNOWN`), each change recorded with who made it. Approving binds the
  exact text you reviewed; any edit drops the approval. The default wait before sending is
  15 minutes, so there is time to cancel. Scheduled jobs are sent by the navig daemon's mailroom
  tick while the computer is online; a job more than 24 h late goes back to you.
- **`reply --queue`** puts a generated reply in the outbox (from the alias the mail was addressed
  to, signed by that identity) instead of Gmail Drafts; `--yes --send-in 15m` approves it too.
  **`send --schedule 30m`** queues a message instead of sending it now.
- **No blind resends.** Each job carries its own `Message-ID`. When the provider's answer is lost
  (a timeout after the request left), the job is looked up in Sent before anything else happens;
  not found after 30 minutes → `NEEDS_REVIEW`, never an automatic retry.
- **A quality gate on generated replies:** empty or one-word output, unfilled placeholders, a
  header or "as an AI" leak, the wrong language, or another identity's signature is rejected and
  regenerated once (optionally with `mailroom.ai.fallback_model`); unexplained numbers, money or
  promises are flagged. A reply that still fails is not saved.
- **Emergency pause:** `outbox pause` (automatic replies) / `outbox pause --all` / `resume`.
  Queued jobs stay visible and cancellable while paused.
- Notifications for queued, changed, cancelled, sent, failed and uncertain sends (new notify types
  `mail_outbox`, `mail_sent`, `mail_send_problem`) — "sent" is only said once the provider
  confirmed it or the message was found in Sent.

- **Auto-reply policies** (`navig email policy list | show | enable | disable | test`). Six
  templates ship, all **off**: business inquiry acknowledgment, support acknowledgment, missing
  information request, trusted repetitive correspondence (named senders + approved facts only),
  routine confirmation, and drafts-only. `enable` scopes one to an account (and optionally
  aliases and senders); a sending policy needs `--yes`. The 5-minute `watch` evaluates new mail:
  machine mail (lists, bounces, auto-replies, other assistants, our own) is ignored; contracts,
  payments, bank details, job offers, government mail, account recovery, credentials, security
  incidents, sensitive attachments and irreversible requests are **never** answered automatically
  (EN/FR/RU) — a reply is drafted for review instead; so is mail whose sender's domain did not
  authenticate (DKIM/SPF/DMARC), a weak category match, or a reply that is not spotless. Per
  thread, per sender and daily limits apply. An approved automatic reply waits its delay
  (15 min) and stays cancellable; disabling the policy stops replies it already queued.

- **The newsletter briefing** (`navig email brief run | items | save | dismiss | extract | restore |
  schedule`). Not a summary: each newsletter is taken apart into products, events, jobs and
  articles — Product Hunt picks with their category and platform, an event's exact date, hours,
  venue, address and time zone (labelled when inferred), every job's annual salary and day rate
  kept apart with its own link. The same item from several emails becomes one entry with every
  source kept; anything an earlier briefing delivered is not repeated. Relevance is explained
  from the interests in `mailroom.brief`. Weekly on Monday 08:30 Europe/Paris by default (daily
  to monthly, adaptive, or manual), a thin week is postponed (at most 14 days), quiet hours are
  respected, and an event or registration deadline within 3 days is announced on its own. Event
  buttons open the registration page, a map, or a pre-filled calendar form — nothing is booked.
  Parsing is deterministic; a configured local model is used only for newsletters no parser
  understands, and anything it returns that the email does not contain is dropped. Results are
  cached per message content.
- **Newsletter clean-up after a verified delivery only:** sources whose every item was delivered
  get `Mailroom/Digested` and leave the inbox; invoices, contracts, government, finance,
  security, attachments, personal mail and saved items are never touched. `cleanup.trash_senders`
  opts a sender into Trash after `trash_after_days` (7) — recoverable, `brief restore` brings it
  back; nothing is ever deleted permanently. A failed delivery cleans nothing up.
- `brief schedule --apply` moves the weekly template digest job to the briefing (the old job is
  disabled, not removed) so Monday brings one digest, not two.

- **Telegram controls.** Outbox notifications carry buttons for what the job's state allows —
  View, Send now (asks for a confirming tap), Delay 1 h, Cancel, Approve (only the exact text
  that notification showed), Pause automatic replies — and briefing items carry Save / Dismiss
  next to their links. A button holds only a random token; what it does lives in
  `mailroom.db`, expires (24 h for sending actions), works once for anything that sends,
  approves or cancels (a double tap answers "already done"), and every press is audited.
  Needs a navig with plugin Telegram buttons; older ones get link buttons only.

- **Labels and filters** (`navig email organize audit | preview | apply | rollback | retire`). The
  audit lists every label with its message count, equivalent labels spelled differently, unused
  ones, and filters that conflict, repeat each other, forward mail, or point at a missing label.
  The preview proposes a taxonomy from this mailbox only (the space's identities and actual
  sender patterns; a target with no mail behind it is never proposed). `apply` is additive and
  writes a snapshot first; `rollback` undoes it; `retire` takes old labels off migrated mail
  after you checked. System labels and forwarding are never changed. Reading and creating
  filters needs `navig config set connectors.gmail.filters true` and a Gmail reconnect.
- **`navig email attachments save <message> --space …`** puts an email's documents (PDF, office
  files, images; ≤ 25 MB; same bytes never twice) into the space's `inbox/`, where navig
  paperwork already classifies and files them; provenance goes to `mailroom/ledger/attachments.jsonl`
  and that email is never cleaned up by the briefing.

- **The Mailroom in NAVIG OS** (Email → Mailroom) now has sections: Overview (as before),
  Outbox (what needs you, scheduled sends with a countdown, drafts, recently sent — approve the
  text on screen, send now with a second click, delay, cancel, reopen; the pause switch),
  Briefing (recent runs and their delivery state, items with Save / Dismiss and why they were
  picked), Automation (policies on/off per account; a sending one asks for confirmation),
  Accounts (connection, aliases, mailroom on/off per account) and History (the audit trail).
  Backed by new `/api/deck/email/mailroom/*` routes that take named actions only; list
  responses never carry message bodies.

### Changed
- `send --yes` now goes through the outbox, so every send is recorded and checked the same way.
  `--from` may name an alias of the account when `--space` shows it belongs to that account.
- Reply drafts are threaded with `In-Reply-To`/`References` and answer the sender's `Reply-To`.
- The thread is passed to the model as fenced data with an instruction never to follow
  instructions inside it.
- Requires `navig-sdk>=2.17.0` (its `BaseStore`).

## 0.3.0 — 2026-09-29

(0.2.0 was never published; its changes ship in this release.)

### Added
- **Runs on its own.** `pip install navig-email` now gives a `navig-email` command with no navig
  installed: the same Gmail mailroom navig mounts as `navig email`. Gmail comes through its
  own IMAP backend with an **app password** (Google Account → Security → App passwords), kept
  in the shared encrypted vault: `navig-email imap connect you@gmail.com`. Gmail's IMAP speaks
  Gmail — the same search syntax (`X-GM-RAW`), labels (`X-GM-LABELS`) and message/thread ids —
  so list, search, read, labels, tag, rules, watch, stats, replied and send all work unchanged;
  sending uses SMTP with the same app password. Inside navig, a linked OAuth Gmail is still used
  first, and a navig user can now choose an app password instead of OAuth.
- `imap connect | status | disconnect`. `connect` signs in BEFORE saving anything, so a wrong
  password is never stored; `status` never shows the password.

### Changed
- AI summaries (`digest --llm`) and follow-up drafts still go through navig's privacy guard
  (it refuses cloud models for private mail unless allowed) and say they need navig on their
  own — they never fall back to "whatever AI is configured" with your mail. Telegram alerts
  from `watch` need navig's bot; on their own the alerts are kept for a pass that can send them.
- Hints name the command that exists where you are (`navig-email …` on its own).

### Fixed
- Without navig, a space's `config.yaml` and `rules.yaml` were read through navig inside a broad
  `except` — silently empty. They are read on their own now.

## 0.1.1 — 2026-09-04

### Fixed
- **Requires `navig>=3.25.0`.** 0.1.0 declared `navig>=3.24.0`, and the published navig 3.24.0
  does not contain modules this package imports — `core/pyproject.toml` had said 3.24.0 for 541
  commits, so the source and the wheel answering to that number were different code. Installing
  with the navig it asked for produced `ModuleNotFoundError`: immediately if the import was at
  module scope, otherwise the moment the feature ran. navig 3.25.0 contains them, and
  `scripts/check_plugin_core_floor.py` now checks every declared floor against the file list of
  the wheel it actually selects.

## 0.1.0 — 2026-09-01

First changelog entry. This package existed before this file did, so earlier
history lives in the monorepo's git log rather than being reconstructed here —
an invented history would be worse than an honest starting point.

### Packaging
- Declares its dependency on `navig` (or deliberately does not, if it runs
  standalone) and ships the full licence text in the wheel.
