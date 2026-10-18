# Dev databases

PostgreSQL 17, MySQL 8.4, SQL Server 2022 and an SSH bastion, all bound to `127.0.0.1`. Fake credentials only (see `docker-compose.yml`).

```sh
docker compose -f dev/docker-compose.yml up -d --build --wait
```

## Re-seeding

The seed scripts (`postgres/init/`, `mysql/init/`) only run when a database starts with an empty data directory, so editing
them has no effect on a running container. To apply a change, remove the container and its anonymous data volume, then start it again:

```sh
docker compose -f dev/docker-compose.yml rm -sfv mysql      # or: postgres
docker compose -f dev/docker-compose.yml up -d --build --wait
```

`--wait` returns once the healthcheck passes, which for MySQL means the seed has finished and the `dbaguy` account can log in.
Every MySQL init file starts with `SET NAMES utf8mb4;` because the image's init client runs in the POSIX locale and would
otherwise store non-ASCII text double-encoded.

## SQL Server

The SQL Server image is amd64 only, so it runs under Rosetta (`platform: linux/amd64` in OrbStack) and is slower to start
(about 15-30 s). Port `14337`; `sa` / `Dbaguy_dev1`, and the seed adds the login `dbaguy` / `Dbaguy_dev1` (db_owner of `dbaguy` and
`analytics`, no access to `locked_away`). The bastion reaches it as `mssql:1433`.

SQL Server has no init directory, so the seed runs in the one-shot `mssql-init` container (`mssql/seed.sh`, which feeds
`mssql/init/*.sql` to `sqlcmd -C -I -b`). It skips the seed when `dbaguy.dbo.seed_done` already exists, and then stays up idle
instead of exiting, because `docker compose up --wait` fails once a one-shot service has exited. That is what makes repeated
`up -d --build --wait` runs succeed; `docker stop` ends the idle container at once.

`--wait` covers the seed because the `mssql` healthcheck looks for `seed_done`, the last table the seed creates. To re-seed:

```sh
docker compose -f dev/docker-compose.yml rm -sfv mssql mssql-init
docker compose -f dev/docker-compose.yml up -d --build --wait
```
