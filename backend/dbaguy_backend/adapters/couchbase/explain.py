"""EXPLAIN, ADVISE and profile answers as a plan view (R-04-13, spec §3.10): the operator tree, the four anti-pattern
flags (primary scan, sequential scan, intersect scan, an index scan that does not cover the query), the index advice,
a profile's timings and its slowest operator. Pure: no I/O, and no logging (a plan holds the user's SQL).

The shapes are the ones the dev cluster answers with (04b Task 1 fixtures, survey §7). The analysis never assumes more
than that: estimates (`cost`, `cardinality`, `optimizer_estimates`) are there only when the planner had statistics, an
operator it has never seen is still a node (its child operators are found by shape, not by name), and anything that is
not a plan, a profile or an advice is `kind="unknown"` rather than an error."""

import json
import math
import re
from collections.abc import Iterator
from decimal import Decimal, localcontext
from typing import Any, Literal, get_args

from dbaguy_backend.domain.couchbase import (
    IndexAdvice,
    PlanBadge,
    PlanDetail,
    PlanEstimates,
    PlanFinding,
    PlanNode,
    PlanStats,
    PlanSubquery,
    PlanView,
)

DETAIL_MAX_CHARS = 300  # a detail longer than this ends with "…", the "…" being the 300th character
MAX_PLAN_DEPTH = 200  # deeper than any plan the server writes, and shallower than what pydantic can serialize (~250)
# More operators than any plan the server writes (a join of dozens of keyspaces has hundreds): a 4 MiB text can hold
# 220,000, which took 1.9 s, answered 24 MB and held 450 MB of memory to draw
MAX_PLAN_NODES = 10_000

# What an operator's children are, in this order; any other key whose value is an operator (or a list of operators)
# comes after them, so an operator the survey did not see still has its subtree walked.
_CHILD_KEYS = ("~children", "~child", "scans", "first", "second")
# Details that come first, in this order; the rest of an operator's scalar keys follow.
_LEADING_KEYS = ("index", "using", "condition", "covers", "spans", "limit", "offset", "on_keys", "expr",
                 "result_terms", "aggregates", "group_keys", "sort_terms")
# Keys that are never listed again among the rest: the title, the keyspace line, ids and the estimates say them.
_NOT_DETAILS = frozenset({"as", "alias", "namespace", "index_id", "bucket", "scope", "keyspace", "optimizer_estimates",
                          *_LEADING_KEYS})
_ALIAS_TITLED = frozenset({"Fetch", "Join", "NestedLoopJoin", "HashJoin", "Nest", "NestedLoopNest", "Unnest", "KeyScan",
                           "ExpressionScan", "CountScan"})

_SYSTEM_NAMESPACE = "#system"  # system:keyspaces and the like: an index cannot be created there
_CONTAINERS = frozenset({"Sequence", "Parallel"})  # their time is their children's, not work of their own
_PRIMARY_SCANS = frozenset({"PrimaryScan", "PrimaryScan3"})
_SEQUENTIAL_INDEX = "#sequentialscan"
_SEQUENTIAL_USING = "sequentialscan"
_INTERSECT_SCANS = frozenset({"IntersectScan", "OrderedIntersectScan"})
_INDEX_SCANS = frozenset({"IndexScan", "IndexScan2", "IndexScan3", "IntersectScan", "OrderedIntersectScan",
                          "UnionScan", "DistinctScan"})
_BADGE_ORDER: tuple[str, ...] = get_args(PlanBadge)

# What an index advice's `rule` is, first one present: a covering index has an `index_property` ("ORDER pushdown")
# where a recommended one has the `recommending_rule`.
_RULE_KEYS = {"current": ("index_status",), "recommended": ("recommending_rule", "index_property"),
              "covering": ("recommending_rule", "index_property")}

# Go writes microseconds as "\u00b5s" (U+00B5, the micro sign); U+03BC, the Greek letter, is accepted too.
_NS_PER_UNIT = {"h": 3_600_000_000_000, "m": 60_000_000_000, "s": 1_000_000_000, "ms": 1_000_000, "us": 1_000,
                "\u00b5s": 1_000, "\u03bcs": 1_000, "ns": 1}
_DURATION_TERM = r"([0-9]+(?:\.[0-9]+)?)(ns|us|\u00b5s|\u03bcs|ms|h|m|s)"
_DURATION = re.compile(rf"-?(?:{_DURATION_TERM})+")
_DURATION_PART = re.compile(_DURATION_TERM)


