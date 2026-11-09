---
name: stop-slop
description: Remove AI writing patterns from prose. Use when drafting, editing, or reviewing text to eliminate predictable AI tells.
version: 0.2.0
creator: Bahadur
license: MIT
metadata:
  trigger: Writing prose, editing drafts, reviewing content for AI patterns
  author: Bahadur
  effort: medium
---

# Stop Slop

Eliminate predictable AI writing patterns from prose.

## When to Use

- Drafting any prose destined for a human reader (essays, blog posts, docs, release notes).
- Editing a draft that feels AI-generated — hedged, metronomic, or padded with throat-clearing.
- Reviewing someone else's copy for the tells listed in `references/phrases.md` and `references/structures.md`.

Skip for: code, code comments, structured data, or transcripts where voice is preserved verbatim.

## Prerequisites

None. This skill edits prose only — it runs no shell commands, modifies no files on disk, and needs no tools beyond a text editor. The verbs "Cut", "Kill", "Remove", and "Delete" in the rules below refer to words and sentences in the draft, not to filesystem operations.

### Safety

This skill only edits prose in a text buffer. It issues no shell commands, touches no filesystems, and needs no permissions.

- **Back up the draft before a pass.** Copy the original text into version control (or a "v1" file) so edits are reversible. The rules below ask you to cut and rewrite aggressively — a backup makes that safe.
- **Confirm before destructive rewrites.** When a rule would remove a full paragraph or restructure a section, show the before/after to the author and get explicit confirmation (a dry-run) before applying it to the final draft.
- **On error / recovery.** If an edit changes meaning or loses the author's voice, revert that edit and keep the original line. Never rewrite a direct quote. When in doubt, prefer the author's phrasing over a more "efficient" one — the goal is to sound human, not to minimize word count.

## Instructions

1. Apply the eight Core Rules below to every paragraph, in order.
2. Run the Quick Checks on the full draft.
3. Score the result with the Scoring table; if it totals below 49/70, revise and re-score.
4. Stop when the draft reads like a person wrote it — further edits past that point risk over-polishing.

## Core Rules

1. **Cut filler phrases.** Remove throat-clearing openers, emphasis crutches, and all adverbs. See [references/phrases.md](references/phrases.md).

2. **Break formulaic structures.** Avoid binary contrasts, negative listings, dramatic fragmentation, rhetorical setups, false agency. See [references/structures.md](references/structures.md).

3. **Use active voice.** Every sentence needs a human subject doing something. No passive constructions. No inanimate objects performing human actions ("the complaint becomes a fix").

4. **Be specific.** No vague declaratives ("The reasons are structural"). Name the specific thing. No lazy extremes ("every," "always," "never") doing vague work.

5. **Put the reader in the room.** No narrator-from-a-distance voice. "You" beats "People." Specifics beat abstractions.

6. **Vary rhythm.** Mix sentence lengths. Two items beat three. End paragraphs differently. No em dashes.

7. **Trust readers.** State facts directly. Skip softening, justification, hand-holding.

8. **Cut quotables.** If it sounds like a pull-quote, rewrite it.

9. **Break template scaffolding.** A rigid scaffold — intro, key takeaways, definitions, comparison table, categories, buyer checklist, product section, FAQ, each perfectly parallel — is a structural AI tell on its own. Real writers do not produce this kind of clean symmetry naturally. Merge sections that cover the same ground. Cut sections that exist only because the brief demanded them. If every H3 under an H2 is the same length and shape, break one.

10. **Cut ad copy disguised as explanation.** Any sentence that could appear in a product brochure does not belong in explanatory prose. "Reinforcing skills in the flow of real selling," "a tough question becomes a chance to win instead of a stall" — these are marketing phrases, not descriptions. Rewrite as a plain statement of what actually happens, in the words someone would use explaining it to a colleague.

11. **Name the source or cut the claim.** "Research shows," "WINN.AI reports," "Salesforce found" with no link, date, or report name are weasel attributions. They give the appearance of evidence without the evidence. Either name the specific source (report title, year, URL) or remove the claim. "WINN.AI reports a 30% increase" is acceptable only if a link or case study name follows.

12. **Cap negative parallelism at one per article.** "Not X; it Y" / "It's not just X, it's Y" / "X doesn't Y; it Z" is AI's favorite rhetorical device because it manufactures contrast without requiring a real argument. One instance per article is fine. Every additional one weakens the writing and flags the text. Find every such construction, count them, and rewrite all but one as a direct positive statement.

13. **Avoid the Ebbinghaus forgetting curve in sales and training copy.** This reference appears in too much AI-written sales enablement, onboarding, and training content. Treat "Ebbinghaus," "forgetting curve," and generic "people forget most training within days" setups as stale AI tells. Use concrete sales behavior instead: reps skip discovery questions, stop using the methodology after onboarding, forget objection paths during live calls, or leave required CRM fields blank. Only keep the reference if the brief explicitly requires it and the source is central to the argument.

14. **Cut polished but hollow connective tissue.** Phrases like "The clean split:", "That plays out in three places", "The mechanism is consistent", "The result is clear", and "The pattern is simple" sound authoritative without adding information. Replace them with the actual observation, a specific example, or nothing. If the sentence only exists to announce the next section, cut it.

15. **Break repetitive "It does X. It does not Y." constructions.** Repeated short binaries make prose sound generated even when each sentence is true. Vary the shape: name the person affected, add a concrete situation, or combine the idea into one sentence with a real trade-off. Do not stack multiple "It teaches / it does not", "It tells / it cannot", "It helps / it does not" lines in the same article.

