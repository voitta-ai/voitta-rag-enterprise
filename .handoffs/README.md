# `.handoffs/`

Project context handoffs between sessions / agents / coworkers.
**Checked in** — these are allowed to live in git in this repo
(unlike most temporary work-state files).

## Convention

Every handoff in this folder MUST carry a `**Delete when:**`
marker in its frontmatter, near the `Status:` line. The marker
states the concrete condition that retires the document.

```markdown
**Delete when:** <specific, verifiable condition that ends the
handoff's usefulness — typically pointing at the same Next Steps
or acceptance criteria the handoff describes>
```

When the condition is met, delete the handoff file in the same
commit that satisfies the condition. The doc is meant to be
ephemeral; the PR + issue + git history are the durable record.

## Naming

`YYYY-MM-DD-<slug>.md`. Slug 2-4 words, lowercase, hyphenated.
