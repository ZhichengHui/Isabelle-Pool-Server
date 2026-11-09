---
name: isabelle-proof-development
description: Rules for developing Isabelle/HOL proofs with the Isabelle Pool Server MCP (the isabelle_* tools of the isabelle-pool-server MCP server). Use whenever you write or repair proofs in a .thy file through that MCP.
---

# Proof development with the Isabelle Pool Server MCP

## Tools

Load a tool (ToolSearch) when a rule below calls for it; its description says how to call it.

| Tool | What it does | When to use it |
|---|---|---|
| `isabelle_open` | Binds the file to an Isabelle session and checks it. | Once per file, before the other file tools. |
| `isabelle_diagnostic_messages` | Re-checks the file from the first changed command and reports errors, warnings and `still_running`. | After every edit of the file. |
| `isabelle_goal` | The goals before (`goals_before`) and after (`goals_after`) the command at a line. | Before writing a step, to read the goal it must prove. |
| `isabelle_multi_attempt` | Checks the file's text before a line followed by each candidate, each on its own session with a time limit; the file is not changed. | To test a step you wrote yourself before writing it into the file. |
| `isabelle_sledgehammer` | Searches a proof of a goal (at a line, optionally one subgoal) with external provers and returns proof-method suggestions. | When the standard methods fail on a goal. |
| `isabelle_fill_sorries`, `isabelle_fill_status`, `isabelle_fill_cancel` | Runs sledgehammer on every `sorry` of the file on the server and returns the patched text; collects a job that is still running; stops a job. | For a `sorry` skeleton. |
| `isabelle_query` | Runs a read-only command (`find_theorems`, `find_consts`, `thm`, `term`, …) at the end of the file. | To find lemmas and constants. |
| `isabelle_release_idle`, `isabelle_memory_status` | Frees idle sessions; reports who holds the prover's memory. | When a result reports `memory_pressure` or a memory reminder. |
| `isabelle_heap_status`, `isabelle_build_heap` | List the available session images; build one from a project directory. | When the file imports theories of a project directory that has no ready image. |

## Workflow

1. If the file imports theories of a project directory without a ready image (`isabelle_heap_status`),
   build it with `isabelle_build_heap`. Then `isabelle_open` the file, with `heap_session` if it
   needs that image.
2. For each step: read its goal with `isabelle_goal` (`goals_before` at its `sorry` placeholder, or
   `goals_after` of the command before it), find a step that proves it (next section), and write it
   into the file.
3. After every edit, check the file with `isabelle_diagnostic_messages` and fix what it reports.
4. The proof is complete when `isabelle_diagnostic_messages` reports no errors and no
   `still_running`, and the file contains no `sorry` (check with `grep`). Then run the task's batch
   build command in the shell, once; if it fails, fix what it reports as in step 3. If the task gives
   no batch build command, skip it. Do not use the batch build to test steps.

Write into the file only:
- a candidate whose `isabelle_multi_attempt` result is `success`, or, for a step that does not close
  the goal (`apply`, `proof`), `proof_open` or `pending_qed` with empty `failed`; a result with
  `timed_out` or `used_sorry` counts as failed;
- a suggestion of `isabelle_sledgehammer` (it counts as tested);
- the structure of a `sorry` skeleton, with `sorry` as the placeholders.

## Finding a step that proves a goal

For each goal, do these in order and stop at the first that succeeds. Steps 1 and 2 run once per
goal.

1. `isabelle_multi_attempt` with `timeout` 10 and the candidates `by simp`, `by auto`, `by blast`,
   `by force`, `by fastforce` (in an `apply` script: `apply simp`, `apply auto`, …). If you know
   relevant facts, add the same methods with `using <facts>`, and with `simp add: <rules>` for
   `simp`, `auto`, `force` and `fastforce`.
2. `isabelle_sledgehammer` on the goal; if it has several subgoals, on each subgoal, combining the
   suggestions with `apply`. Choose by method: first `simp`, `auto`, `fastforce`; then `metis`,
   `rule`; then any other method except `smt`; `smt` last. Among equals, take the earlier result.
3. A `sorry` skeleton: write the structure of the proof (case split, induction, intermediate `have`s,
   or separate lemmas) with `sorry` at every leaf, check it until it has no errors, then call
   `isabelle_fill_sorries`. While the result has `continuing: true`, wait about its `eta_s` and call
   `isabelle_fill_status`. Write the `patched_text` into the file and check it. For each leaf that is
   still `sorry`, split it into smaller steps, run steps 1 and 2 on each new leaf, then repeat step 3
   for the rest. If a leaf is still `sorry` after two splits, stop: do not run the batch build, and
   report every remaining `sorry` with its line and goal.

- Run at most 2 `isabelle_sledgehammer` calls at a time; a running fill job counts as one.
- While a fill job runs on a file, do not edit the file and do not run sledgehammer on it. A tool that
  answers `busy` is waiting for that job: call `isabelle_fill_status` instead.

## Time limit

- Every step must finish within 10 seconds; a result with `timed_out` counts as failed.
- `still_running` means the last check of the file stopped with these commands unfinished; they and
  the commands after them are unchecked. Replace the step at the smallest listed line with a tested
  alternative or `sorry`, then check again. Do this before any `isabelle_query` on the file: a query
  waits until the running commands finish.

## Memory

When a result reports `memory_pressure` or a memory reminder, call `isabelle_release_idle` and retry
the failed call. If it fails again, call `isabelle_memory_status`; if the memory is held by others,
stop and report it.

## Searching

- Use `isabelle_query` with `find_theorems` and `find_consts`. Write variables in patterns as `?x`
  or `_`.
- The criteria `intro`, `elim`, `dest` and `solves` search for the goal left open at the end of the
  file. Use them only when the file's last command leaves a proof open; do not cut the file for this.
  Otherwise search by pattern.
- `find_theorems`, `find_consts` and sledgehammer see only theories the file imports, directly or
  indirectly. Search other theories with `grep`.
- Before writing a helper lemma, `grep` the imported theories and the project's files for an existing
  lemma or rule collection (`lemmas name = …`) and use it by name.

## Rules and style

- Pass rules by name (`simp add:`, `intro:`, `elim:`, `dest:`, or a proof method's own rule options).
  Do not give lemmas attributes such as `[simp]`, `[intro]`, `[elim]` or `[dest]`, and do not write
  `declare … [attribute]`.
- Follow the proof style of the surrounding proofs (`apply` scripts or structured Isar).
