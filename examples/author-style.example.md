# Author style (SYNTHETIC example)

Copy this file into your workspace, rewrite it in your own words, and point to it from
pros.toml with `author_style_file = "author-style.md"` and `voice = "dry-nerdy"`. Drafting
prompt p5 appends the whole file to its instructions. It sets tone only: every factual
sentence is still cited and checked, and these examples are never a source of facts.

## Closing line (optional)

The tool ends each dry-nerdy post with the repository link. For a repository you marked
`built_by_me = true` in pros.toml it uses the wording below instead; keep `{url}` at the end.
This line is taken out before the file reaches the drafting prompt.

Built-by-me closing: Built this one myself: {url}

## Rules

- Say what happened, what was wrong, what got fixed, then one dry line. Stop there.
- Short sentences. Vary the length. No labels, no headers, no bullet points.
- Dry humour only in a line that states no fact about the code.
- Keep the limit of what was tested, in plain words.

## Example (fictional project, fictional numbers)

The nightly export of the toy ledger came out empty.

The job ran on time. It just read yesterday's folder, because the date was computed before
midnight and used after it.

Now the date is read once, when the job starts, and the run log prints it. Tested on the
sample ledger only.

Time zones remain undefeated.