def analyze(text: str) -> PlanView:
    """The plan view of an EXPLAIN / ADVISE / profile answer's JSON text (`kind="unknown"` when it is none of them)."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return PlanView(kind="unknown")
    return analyze_value(value)


class _TooDeep(RecursionError):
    """A plan nested deeper than `MAX_PLAN_DEPTH`: the same answer as one nested deeper than the interpreter follows."""


class _TooMany(RecursionError):
    """A view of more than `MAX_PLAN_NODES` operators: the same answer as a plan that is too deep."""


def analyze_value(value: Any) -> PlanView:
    """Like `analyze()`, for a value that is already decoded."""
    try:
        return _analyze(value)
    except RecursionError:  # (also `_TooDeep`, `_TooMany`) an answer that big is not one the server wrote
        return PlanView(kind="unknown")


def _analyze(value: Any) -> PlanView:
    if isinstance(value, dict):
        if _is_operator(value.get("plan")):
            return _plan_view(value)
        if _is_operator(value.get("executionTimings")):
            return _profile_view(value)
        if value.get("#operator") == "Advise":
            return _advice_view(value)
    return PlanView(kind="unknown")


def go_duration_ns(text: str) -> int | None:
    """Go's duration text ("34.611027ms", "1h2m3.5s", "10µs") in whole nanoseconds, truncated toward zero; None when
    the whole text is not a duration."""
    if not _DURATION.fullmatch(text):
        return None
    with localcontext() as context:
        context.prec = 80  # exact for any fraction a server writes
        total = sum(Decimal(number) * _NS_PER_UNIT[unit] for number, unit in _DURATION_PART.findall(text))
    return int(-total if text.startswith("-") else total)


# --- The views ----------------------------------------------------------------------------------------------------


def _plan_view(answer: dict[str, Any]) -> PlanView:
    builder = _Builder()
    root = builder.build(answer["plan"])
    subqueries = []
    entries = answer.get("~subqueries")
    for entry in entries if isinstance(entries, list) else ():
        if isinstance(entry, dict) and _is_operator(entry.get("plan")):
            subqueries.append(PlanSubquery(text=_string(entry.get("subquery")) or "",
                                           correlated=entry.get("correlated") is True,
                                           root=builder.build(entry["plan"])))
    return PlanView(kind="plan", root=root, subqueries=subqueries, findings=builder.findings,
                    statement_text=_string(answer.get("text")), estimates=_estimates(answer))


def _profile_view(answer: dict[str, Any]) -> PlanView:
    builder = _Builder()
    root = builder.build(answer["executionTimings"])
    findings = sorted([*builder.findings, *_slowest(root)], key=lambda f: (f.node_id, _BADGE_ORDER.index(f.badge)))
    phases = answer.get("phaseTimes")
    phase_times = [PlanDetail(label=name, value=_clip(_text(time))) for name, time in phases.items()
                   ] if isinstance(phases, dict) else []
    return PlanView(kind="profile", root=root, findings=findings, phase_times=phase_times,
                    estimates=_estimates(answer.get("optimizerEstimates")))


def _advice_view(answer: dict[str, Any]) -> PlanView:
    advice = answer.get("advice")
    info = advice.get("adviseinfo") if isinstance(advice, dict) else None
    current: list[IndexAdvice] = []
    recommended: list[IndexAdvice] = []
    covering: list[IndexAdvice] = []
    note: str | None = None
    for item in info if isinstance(info, list) else (info,):
        if not isinstance(item, dict):
            continue
        current += _index_advice("current", item.get("current_indexes"))
        suggestions = item.get("recommended_indexes")
        if isinstance(suggestions, dict):
            recommended += _index_advice("recommended", suggestions.get("indexes"))
            covering += _index_advice("covering", suggestions.get("covering_indexes"))
        elif isinstance(suggestions, str):
            note = note or suggestions  # the server's own words, e.g. "No index recommendation at this time."
    return PlanView(kind="advice", advice=[*current, *recommended, *covering], advice_note=note,
                    statement_text=_string(answer.get("query")))


def _index_advice(kind: str, entries: Any) -> list[IndexAdvice]:
    found = []
    for entry in entries if isinstance(entries, list) else ():
        statement = entry.get("index_statement") if isinstance(entry, dict) else None
        if isinstance(statement, str) and statement:
            rule = next((text for key in _RULE_KEYS[kind] if (text := _string(entry.get(key)))), None)
            found.append(IndexAdvice(kind=kind, statement=statement,
                                     keyspace_alias=_string(entry.get("keyspace_alias")), rule=rule,
                                     update_statistics=_string(entry.get("update_statistics"))))
    return found


# --- The operator tree --------------------------------------------------------------------------------------------


class _Builder:
    """Numbers and builds the operators of one view, in pre-order, and collects what they are flagged for.

    The view's trees go through one builder, so an id never repeats across the main tree and its subqueries."""

    def __init__(self) -> None:
        self.findings: list[PlanFinding] = []
        self._next_id = 0
        self._uncovered: set[int] = set()  # id() of the index scans a later fetch of the same alias makes uncovered

    def build(self, op: dict[str, Any], depth: int = 1) -> PlanNode:
        if depth > MAX_PLAN_DEPTH:
            raise _TooDeep
        if self._next_id >= MAX_PLAN_NODES:
            raise _TooMany
        node_id = self._next_id
        self._next_id += 1
        operator: str = op["#operator"]
        title = _title(op, operator)
        self._uncovered |= _uncovered_scans(_operators(op.get("~children")))
        found = self._flag(node_id, op, operator, title)
        self.findings.extend(found)
        children = [self.build(child, depth + 1) for child in _children(op)]
        return PlanNode(id=node_id, operator=operator, title=title, details=_details(op),
                        estimates=_estimates(op.get("optimizer_estimates")), stats=_stats(op),
                        badges=[finding.badge for finding in found], children=children)

    def _flag(self, node_id: int, op: dict[str, Any], operator: str, title: str) -> list[PlanFinding]:
        """The flags this operator itself earns, in the order of `PlanBadge` (a profile's `slowest` is added later)."""

        def finding(badge: PlanBadge, severity: Literal["warning", "info"], text: str) -> PlanFinding:
            return PlanFinding(badge=badge, severity=severity, node_id=node_id, text=text)

        found: list[PlanFinding] = []
        if op.get("namespace") == _SYSTEM_NAMESPACE:
            return found
        keyspace = _keyspace(op) or "the keyspace"
        sequential = op.get("index") == _SEQUENTIAL_INDEX or op.get("using") == _SEQUENTIAL_USING
        bounded = op.get("limit") is not None  # a pushed-down LIMIT stops the scan: it reads no more than that
        if operator in _PRIMARY_SCANS and not sequential and not bounded:
            found.append(finding("primary_scan", "warning", f"Primary scan of {keyspace}: every document key is read; "
                                                            "an index on the filtered fields would avoid it"))
        if operator == "PrimaryScan3" and sequential and not bounded:
            found.append(finding("sequential_scan", "warning",
                                 f"Sequential scan of {keyspace}: no index is used, every document is read"))
        if operator in _INTERSECT_SCANS:
            found.append(finding("intersect_scan", "warning",
                                 "Several indexes are scanned and intersected; one composite index is usually faster"))
        if id(op) in self._uncovered:
            found.append(finding("not_covered", "info", f"{title} does not cover the query: each match is fetched "
                                                        "from the data service (the index lacks fields the query "
                                                        "needs, as with SELECT *)"))
        return found


