#!/bin/bash
# Seeds the dev SQL Server (the mssql-init container): waits for the server, skips when the seed already ran, then runs
# init/*.sql in order. -I: QUOTED_IDENTIFIER ON (filtered indexes need it); -b: stop at the first error (the container
# then exits non-zero and `up --wait` reports it).
# Both successful paths end idle instead of exiting: `docker compose up --wait` fails when a one-shot service has
# already exited ("exited (0)"), so the container stays up until `docker stop`, which the TERM trap answers at once.
set -euo pipefail
idle() { trap 'exit 0' TERM; sleep infinity & wait; }
SQLCMD=(/opt/mssql-tools18/bin/sqlcmd -S mssql -U sa -P "$MSSQL_SA_PASSWORD" -C -I -b)
for _ in $(seq 1 120); do
  if "${SQLCMD[@]}" -Q "SELECT 1" >/dev/null 2>&1; then break; fi
  sleep 2
done
seeded="$("${SQLCMD[@]}" -h -1 -W -Q "SET NOCOUNT ON; SELECT CASE WHEN OBJECT_ID('dbaguy.dbo.seed_done') IS NULL THEN 0 ELSE 1 END")"
if [[ "$seeded" == "1" ]]; then
  echo "already seeded"
  idle
fi
for file in /mssql/init/[0-9]*.sql; do
  echo "seeding $file"
  "${SQLCMD[@]}" -i "$file"
done
echo "seeded"
idle
