---
name: bedtime-reader
description: Read a text aloud like a book, for listening in bed. Use when the user wants a text, story, article or chapter read out loud to them — "read this to me", "read me a story", "read it like a book", "I want to listen to this" — or wants something to listen to before sleep. It turns the text into quiet ten-minute audio parts, delivered as they are made.
activation_keywords: ["read to me", "read this to me", "read me", "read aloud", "read it out", "read it like a book", "bedtime", "audiobook"]
# Trigger vocabulary for an owner who writes Russian or French (see
# core/navig/agent/skills_context.py for the matching rules). Precision over recall:
#  - NOT a bare `прочитай` / `почитай` ("read the file" / "go read about X" are not read-aloud),
#    only the "…мне" forms and `вслух` ("aloud"), which are the tell;
#  - NOT `озвуч…` — that is a voiceover FILE and belongs to audio-generation;
#  - NOT "before sleep" phrases — they would fire on every sleep-related reminder.
activation_keywords_ru: ["почитай мне", "прочитай мне", "прочти мне", вслух, "как книгу", аудиокниг]
activation_keywords_fr: ["lis moi", "a voix haute", "livre audio", "comme un livre"]
metadata:
  version: 1.0.0
  toolsAllowed:
    - read_aloud
    - bash_exec
---

# Bedtime reader — a text read to you, like a book

You are in bed, you send a text, and it is read to you: quiet audio parts of about ten
minutes, unhurried, the first within a minute, the rest as they are made — so you can fall
asleep to part 1 while part 4 is still being generated.

This is provided by the **navig-audio** plugin. Reading costs one ElevenLabs credit per
character, so a chapter can be most of a month — the cost rules below matter.

## From a chat (Telegram, deck)

1. **A short text the user sent with the request** → call the `read_aloud` tool with the
   text **exactly as given**. Never shorten, summarise, translate or "tidy" it: the words
   are theirs. The tool returns at once; reply with one short line ("Reading to you now —
   about 8 minutes, 2 parts") and stop. Do not wait, and do not narrate the audio.
2. **A long text** (more than a few thousand characters — Telegram caps a message at 4096)
   → do not try to pass it through. Tell them to send it as a **`.txt` or `.md` file with
   the caption `/read`**. That is the way in for a chapter.
3. **`/read <text>`** and **`/read stop` · `/read go` · `/read status`** are handled by the
   bot itself — they need no tool call.

### Cost — the part not to get wrong

- `read_aloud` quotes before it spends. If the result says **NOT started** (a big text),
  tell the user the length, minutes and credits **in their language**, and **ask**. Only on
  a yes call `read_aloud` again with `confirmed=true` and **no text** (it is held — never
  re-send a chapter to say yes).
- If it says the account **cannot afford** it, say so with both numbers and suggest a
  shorter text. Never retry in a loop.
- Re-reading something already read is free (cached) — you do not need to warn about cost
  then; the tool says 0.
- `action='stop'` cancels a reading in progress. If the user says stop, stop — then say it
  is stopped, once.

### Choosing how it sounds

- Voice and pace come from the user's config: `navig config set audio.read.voice Marcus`
  (an id, or the name of one of their voices — `navig audio voices`),
  `audio.read.speed 0.85` (0.7–1.2, default 0.9), `audio.read.part_minutes 10`.
- A different voice for one reading: pass `voice="<name>"`. Language is detected from the
  text; pass `language="ru|fr|en"` only to override.

## From a terminal

```bash
navig audio read --file chapter-3.txt --dry-run      # length, parts, credits — spends nothing
navig audio read --file chapter-3.txt                # reads it; each part is printed as it lands
type story.txt | navig audio read -                  # from stdin
navig audio read --file book.md --voice Marcus --speed 0.85
```

Over 15,000 credits needs `--yes`. The files land in `<audio dir>/read/<id>/`
(`read-<id>-part-01-of-04.mp3` …).

## Notes

- This is for **listening to a text**. Making a voiceover *file* for a video, music, or sound
  effects is `audio-generation`; turning speech into text is `navig voice`.
- Text files may be UTF-8, UTF-16 or an old Windows code page (cp1251/cp1252) — they are
  decoded correctly; do not ask the user to convert them.
- Never claim it is "done" while parts are still arriving: the reading ends when the chat
  says "That's all".
