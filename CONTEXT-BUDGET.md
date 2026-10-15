# Context budget

Agents working here have a code graph ([code-review-graph](https://github.com/tirth8205/code-review-graph))
so they can ask about structure instead of reading whole files. Every request re-sends the whole
conversation, so a file read early in a long session is paid for again on every later request.

## For agents

- Ask the graph first (the `code-review-graph` MCP tools: architecture overview, callers, impact
  radius, review context). Then read only the lines you need with Read `offset`/`limit`, or Grep.
- After large changes, refresh the graph: `code-review-graph update`.

## For people

- Measure this repository: `pnpm aios tokens --project <repo-folder>` in the AIOS repository, or the
  **Harness fleet** view at http://127.0.0.1:4777/.
- Paths the graph should skip belong in `.code-review-graphignore`.
- The graph database lives in `.code-review-graph/` and is not committed.
