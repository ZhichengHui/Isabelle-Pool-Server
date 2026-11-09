---
name: isabelle-proof-development
description: Rules for developing Isabelle/HOL proofs with the Isabelle Pool Server MCP (the isabelle_* tools of the isabelle-pool-server MCP server). Use whenever you write or repair proofs in a .thy file through that MCP.
---

# Proof development with the Isabelle Pool Server MCP

## Tools

Load a tool with ToolSearch when a rule calls for it; its description says how to call it.

| Tool | Use it to |
|---|---|
| `isabelle_open` | bind the file to an Isabelle session (once per file) |
| `isabelle_diagnostic_messages` | re-check the file after an edit: errors, warnings, `still_running` |
| `isabelle_goal` | read the goals before and after the command at a line |
| `isabelle_multi_attempt` | test candidate steps at a line on separate sessions, without changing the file; reports for each whether it passed and the goals it leaves |
| `isabelle_sledgehammer` | search a proof of a goal with external provers |
| `isabelle_fill_sorries`, `isabelle_fill_status` | run sledgehammer on every `sorry` of the file on the server; collect a running job |
| `isabelle_query` | run `find_theorems`, `find_consts`, `thm`, `term`, … on the file |

## Rules

1. `isabelle_open` the file. After every edit, check it with `isabelle_diagnostic_messages`.
2. Read the goal a step must prove with `isabelle_goal` (`goals_before` at the step's line).
3. Test every step with `isabelle_multi_attempt` before writing it into the file, however short it is:
   a step that does not finish blocks every later check and query of the file. A candidate passes
   when `failed` is empty and it is neither `timed_out` nor `used_sorry`; `proof_open` only means the
   proof continues. Only sledgehammer suggestions and the structure of a `sorry` skeleton need no test.
4. A step must finish within 10 seconds (`timeout` 10), unless the project's skill sets another limit.
5. When a result has `still_running`, replace the step at the smallest listed line with a tested
   alternative or `sorry` and check again, before anything else.
6. The proof is done when `isabelle_diagnostic_messages` reports no errors and no `still_running`
   and `grep` finds no `sorry` in the file. Then run the task's batch build once, saving its output
   with `tee`.

## Finding a step

For goals of a domain that has its own skill or worked examples (such as a program logic), follow
those first. Otherwise, in this order:

1. `isabelle_multi_attempt` with `simp`, `auto`, `blast`, `force`, `fastforce`, adding the facts you
   know are relevant.
2. `isabelle_sledgehammer`. Prefer a `simp`, `auto` or `fastforce` suggestion over `metis`, and
   `metis` over `smt`.
3. A `sorry` skeleton: write the proof's structure (cases, induction, intermediate `have`s or
   lemmas) with `sorry` at the leaves, check it, and run `isabelle_fill_sorries` on it. Split the
   leaves that remain and repeat; leave a leaf that survives two splits as `sorry` and report its
   line and goal.

## Searching

- Use `isabelle_query` with `find_theorems` and `find_consts`; write pattern variables as `?x` or `_`.
- These commands see only the theories the file imports; search others with `grep`.
- Before writing a helper lemma, `grep` the imported theories and the project for an existing one.

## Style

- Pass rules by name (`simp add:`, `intro:`, a method's rule options); add no attributes such as
  `[simp]` and no `declare`.
- Follow the proof style of the surrounding proofs.
