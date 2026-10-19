"""API models of the Couchbase features: the plan view (EXPLAIN, ADVISE and the profile of a statement as an operator
tree with flags, index advice and timings, spec §3.10), the query contexts a tab can pick (§3.11.1), the field paths
of a keyspace's sampled documents (R-04-11), a document as read and as saved (§3.9.2), and what saving a document would
change (§3.9.3)."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT = "_default"  # the name of a bucket's default scope, and of its default collection

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
    display: str  # the path as SQL++ writes it (a key that needs back-ticks has them; `[]` an array's elements)
    types: dict[str, int]  # JSON kind ("binary" for a binary document) → documents in which the path has that kind
    docs: int  # documents in which the path occurs
    percent: float  # docs / documents sampled × 100, one decimal


class SampleLimits(BaseModel):
    """What bounds a sample, as the backend has it: the UI words what a sample left out with these numbers."""

    max_bytes: int  # reading stops after the document that takes the documents read past this (SAMPLE_MAX_BYTES)
    max_paths: int  # field paths kept; later ones are left out (FIELD_PATHS_MAX)
    max_depth: int  # levels of a path kept; nothing deeper is listed (FIELD_PATH_MAX_DEPTH)
    max_key_chars: int  # a longer key is left out with everything under it (FIELD_KEY_MAX_CHARS)


class FieldSample(BaseModel):
    """A manual sample of a keyspace (R-04-11): the field paths derived in Python from the documents read."""

    keyspace: Keyspace
    sampled_at: datetime
    requested: int  # the sample size asked for
    documents: int  # documents read
    cut_at_bytes: bool  # stopped at SAMPLE_MAX_BYTES before `requested` documents
    paths_capped: bool  # paths left out: more than `limits.max_paths` of them
    long_keys_skipped: bool  # a key over `limits.max_key_chars` left out, with everything under it
    depth_capped: bool  # fields left out below `limits.max_depth` levels
    limits: SampleLimits
    elapsed_ms: int
    fields: list[FieldPath]


ChangeKind = Literal["key_order", "number", "duplicate_key", "other"]


class DocumentChange(BaseModel):
    """One thing the server would store differently from the edited text (spec §3.9.3, ruling C2)."""

    path: str  # JSONPath as the value viewer writes it: $, $.address, $.items[0], $["odd key"]
    kind: ChangeKind
    before: str  # the edited side: a literal, a key list (for a key-set change, the keys on this side only) or, for a
    # duplicate key, the dropped value; at most 200 characters, a long text cut around its first difference with "…"
    after: str  # the server's side; for a duplicate key, the later value (the server keeps the last one)
    value_changed: bool  # something is lost or changed; false for reordered keys and a respelled equal number


class ChangeCounts(BaseModel):
    key_order: int = 0
    number: int = 0
    duplicate_key: int = 0
    other: int = 0


DOCUMENT_ID_MAX_BYTES = 250  # Couchbase's key limit, UTF-8 bytes
DOCUMENT_TEXT_LIMIT = 20 * 1024 * 1024  # characters of a saved text; the server enforces its own 20 MiB
CAS_PATTERN = r"^[1-9][0-9]{0,18}$"
CAS_MAX = 2**63 - 1  # a CAS beyond int64 would be compared as a double by SQL++ (ruling C11)


class DocumentView(BaseModel):
    """One document as the server holds it (spec §3.9.2, R-04-2)."""

    id: str
    cas: str  # decimal text: a CAS is beyond 2^53
    expiration: int  # Unix seconds; 0 = none
    type: Literal["json", "base64"]  # base64: a binary document, shown but never changed
    text: str | None  # the stored text exactly as the server holds it; None for a binary document
    size: int  # bytes of the text; for a binary document what the server reports


class DocumentSaved(BaseModel):
    """A write that happened: the document's new CAS and its expiration."""

    status: Literal["saved"]
    cas: str
    expiration: int  # as read right after the write (A28: RETURNING says 0)
    size: int  # bytes of the saved text without the whitespace between its tokens (ruling C21)


class SavePreview(BaseModel):
    """What the server would store instead of the edited text: nothing is written until the user confirms."""

    status: Literal["needs_confirmation"]
    server_text: str  # the server's form of the text (SELECT RAW $doc, A2)
    changes: list[DocumentChange]  # value changes first, at most CHANGES_LISTED (ruling C2)
    counts: ChangeCounts  # every change, by kind


SaveAnswer = Annotated[DocumentSaved | SavePreview, Field(discriminator="status")]
