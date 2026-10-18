#!/bin/bash
# Seeds the dev Couchbase cluster (the couchbase-init container, same image as `couchbase`): initialises the one-node
# cluster, installs travel-sample, creates the dbaguy bucket with its scopes, collections, indexes and documents, and
# the limited user. Fake credentials only. Every step can run again (IF NOT EXISTS, UPSERT, "exists" checks), so a seed
# that stopped half-way is finished by the next `up`. The last step writes dbaguy._default._default::seed_done, which
# the `couchbase` healthcheck waits for.
# Both successful paths end idle instead of exiting: `docker compose up --wait` fails once a one-shot service has
# exited (R-03b-P1). TERM ends the container at once in every phase.
set -euo pipefail
trap 'exit 0' TERM
# bash runs a pending trap only after the foreground command has ended, so slow commands run in the background and are
# waited for. Not for commands that read stdin: a background command reads /dev/null.
run() { "$@" & wait $!; }
idle() { sleep infinity & wait; }
# step WHAT: names the step in `docker logs`, so a bare curl or cbq error says where the seed stopped.
step() { echo "seed: $*"; }
MGR=http://couchbase:8091
QRY=http://couchbase:8093/query/service
CURL=(curl -sS -u dbaguy:dbaguy)
CBQ=(cbq -e http://couchbase:8093 -u dbaguy -p dbaguy -q -exit-on-error)

# q STATEMENT: one SQL++ statement; anything but status "success" stops the seed (printed to stderr).
q() {
  local out
  out="$(run "${CURL[@]}" "$QRY" --data-urlencode "statement=$1")" || { echo "$out" >&2; return 1; }
  if ! grep -q '"status": *"success"' <<<"$out"; then echo "$1" >&2; echo "$out" >&2; return 1; fi
  printf '%s' "$out"
}
# value STATEMENT: the first result of a SQL++ statement.
value() { q "$1" | python3 -c 'import json, sys; print(json.load(sys.stdin)["results"][0])'; }
# wait_for SECONDS WHAT COMMAND…: retries COMMAND every 2 s.
wait_for() {
  local limit=$1 what=$2
  shift 2
  for _ in $(seq 1 $((limit / 2))); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    run sleep 2
  done
  echo "timed out waiting for $what" >&2
  return 1
}
# doc SCOPE COLLECTION ID JSON: stored byte for byte through the console's docs endpoint (SQL++ would sort the keys and
# normalise the numbers, survey §9.3).
doc() {
  run "${CURL[@]}" -f -o /dev/null -X POST "$MGR/pools/default/buckets/dbaguy/scopes/$1/collections/$2/docs/$3" \
    --data-urlencode "value=$4"
}
# blob ID FLAGS: a non-JSON document in dbaguy.app.blobs, value read from /tmp/ID (cbc reads stdin: not through `run`).
# cbc reads -f as a decimal number only ("0x…" is "trailing garbage"), so FLAGS goes through bash arithmetic.
blob() { cbc create -U couchbase://couchbase/dbaguy -u dbaguy -P dbaguy --scope app --collection blobs -f "$(($2))" "$1" <"/tmp/$1"; }
seeded() { [[ "$(value 'SELECT RAW COUNT(*) FROM dbaguy._default._default USE KEYS "seed_done"')" == 1 ]]; }
sample_ready() {
  ! "${CURL[@]}" "$MGR/pools/default/tasks" | grep -q loadingSampleBucket &&
    [[ "$(value 'SELECT RAW COUNT(*) FROM system:indexes AS i WHERE i.bucket_id = "travel-sample" OR i.keyspace_id = "travel-sample"')" -gt 0 ]] &&
    [[ "$(value 'SELECT RAW COUNT(*) FROM system:indexes AS i WHERE (i.bucket_id = "travel-sample" OR i.keyspace_id = "travel-sample") AND i.state != "online"')" == 0 ]]
}
# Only the scopes this seed creates are counted (P36): a `_system` scope (7.6) or a `_default._default` row that carries
# bucket and scope fields would make an unrestricted count 308–310, the wait would time out and `up --wait` would fail.
collections_ready() {
  [[ "$(value 'SELECT RAW COUNT(*) FROM system:keyspaces AS k WHERE k.`bucket` = "dbaguy" AND k.`scope` IN ["app", "many"]')" == 307 ]]
}
indexes_ready() {
  [[ "$(value 'SELECT RAW COUNT(*) FROM system:indexes AS i WHERE i.bucket_id = "dbaguy" AND i.scope_id = "app" AND i.state = "online"')" == 4 ]]
}

wait_for 300 "the cluster manager" curl -sf -o /dev/null "$MGR/ui/index.html"
if seeded 2>/dev/null; then
  echo "already seeded"
  idle
fi
step "cluster init"
if ! "${CURL[@]}" -f -o /dev/null "$MGR/pools/default" 2>/dev/null; then
  run curl -sSf -o /dev/null -X POST "$MGR/clusterInit" -d hostname=127.0.0.1 -d services=kv,n1ql,index \
    -d memoryQuota=512 -d indexMemoryQuota=256 -d indexerStorageMode=plasma -d username=dbaguy -d password=dbaguy \
    -d port=SAME
fi
wait_for 120 "the query service" q 'SELECT 1'

step "travel-sample"
if ! "${CURL[@]}" -f -o /dev/null "$MGR/pools/default/buckets/travel-sample" 2>/dev/null; then
  run "${CURL[@]}" -f -o /dev/null -X POST "$MGR/sampleBuckets/install" -d '["travel-sample"]'
fi
wait_for 300 "travel-sample and its indexes" sample_ready

step "dbaguy bucket, scopes and collections"
if ! "${CURL[@]}" -f -o /dev/null "$MGR/pools/default/buckets/dbaguy" 2>/dev/null; then
  run "${CURL[@]}" -f -o /dev/null -X POST "$MGR/pools/default/buckets" -d name=dbaguy -d ramQuota=100 \
    -d bucketType=couchbase
fi
wait_for 120 "the dbaguy bucket" q 'CREATE SCOPE dbaguy.app IF NOT EXISTS'
run "${CBQ[@]}" -f /couchbase/init/01-keyspaces.n1ql
for i in $(seq -w 1 300); do echo "CREATE COLLECTION dbaguy.many.c$i IF NOT EXISTS;"; done >/tmp/many.n1ql
run "${CBQ[@]}" -f /tmp/many.n1ql
wait_for 120 "the collections" collections_ready  # 300 in many + 7 in app
step "indexes"
run "${CBQ[@]}" -f /couchbase/init/02-indexes.n1ql
wait_for 180 "the indexes" indexes_ready  # three primary indexes and idx_orders_amount; idx_users_deferred stays deferred

step "documents"
doc app users u_numbers '{"big":9007199254740993,"i64max":9223372036854775807,"i64min":-9223372036854775808,"u64max":18446744073709551615,"beyond":12345678901234567890,"dec":1.10,"pi":3.141592653589793238462643383279,"sci":1E+3,"tiny":1e-400,"huge":1e400,"half":0.5}'
doc app users u_keyorder '{"zeta": 1,
 "alpha": {"z": 1, "a": 2}, "ws" :  [ 1 , 2 ], "mid": "a b"}'
doc app users u1 '{"name":"Ann","age":31,"tags":["a","b"],"address":{"city":"Taipei","zip":"100"}}'
doc app users u2 '{"name":"Bob","age":null,"address":{"city":"Tainan"}}'
doc app users u3 '{"title":"no name field","nested":{"deep":{"deeper":[1,{"x":null}]}}}'
doc app users u4 '{"name":"Dee","value":true,"select":1,"odd key":"needs quoting"}'
doc app users u5 '{"name":"Eve","age":28,"tags":[]}'
doc app users s1 '"just a string document"'
doc app users a1 '[1,2,3]'
doc app users n1 '42'
doc app blobs j1 '{"ok":true}'
doc app value v1 '{"value":1}'
doc app odd-name x1 '{"k":"v"}'
printf '\x00\x01\xffbinary' >/tmp/bin1
printf 'plain text, not JSON' >/tmp/txt1
blob bin1 0x03000002  # A20: binary and text documents (META().type "base64"); they trip INFER and ADVISE (survey §5.3)
blob txt1 0x04000004
run "${CBQ[@]}" -f /couchbase/init/03-data.n1ql

step "limited user"
run "${CURL[@]}" -f -o /dev/null -X PUT "$MGR/settings/rbac/users/local/dbaguy_reader" -d password=dbaguy \
  --data-urlencode 'roles=query_select[dbaguy:app:users],data_reader[dbaguy:app:users]'
q 'UPSERT INTO dbaguy._default._default (KEY, VALUE) VALUES ("seed_done", {"at": NOW_STR()})' >/dev/null
echo "seeded"
idle
