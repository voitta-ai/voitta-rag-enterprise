# Contributing to Voitta RAG Enterprise

Thanks for your interest. A few things before you open a PR.

## Contributor License Agreement

`Voitta RAG Enterprise` is [dual-licensed](./LICENSING.md) — AGPL-3.0 for the
public, commercial license for customers who can't or won't take on AGPL
obligations. For dual licensing to work, the project needs the right to
relicense every line of code.

**Every external contributor must sign the CLA before their first PR is
merged.** The CLA is enforced automatically via
[CLA Assistant](https://cla-assistant.io/) on first PR — it'll comment with
a link, you click "Sign", and the bot records consent. One-time per
contributor.

The CLA grants Voitta AI:

- a perpetual, irrevocable copyright license to your contribution;
- the right to relicense your contribution under future license terms
  (specifically: the right to ship it under a commercial license alongside
  AGPL-3).

You retain copyright of your own contribution. You can use it however you
want elsewhere. The CLA is **not** a copyright assignment.

If you can't sign the CLA (e.g. employer policy), open an issue first and
we'll discuss alternatives.

## Code

- Add `# SPDX-License-Identifier: AGPL-3.0-or-later` to new source files.
- Match existing style: `make lint` and `make test` should both pass.
- Keep PRs focused. One concern per PR; bundle later if needed.
- Don't introduce new third-party dependencies without flagging the license
  in the PR description — anything GPL-incompatible is a no-go for the
  commercial license track.

## Reporting security issues

Don't open a public issue for security bugs. Email **support@voitta.ai**
with the details and we'll respond within 72 hours.