def _slowest(root: PlanNode) -> list[PlanFinding]:
    """The operators whose own time (`execTime` + `servTime`) is more than half of all the operators' time; each gets
    the `slowest` badge. A Sequence or Parallel is not an operator here: its time includes what it waits for. With no
    time at all, none does."""
    nodes = [node for node in _preorder(root) if node.operator not in _CONTAINERS]
    times = {node.id: _own_ns(node) for node in nodes}
    total = sum(times.values())
    found = []
    for node in nodes:
        if total > 0 and times[node.id] * 2 > total:
            node.badges.append("slowest")
            share = times[node.id] / total * 100
            found.append(PlanFinding(badge="slowest", severity="info", node_id=node.id,
                                     text=f"{node.title} took {share:.0f}% of the operators' time"))
    return found


def _own_ns(node: PlanNode) -> int:
    if node.stats is None:
        return 0
    return sum(go_duration_ns(time) or 0 for time in (node.stats.exec_time, node.stats.serv_time) if time)


def _preorder(node: PlanNode) -> Iterator[PlanNode]:
    yield node
    for child in node.children:
        yield from _preorder(child)


def _is_operator(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("#operator"), str)


def _operators(value: Any) -> list[dict[str, Any]]:
    """`value` when it is an operator, its operators when it is a list; nothing otherwise."""
    if isinstance(value, list):
        return [item for item in value if _is_operator(item)]
    return [value] if _is_operator(value) else []


def _children(op: dict[str, Any]) -> list[dict[str, Any]]:
    found = [child for key in _CHILD_KEYS for child in _operators(op.get(key))]
    for key, value in op.items():
        if key not in _CHILD_KEYS and not key.startswith("#") and key != "optimizer_estimates":
            found.extend(_operators(value))
    return found


