"""API models of the Couchbase features: the plan view (EXPLAIN, ADVISE and the profile of a statement as an operator
tree with flags, index advice and timings, spec §3.10), the query contexts a tab can pick (§3.11.1) and the field paths
of a keyspace's sampled documents (R-04-11)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PlanBadge = Literal["primary_scan", "sequential_scan", "intersect_scan", "not_covered", "slowest"]


class PlanDetail(BaseModel):
    label: str
    value: str


class PlanEstimates(BaseModel):  # EE cost-based optimizer
    cost: float | None = None
    cardinality: float | None = None


class PlanStats(BaseModel):  # a profile's `#stats`, durations as the server wrote them (Go: "34.611027ms", "10µs")
    items_in: int | None = None
    items_out: int | None = None
    exec_time: str | None = None
    serv_time: str | None = None
    kern_time: str | None = None


class PlanNode(BaseModel):
    id: int  # pre-order number over the whole view: the main tree first, then each subquery's
    operator: str
    title: str  # "IndexScan3 · idx_orders_amount", "Fetch · o", "Filter"
    details: list[PlanDetail] = []
    estimates: PlanEstimates | None = None
    stats: PlanStats | None = None
    badges: list[PlanBadge] = []
    children: list["PlanNode"] = []


class PlanFinding(BaseModel):
    badge: PlanBadge
    severity: Literal["warning", "info"]
    node_id: int
    text: str


class IndexAdvice(BaseModel):
    kind: Literal["recommended", "covering", "current"]
    statement: str  # `index_statement`, fully qualified: it can be copied and run as it is
    keyspace_alias: str | None = None
    rule: str | None = None  # `recommending_rule` (a covering index: `index_property`); a current index: `index_status`
    update_statistics: str | None = None


class PlanSubquery(BaseModel):
    text: str
    correlated: bool
    root: PlanNode


class PlanView(BaseModel):
    kind: Literal["plan", "profile", "advice", "unknown"]
    root: PlanNode | None = None
    subqueries: list[PlanSubquery] = []
    findings: list[PlanFinding] = []
    advice: list[IndexAdvice] = []
    advice_note: str | None = None  # the server's own words when it recommends nothing
    statement_text: str | None = None  # EXPLAIN's `text`, ADVISE's `query`
    estimates: PlanEstimates | None = None  # the whole plan's (EXPLAIN's top-level cost / cardinality)
    phase_times: list[PlanDetail] = []  # a profile's `phaseTimes`, in the server's order


class ContextOption(BaseModel):
    """One entry of the query-context picker. `text` is `QueryContext(bucket, scope).text()`, so the frontend never
    quotes a name (B13)."""

    bucket: str
    scope: str | None = None
    text: str
    listed: bool = True  # False: the tab's current context, which the schema snapshot does not list


class Keyspace(BaseModel):
    """A collection by its three names. Frozen: it is the key of the field-sample cache."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket: str = Field(min_length=1, max_length=100)
    scope: str = Field(min_length=1, max_length=251)
    collection: str = Field(min_length=1, max_length=251)


class FieldPath(BaseModel):
    """One field path of a sample: how many documents have it and with which JSON kinds."""

    path: list[str | None]  # keys from the document's root; None: an element of an array; []: the document itself
    types: dict[str, int]  # JSON kind ("binary" for a binary document) → documents in which the path has that kind
    docs: int  # documents in which the path occurs
    percent: float  # docs / documents sampled × 100, one decimal


class FieldSample(BaseModel):
    """A manual sample of a keyspace (R-04-11): the field paths derived in Python from the documents read."""

    keyspace: Keyspace
    sampled_at: datetime
    requested: int  # the sample size asked for
    documents: int  # documents read
    cut_at_bytes: bool  # stopped at SAMPLE_MAX_BYTES before `requested` documents
    paths_capped: bool  # paths left out: more than FIELD_PATHS_MAX of them, or a key over FIELD_KEY_MAX_CHARS
    elapsed_ms: int
    fields: list[FieldPath]
