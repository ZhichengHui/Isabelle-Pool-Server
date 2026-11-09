---
name: isabelle-proof-development
description: Rules for developing Isabelle/HOL proofs with the Isabelle Pool Server MCP (the isabelle_* tools of the isabelle-pool-server MCP server). Use whenever you write or repair proofs in a .thy file through that MCP.
---

# Proof development with the Isabelle Pool Server MCP

## Tools

Load a tool (ToolSearch) when a rule below calls for it; its description says how to call it.

| Tool | What it does | When to use it |
|---|---|---|
| `isabelle_open` | Binds the file to an Isabelle session and checks it. | Once per file, before anything else. |
| `isabelle_diagnostic_messages` | Re-checks the file from the first changed command and reports errors, warnings and `still_running`. | After every edit of the file. |
| `isabelle_goal` | The goal before (`goals_before`) and after the command at a line. | Before writing a step: `goals_before` is the goal it must prove. |
| `isabelle_multi_attempt` | Checks the file up to a line followed by each candidate, on separate sessions with a time limit; the file is not changed. | To test a step you wrote yourself before writing it into the file. |
| `isabelle_sledgehammer` | Searches a proof of the goal at a line with external provers; returns tested proof suggestions. | When the standard methods fail on a goal. |
| `isabelle_fill_sorries`, `isabelle_fill_status`, `isabelle_fill_cancel` | Runs sledgehammer on every `sorry` of the file on the server and returns the patched text; collects a job that is still running; stops a job. | For a `sorry` skeleton. |
| `isabelle_query` | Runs a read-only command (`find_theorems`, `find_consts`, `thm`, `term`, …) at the end of the file. | To find lemmas and constants. |
| `isabelle_release_idle` | Frees idle sessions. | When a result reports `memory_pressure` or a memory reminder. |
| `isabelle_heap_status`, `isabelle_build_heap` | List and build session images. | When the file imports a session that has no image yet. |

## Workflow

1. `isabelle_open` the file.
2. For each step: read its goal with `isabelle_goal`, find a step that proves it (next section), and
   write it into the file.
3. Check the file with `isabelle_diagnostic_messages` after every edit and fix what it reports.
4. The proof is complete when `isabelle_diagnostic_messages` reports no errors, no `still_running`
   and no `sorry`. Then run the task's batch build command in the shell, once. Do not use the batch
   build to test steps.

Write into the file only:
- a candidate whose `isabelle_multi_attempt` result is `success`, or, for an intermediate step
  (`apply`, `proof`), `proof_open` or `pending_qed` with empty `failed` and no `timed_out`;
- a suggestion of `isabelle_sledgehammer`;
- the structure of a `sorry` skeleton, with `sorry` as the placeholders.

## Finding a step that proves a goal

Do these in order, each once, and stop at the first that succeeds:

1. `isabelle_multi_attempt` with the candidates `by simp`, `by auto`, `by blast`, `by force`,
   `by fastforce` (with `using <facts>` or `simp add: <rules>` you consider relevant), `timeout` 10.
2. `isabelle_sledgehammer` on the goal. Take the first suggestion in this order: `simp`, `auto` or
   `fastforce`; then `metis` or `rule`; then `smt`.
3. A `sorry` skeleton: write the structure of the proof (case split, induction, intermediate `have`s,
   or separate lemmas) with `sorry` at every leaf, check it until it has no errors, then call
   `isabelle_fill_sorries`. While its result says `continuing`, call `isabelle_fill_status` again.
   Then write the `patched_text` into the file and check it. Split each leaf that
   is still `sorry` into smaller steps and repeat this step. If a leaf is still `sorry` after two
   splits, leave it and report its line and goal.

- Write `metis` calls and fact lists into the file only if they succeeded in `isabelle_multi_attempt`
  or came from `isabelle_sledgehammer`.
- Run at most 2 sledgehammers at a time. While a fill job runs on a file, do not edit that file and
  do not run sledgehammer on it.

## Time limit

- Every step must finish within 10 seconds. Test steps you wrote yourself with
  `isabelle_multi_attempt` and `timeout` 10; a result with `timed_out` counts as failed.
  Sledgehammer suggestions are already tested.
- `still_running` means the last check of the file stopped with these commands unfinished; they and
  the commands after them are unchecked. Replace the step at the smallest listed line with a tested
  alternative or `sorry`, then check again. Do this before any `isabelle_query` on the file: a query
  waits until the running commands finish.

## Searching

- Use `isabelle_query` with `find_theorems` and `find_consts`. Write variables in patterns as `?x` or
  `_` (a free `x` matches only `x`), combine criteria (`name: rev "rev (rev _) = _"`), negate with
  `-`, limit with `find_theorems (20)`.
- The criteria `intro`, `elim`, `dest` and `solves` search for the goal at the end of the file. Use
  them only when the file ends inside an unfinished proof; otherwise search by pattern.
- `find_theorems`, `find_consts` and sledgehammer see only theories the file imports, directly or
  indirectly. Search other theories with `grep`.
- Before writing a helper lemma, `grep` the imported theories and the project's files for an existing
  lemma or rule collection (`lemmas name = …`) and use it by name.

## Rules and style

- Pass rules by name (`simp add:`, `intro:`, `elim:`, `dest:`, or a proof method's own rule options).
  Do not give lemmas attributes such as `[simp]`, `[intro]`, `[elim]` or `[dest]`, and do not write
  `declare … [attribute]`.
- Follow the proof style of the surrounding proofs (`apply` scripts or structured Isar).
