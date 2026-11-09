---
name: isabelle-proof-development
description: Developing Isabelle/HOL proofs with the Isabelle Pool Server MCP (the isabelle_* tools of the isabelle-pool-server MCP server). Use whenever you work on proofs in a .thy file through that MCP — closing goals (quick attempts, sledgehammer, sorry skeletons), avoiding proof steps that never finish, searching for lemmas, and deciding when to run a batch build.
---

# Proof development with the Isabelle Pool Server MCP

## Two loops: locate, then iterate

- **Locate and confirm on the file.** `isabelle_open` the theory once. After each edit,
  `isabelle_diagnostic_messages` re-checks from the first changed command on (incremental) and
  tells you where the theory stands. Use it to find the problem and to confirm a fix.
- **Iterate on a step in isolation.** `isabelle_goal(file, line)` shows the goal at a step.
  `isabelle_multi_attempt(file, line, candidates, timeout)` checks the file text before `line`
  followed by each candidate, on separate sessions, each with its own time limit; the file is not
  touched. Try candidates there, write the one that works into the file, then confirm.
- **Batch build only once, at the end.** `isabelle build` re-checks the whole session (tens of
  seconds or more each time). Run it as the final acceptance check when the MCP reports no errors,
  not to test individual steps.

## Closing a goal you cannot write in one shot

1. **Quick attempts (the `try0` of this MCP).** One `isabelle_multi_attempt` with the standard
   methods as candidates, e.g. `by simp`, `by auto`, `by blast`, `by force`, `by fastforce`, adding
   `using <facts>` or `simp add: <rules>` when you know them, and `timeout` 10–15. Take the fastest
   one that succeeds.
2. **Sledgehammer.** `isabelle_sledgehammer(file, line, timeout_s=30)` when the quick attempts fail,
   or when you do not know which lemmas are needed. Paste a suggestion verbatim and confirm it.
   Among the suggestions prefer methods with good default behaviour (`simp`, `auto`, `fastforce`)
   over explicit ones (`metis`, `rule`) over `smt`, unless they are much slower.
3. **Sorry skeleton.** If sledgehammer finds nothing, the step is too big for it. Write the
   structure of the proof — the case split, the induction, intermediate `have`s, or separate lemmas —
   with `sorry` at every leaf, check that the structure is accepted, then call
   `isabelle_fill_sorries`: it runs sledgehammer on every `sorry`; write its `patched_text` to the
   file. Split the leaves that remain further and repeat. Sledgehammer rarely closes one large goal
   but often closes its small pieces.

- After two failed direct attempts on the same goal, switch to step 3.
- **Do not guess `metis` calls** (or long fact lists for any method). With the wrong facts, `metis`
  searches without end and reports no error. Sledgehammer finds such calls in seconds and only
  returns ones it has tested.
- Run only a few sledgehammers at a time; they are expensive.
- `sorry` is a placeholder for development only; the final file must not contain it if the task
  forbids it. A proof found above a remaining `sorry` can break when the `sorry` is filled: re-check
  after every round.

## Proof steps that do not finish

Measured on agents' AutoCorrode proofs: every proof step that finished took under 6 seconds (the
slowest were `crush_base` calls); the steps that held up the checks did not finish within 15
minutes. There was nothing in between. A step that runs for 10 seconds or more is very likely not
going to finish.

- A tool result with `still_running` means the file's last check stopped at its time limit (30 s)
  with these commands unfinished: line, kind and how long they have been running. A listed command
  may itself be slow or may be waiting behind another one; look at the earliest listed line first.
- Until the step is changed, every check of the file and every `isabelle_query` on it waits behind
  it (a query has waited 10 minutes this way).
- So: test any step you are not sure finishes within seconds with `isabelle_multi_attempt` first,
  instead of writing it into the file. If the same line stays in `still_running` across checks, the
  step will not finish: replace it with a tested alternative or with `sorry` before you query the file.

## Searching

- `isabelle_query(file, "find_theorems …")` and `find_consts`, at the file tip:
  - patterns: use `?x` or `_` for variables (a free `x` matches only itself), e.g.
    `find_theorems "rev (rev _) = _"`; `name: rev`; negate with `-`; limit with `find_theorems (20) …`;
  - `intro`, `elim`, `dest`, `solves` refer to the goal at the file tip;
  - `find_consts "nat ⇒ _"` searches constants by type, `strict:` for the whole type.
- These commands and sledgehammer see only transitively imported theories. For other theories,
  search their sources as text (`grep`).
- Frameworks often provide named rule collections for recurring patterns (`lemmas name = …`). Before
  writing a helper lemma about a recurring pattern (loop indices, list updates, word arithmetic),
  `grep -rn "^lemmas\|^lemma" <framework dir>` for an existing one and pass it by name.

## Rules and style

- Pass rules by name (`simp add:`, `intro:`, `crush_base simp add: …`) rather than declaring
  global `[simp]` or other attributes, which change every later proof.
- Tag your own new definitions' basic facts only if they are used often, and avoid simp loops.
- `apply` scripts are the standard style in program logics (such as AutoCorrode's `crush`);
  structured Isar helps for the mathematical side lemmas.
