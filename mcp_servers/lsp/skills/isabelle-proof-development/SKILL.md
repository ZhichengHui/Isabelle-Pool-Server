---
name: isabelle-proof-development
description: Developing Isabelle/HOL proofs with the Isabelle Pool Server MCP (the isabelle_* tools of the isabelle-pool-server MCP server). Use whenever you work on proofs in a .thy file through that MCP.
---

# Proof development with the Isabelle Pool Server MCP

## Workflow

1. `isabelle_open` the theory once.
2. After each edit, check the file with `isabelle_diagnostic_messages`. Use it to find errors and to
   confirm fixes.
3. To develop a step, read its goal with `isabelle_goal(file, line)` and try candidates with
   `isabelle_multi_attempt(file, line, candidates, timeout)`. Write into the file only steps that
   succeeded there, sledgehammer suggestions, and `sorry` placeholders.
4. Run `isabelle build` once, at the end, when the MCP reports no errors. Do not use it to test steps.

## Closing a goal

Take the first of these that succeeds:

1. `isabelle_multi_attempt` with the standard methods as candidates: `by simp`, `by auto`,
   `by blast`, `by force`, `by fastforce` (with `using <facts>` or `simp add: <rules>` if you know
   them), `timeout` 10.
2. `isabelle_sledgehammer(file, line)`. Paste a suggestion verbatim. Prefer `simp`, `auto` or
   `fastforce` suggestions over `metis` or `rule`, and those over `smt`, unless much slower.
3. A `sorry` skeleton: write the structure of the proof (case split, induction, intermediate `have`s,
   or separate lemmas) with `sorry` at every leaf, check it, then run `isabelle_fill_sorries` and
   write its `patched_text` to the file. Split the leaves that remain and repeat.

Rules:
- After two failed attempts on the same goal, go to step 3.
- Do not write `metis` calls or fact lists you have not seen succeed; get them from sledgehammer.
- Run at most a few sledgehammers at a time.
- Re-check the file after filling `sorry`s. Leave no `sorry` in the final file.

## Steps that do not finish

A proof step must finish within 10 seconds. Treat a step that runs longer as non-terminating.

- A result with `still_running` lists the commands the last check of the file left unfinished. A
  listed command may be waiting behind an earlier one: look at the earliest line first.
- A running step blocks every later check and every `isabelle_query` of the file. Replace it (with a
  tested alternative or `sorry`) before you query or continue.

## Searching

- Use `isabelle_query(file, "find_theorems …")` and `isabelle_query(file, "find_consts …")`.
- In patterns, write variables as `?x` or `_`; a free `x` matches only `x`. Combine criteria
  (`name: rev`, `"rev (rev _) = _"`), negate with `-`, limit with `find_theorems (20)`. `intro`,
  `elim`, `dest` and `solves` refer to the goal at the end of the file.
- These commands and sledgehammer see only transitively imported theories. Search other theories
  with `grep`.
- Before writing a helper lemma, `grep` the framework for an existing lemma or rule collection
  (`lemmas name = …`) and use it by name.

## Rules and attributes

- Pass rules by name (`simp add:`, `intro:`, or the framework's method options). Do not declare
  global `[simp]` or other attributes.
- `apply` scripts are the normal style in program logics; use structured Isar for side lemmas.
