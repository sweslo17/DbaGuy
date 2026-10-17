# Dev databases

PostgreSQL 17, MySQL 8.4 and an SSH bastion, all bound to `127.0.0.1`. Fake credentials only (see `docker-compose.yml`).

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