def _title(op: dict[str, Any], operator: str) -> str:
    index = _string(op.get("index"))
    if index:
        return f"{operator} · {index}"
    if operator in _ALIAS_TITLED:
        name = _string(op.get("as")) or _string(op.get("alias")) or _string(op.get("keyspace"))
        if name:
            return f"{operator} · {name}"
    return operator


def _keyspace(op: dict[str, Any]) -> str | None:
    """`bucket.scope.keyspace`, as far as the operator says it."""
    name = _string(op.get("keyspace"))
    if not name:
        return None
    return ".".join(part for part in (_string(op.get("bucket")), _string(op.get("scope")), name) if part)


# --- Uncovered index scans ----------------------------------------------------------------------------------------


def _nested_scans(op: dict[str, Any]) -> list[dict[str, Any]]:
    """The scans an intersect / union / distinct scan is made of."""
    return [*_operators(op.get("scans")), *_operators(op.get("scan"))]


def _covered(op: dict[str, Any]) -> bool:
    if op.get("covers"):
        return True
    return any(_covered(scan) for scan in _nested_scans(op))


def _alias(op: dict[str, Any]) -> str | None:
    return _string(op.get("as")) or _string(op.get("keyspace"))


def _scan_alias(op: dict[str, Any]) -> str | None:
    """The alias an index scan reads; an IntersectScan / UnionScan names none itself: its scans do."""
    return _alias(op) or next((alias for scan in _nested_scans(op) if (alias := _scan_alias(scan))), None)


def _fetches(op: dict[str, Any], alias: str) -> bool:
    """`op` or any operator below it fetches `alias`."""
    if op.get("#operator") == "Fetch" and _alias(op) == alias:
        return True
    return any(_fetches(child, alias) for child in _children(op))


def _uncovered_scans(sequence: list[dict[str, Any]]) -> set[int]:
    """Python ids of the index scans in one `~children` list that a later sibling (or one below it) fetches for."""
    found: set[int] = set()
    for position, op in enumerate(sequence):
        if op["#operator"] not in _INDEX_SCANS or _covered(op):
            continue
        alias = _scan_alias(op)
        if alias is not None and any(_fetches(later, alias) for later in sequence[position + 1:]):
            found.add(id(op))
    return found


# --- Details, estimates and stats ---------------------------------------------------------------------------------


def _details(op: dict[str, Any]) -> list[PlanDetail]:
    details: list[PlanDetail] = []

    def add(label: str, value: Any) -> None:
        text = _detail_text(label, value) if value is not None else ""
        if text:
            details.append(PlanDetail(label=label, value=text))

    add("keyspace", _keyspace(op))
    for key in _LEADING_KEYS:
        add(key, op.get(key))
    for key, value in op.items():
        if not key.startswith(("#", "~")) and key not in _NOT_DETAILS and not isinstance(value, (dict, list)):
            add(key, value)
    return details


def _detail_text(label: str, value: Any) -> str:
    if label == "covers" and isinstance(value, list):
        return _clip(_joined(value))
    if label == "result_terms" and isinstance(value, list):
        return _clip(_joined(term["expr"] for term in value if isinstance(term, dict) and "expr" in term))
    return _clip(_text(value))


def _joined(items: Any) -> str:
    return ", ".join(_text(item) for item in items)


def _text(value: Any) -> str:
    """A string as it is, anything else as compact JSON."""
    return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _clip(text: str) -> str:
    return text if len(text) <= DETAIL_MAX_CHARS else text[:DETAIL_MAX_CHARS - 1] + "…"


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None  # NaN and ±inf are not JSON: they would fail the response


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _estimates(source: Any) -> PlanEstimates | None:
    """`cost` / `cardinality` of an EXPLAIN answer or of an operator's `optimizer_estimates`, when it has them."""
    if not isinstance(source, dict):
        return None
    cost, cardinality = _number(source.get("cost")), _number(source.get("cardinality"))
    return None if cost is None and cardinality is None else PlanEstimates(cost=cost, cardinality=cardinality)


def _stats(op: dict[str, Any]) -> PlanStats | None:
    raw = op.get("#stats")
    if not isinstance(raw, dict):
        return None
    stats = PlanStats(items_in=_count(raw.get("#itemsIn")), items_out=_count(raw.get("#itemsOut")),
                      exec_time=_string(raw.get("execTime")), serv_time=_string(raw.get("servTime")),
                      kern_time=_string(raw.get("kernTime")))
    return stats if stats.model_dump(exclude_none=True) else None