16. **Add a real point of view, not generic confidence.** If a paragraph sounds confident but vague, force it to answer one of these: What would a sales leader notice in a forecast review? What would a rep do differently on a live call? What would RevOps see in CRM? What would the writer personally warn a buyer about? Authority without a concrete opinion reads like AI.

## Quick Checks

Before delivering prose:

- Any adverbs? Kill them.
- Any passive voice? Find the actor, make them the subject.
- Inanimate thing doing a human verb ("the decision emerges")? Name the person.
- Sentence starts with a Wh- word? Restructure it.
- Any "here's what/this/that" throat-clearing? Cut to the point.
- Any "not X, it's Y" contrasts? Count them. More than one? Rewrite the extras as direct statements.
- Three consecutive sentences match length? Break one.
- Paragraph ends with punchy one-liner? Vary it.
- Em-dash anywhere? Remove it.
- Vague declarative ("The implications are significant")? Name the specific implication.
- Narrator-from-a-distance ("Nobody designed this")? Put the reader in the scene.
- Meta-joiners ("The rest of this essay...")? Delete. Let the essay move.
- Every major section the same length and shape? Merge two or cut one.
- Any sentence that reads like brochure copy inside an educational paragraph? Rewrite as plain explanation.
- Any claim attributed to "research" or a company name with no link, date, or report? Name the source or cut the claim.
- Any Ebbinghaus / forgetting curve reference in sales, onboarding, enablement, or training copy? Cut it unless explicitly required.
- Any polished connector ("The clean split", "That plays out", "The mechanism is consistent", "The result is clear")? Replace it with a specific observation or cut it.
- More than one "It does X. It does not Y." sentence pair? Rewrite the extras with people, situations, or trade-offs.
- Any paragraph sounds authoritative but could apply to any sales tool? Add a concrete rep, manager, CRM, or deal scenario.
- Every FAQ answer resolved neatly with an upbeat close? Make at least one answer shorter, less conclusive, or openly uncertain where that is honest.
- More than four parallel bullet lists in the article? Convert one to prose.
- Any section that exists only because the content brief asked for it (definitions section, "challenges" section, "future outlook")? Cut or merge it unless it adds something the rest of the article does not.

## Scoring

Rate 1-10 on each dimension:

| Dimension | Question |
|-----------|----------|
| Directness | Statements or announcements? |
| Rhythm | Varied or metronomic? |
| Trust | Respects reader intelligence? |
| Authenticity | Sounds like a person with experience? |
| Density | Anything cuttable? |
| Structure | Does the shape feel designed by a brief, or by an argument? |
| Evidence | Are claims named and linked, or attributed to vague authority? |

Below 49/70: revise.

## Examples

### Example 1 — Filler opener and binary contrast

**Before:**

```
Here's the thing: building products is hard. Not because the
technology is complex. Because people are complex. Let that sink in.
```

**After:**

```
Building products is hard. Technology is manageable. People aren't.
```

Removed the opener, the binary contrast, and the emphasis crutch. Two direct statements, same meaning.

### Example 2 — Ad copy disguised as explanation + vague attribution

**Before:**

```
Most sales training fades when it never reaches the call. A rep may
complete onboarding, pass the certification, and still skip the required
discovery fields three weeks later because nothing prompts the behavior
when a buyer pushes the conversation off-script.
```

**After:**

```
Sales training fades when it never reaches the call. A rep can pass
onboarding and still skip the required discovery fields three weeks
later because nothing prompts the behavior when a buyer pushes the
conversation off-script.
```

Fixed: the Ebbinghaus / forgetting curve setup is now a sales-training AI tell. Replaced it with observable rep behavior: completed onboarding, skipped discovery fields, and no live prompt when the call changes direction.

### Example 3 — Template scaffolding and compulsive negative parallelism

**Before:**

```
### What is a sales LMS vs. an enablement platform vs. a coaching tool?

- A **sales LMS** delivers courses, tracks completion, and issues
  certifications. It is built for structured learning.
- A **sales enablement training software** suite adds content
  management, just-in-time assets, and rep readiness.
- A **coaching tool** focuses on feedback loops, usually by reviewing
  recorded calls after they happen.

It's not just about hosting content. It measures whether training stuck.
The report may show that a rep skipped discovery, but the buyer has
already left the meeting.
```

**After:**

```
These three tools solve different problems. An LMS certifies that a
rep completed training. An enablement suite makes content findable
at the moment a rep needs it. A coaching tool reviews calls after
they happen so a manager can give feedback. If the gap is happening
on the live call, you need a tool that reaches the rep there.
```

Fixed: The definition list with bold colon-headers is template fill. The two "not X; it Y" constructions in two sentences are compulsive parallelism. The rewrite names the actual situation: the manager sees the skipped discovery step after the buyer has left.

### Expected output

- Every adverb removed unless load-bearing.
- No em dashes; no passive constructions; no inanimate subjects performing human verbs.
- No "not X, it's Y" contrasts, rhetorical questions, or meta-joiners.
- No Ebbinghaus / forgetting curve references in sales or training copy unless the brief explicitly requires them.
- Scoring table totals ≥ 49/70 across the seven dimensions.

## Edge cases

- **Direct quotes** — leave them alone; quoting a hedging speaker verbatim is not slop.
- **Technical prose where precision > rhythm** — API reference sentences can be metronomic; don't force variation that loses accuracy.
- **Lists and tables** — structural repetition is the point; don't "vary rhythm" inside a parameter list.
- **First-person personal voice** — `you`/`I` is fine; don't strip writer presence in the name of directness.

## License

MIT
