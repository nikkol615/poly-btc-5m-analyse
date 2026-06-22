# Documentation index

This directory is the single source of truth for how the project is structured,
what its known issues are, and how to evolve it without making the existing
problems worse.

## For humans

| File | What it covers |
|---|---|
| [`architecture.md`](architecture.md) | Components, processes, data flow |
| [`database.md`](database.md) | Postgres schema, growth math, compaction |
| [`data-sources.md`](data-sources.md) | Quirks of each upstream feed (Polymarket RTDS/CLOB, Binance, Chainlink) |
| [`backtester.md`](backtester.md) | Strategy parameters, metrics, biases to avoid |
| [`deployment.md`](deployment.md) | Coolify resource layout, env vars, networking |
| [`backlog.md`](backlog.md) | **Technical debt and known issues — read before designing anything new** |
| [`runbook.md`](runbook.md) | On-call playbook: disk full, recovery loop, lost SSH, etc. |

## For agents

[`agent/`](agent/) — instructions for AI agents working in the codebase.
Start with [`agent/README.md`](agent/README.md). Those documents exist because
the existing codebase has rough edges that should not be propagated. The agent
docs explicitly call out what NOT to copy.
