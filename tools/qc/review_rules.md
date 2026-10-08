# Rules for the reviewers of proposed corrections

Use these as the instructions for each reviewer, and for each skeptic with a lens added (linguistic, evidence,
structure). Everything is read only: write no files.

You are checking proposed corrections to a Turkish TV drama subtitle. The subtitle was produced by Google
Chirp 3 from one long recognition. Reads A and B in the evidence are two independent fresh recognitions of the
same audio in short pieces, cut at different places. Edge words of a piece are already filtered out.

How to judge:

1. A change needs BOTH reads to agree with each other against the subtitle, and the new wording must make better
   sense in context than the old (grammar, plot, who is speaking, the names in the show's term sheets). Where the
   reads disagree with each other, or only one disagrees, keep the subtitle.
2. Pure variants are not worth changing: tabi/tabii, ee/eee/eh, he/ha/hı/hım/hah, aa/ah/oh, ya/yahu, punctuation,
   a missing or extra filler. Reject them with the reason "variant".
3. Wording may only come from read A or read B. Never invent wording from your own guess of what was said. When A
   and B differ in a small detail, take the form closest to the subtitle's existing style.
4. Delete a cue or words only when both reads are silent there (or read something unrelated) AND the subtitle's
   words are implausible, or the cue is a non-speech description, or an exact duplicate of a neighbour. If the
   words could be quiet real speech that the reads cannot hear, keep them and flag.
5. Keep the cue's existing capitalization and punctuation style. Do not merge or split cues. If two proposals
   touch the same cue, give ONE edit with the combined final text.
6. Timing: leave a cue's timing alone unless the evidence shows both reads place it more than 1 second away (then
   give new_start and new_end in seconds from the reads' own word times, keeping new_end after new_start, at
   least 0.8 s long when the words need it, and not overlapping the neighbouring cues), or a cue must be
   inserted as a new cue (give start and end from the reads' word times). A retime-only edit has new equal to old.
7. "old" must be the exact current text of the cue as printed in the evidence. "new" is the whole new text of the
   cue, or an empty string to delete the cue.
8. Beyond the listed proposals: if in the context you see a cue that is clearly garbled nonsense and the reads
   give one coherent reading, you may add an edit for it (with from_ids empty and the reason starting with
   "extra:"). Do not change a cue just because a read words something differently.
9. Anything you cannot settle goes in flags: the cue number, the time as h:mm:ss, and one plain sentence for the
   person who will listen to it.

Lessons from the first episode that used this: hybrids are a trap (if both reads hear one verb form, do not keep
the subtitle's different form inside a change that takes the rest from the reads); a name that comes only from
the names list and from no read is not evidence.

## Addendum for historical drama with archaic or dialect speech (added on the first episode of this kind)

10. If the subtitle uses archaic or dialect forms consistently (men / menim / meni for ben / benim / beni, deyu for
    diye, ula, kardaş, imdi), they are probably what is said. Short fresh reads, which have less context,
    normalize them to modern Turkish, and BOTH reads do it the same way, so "both reads agree" is NOT evidence
    against such a form. Reject these changes with the reason "dialect". Also reject spelling variants of a name
    that do not change the person (two transliterations of one person's name), unless a fresh read supports the
    other spelling and the term sheet or the scene fixes it. A name that comes only from the term sheet and from
    no read is not evidence.
11. Change a word only when the subtitle's word is plainly wrong (a non-word, ungrammatical, or contradicted by the
    scene) AND both reads give the same fitting replacement.

Output per file: decisions (id, apply|reject|flag, reason), edits (cue, old, new, new_start, new_end, from_ids,
confidence high|medium, reason), inserts (after_cue, start, end, text, from_ids, confidence, reason), flags.
