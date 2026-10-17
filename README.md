<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="logos/01_Horizontal-Logo/For-Dark-Background/DbaGuy-logo-reversed-1024px.png">
    <img src="logos/01_Horizontal-Logo/For-Light-Background/DbaGuy-logo-color-1024px.png" alt="DbaGuy" width="360">
  </picture>
</h1>

A fast, focused SQL IDE for macOS — one app for PostgreSQL, MySQL, SQLite, Couchbase (SQL++) and SQL Server.

> Personal side project, work in progress.

## Goals

- **Fast and stable**: quick startup, low memory, never lose unsaved work.
- **One tool for five engines**: PostgreSQL, MySQL, SQLite, Couchbase 7.x, SQL Server.
- **First-class Couchbase**: keyspace tree, SQL++ editor, document editing with CAS, readable EXPLAIN.
- **Focused scope**: the daily loop — connect, find objects, write and run SQL, read results, copy/export — done well.

## Stack

- Shell: [Tauri 2](https://v2.tauri.app/)
- Frontend: React + TypeScript + Vite
- Backend sidecar: Python (FastAPI, pydantic v2, loguru)

## Docs

- [Market research & tech stack](docs/research/2026-10-15-sql-ide-landscape.md)
- [Feature priorities (MoSCoW)](docs/research/2026-10-15-feature-priorities.md)
- Design specs: `docs/superpowers/specs/`
- Implementation plans: `docs/superpowers/plans/`
