"""Disclosure floors — the policy engine stage between row-scope rewrite and execution.

Two floors are enforced here, both computed by the policy engine from the rewritten
statement; the model's original SQL is never executed by this stage.

1. k=2 customer-aggregate floor. Any aggregate over customer-grain rows is probed with a
   companion COUNT(DISTINCT customer_id) built from a copy of the rewritten tree; k < 2
   refuses with `floor_k_anonymity`. The refusal is uniform for k=0 and k=1 so a refusal
   can never be read as "exactly one customer matched". Row-grain customer reads are
   exempt (a single in-region customer's authorized columns may be viewed directly), and
   transaction/alert business metrics are exempt even when joined to customers. GROUP BY
   shapes enforce per-group k: sub-floor groups are dropped behind a generic suppression
   note, and a statement whose groups ALL fall below the floor refuses outright.

2. Protected-class reporting rule. fair_lending's customer access is aggregate-only and
   runs exclusively through the sanctioned server-side path: grouping keys from the
   allowlist (region, segment, income_band, protected-class attributes), COUNT(*)
   aggregates, and filters limited to region/segment/income_band (plus the redundant
   deleted-at-is-null form the row scope already applies). A protected-class breakdown is
   reportable only when the population in scope is >= 10 distinct customers and every
   reported cell is >= 3; cells below 3 are suppressed, and when suppression would leave
   a single suppressed cell (derivable from the reported rest) the whole breakdown
   refuses with `floor_protected_class`.

Suppression notes state the policy threshold only — never sizes, group names, or how
many groups were dropped — so they cannot enable derivation when combined with totals.
"""

import sqlite3

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import build_scope

from . import db, policy

# ---------------------------------------------------------------- floor constants

K_MIN = 2
T5_MIN_POPULATION = 10
T5_MIN_CELL = 3

T5_ATTRIBUTES = frozenset({"race", "ethnicity", "sex"})
SANCTIONED_GROUPING_KEYS = frozenset({"region", "segment", "income_band"}) | T5_ATTRIBUTES
SANCTIONED_FILTER_KEYS = frozenset({"region", "segment", "income_band"})

K_SUPPRESSION_NOTE = (
    f"note: some groups were withheld because each covers fewer than {K_MIN} distinct customers"
)
T5_SUPPRESSION_NOTE = (
    "note: some protected-class cells were withheld because each covers "
    f"fewer than {T5_MIN_CELL} distinct customers"
)


class FloorOutcome:
    """What the floors stage decided for one rewritten statement.

    Refusals are raised as policy refusals instead of stored here. `rows` is left None
    when the caller should execute the (possibly modified) tree itself; the sanctioned
    path fills `rows` directly because the model's statement is never executed for it.
    """

    def __init__(self) -> None:
        self.rows: list[dict] | None = None
        self.sql_executed: str | None = None
        self.rewrites: list[str] = []
        self.notes: list[str] = []
        self.modified: bool = False


# ---------------------------------------------------------------- scope helpers


def _scope_map(tree: exp.Expression) -> dict[int, object]:
    root = build_scope(tree)
    return {id(scope.expression): scope for scope in root.traverse()}


def _scope_grain(scope, scopes: dict[int, object], visiting: set[int] | None = None) -> str:
    """'business', 'customer', or 'none' — the grain of the rows this scope reads.

    A business table anywhere in the scope's sources (or inside their derived tables)
    makes the scope business-grained: aggregating over rows that fan customer attributes
    out through transactions/alerts is a business metric, exempt from the customer
    floors even when the statement also joins customers for row scope. Self-referential
    recursive CTEs contribute nothing (their generator rows come from no table).
    """
    if visiting is None:
        visiting = set()
    if id(scope.expression) in visiting:
        return "none"
    visiting = visiting | {id(scope.expression)}
    grain = "none"
    for source in scope.sources.values():
        if isinstance(source, exp.Table):
            name = source.name.lower()
            if name in policy.BUSINESS_TABLES:
                return "business"
            if name == "customers":
                grain = "customer"
            continue
        inner = getattr(source, "expression", None)
        inner_scope = scopes.get(id(inner)) if inner is not None else None
        if inner_scope is None:
            continue
        inner_grain = _scope_grain(inner_scope, scopes, visiting)
        if inner_grain == "business":
            return "business"
        if inner_grain == "customer":
            grain = "customer"
    return grain


def _aggregate_hosts(tree: exp.Expression) -> list[exp.Select]:
    """Distinct Select nodes hosting an aggregate function of their own.

    An aggregate inside a scalar subquery belongs to the subquery's Select, not the
    enclosing one — the nearest enclosing Select is the host.
    """
    hosts: dict[int, exp.Select] = {}
    for agg in tree.find_all(exp.AggFunc):
        node = agg.parent
        while node is not None and not isinstance(node, exp.Select):
            node = node.parent
        if node is not None:
            hosts[id(node)] = node
    return list(hosts.values())


def _customer_id_reference(
    select_node: exp.Expression, scopes: dict[int, object], add: bool = False, depth: int = 0
) -> str | None:
    """A SQL reference to customers.customer_id visible from this scope, or None.

    The row-scope wrap always projects customer_id, so the common shape (aggregating
    directly over the wrapped relation) resolves immediately. For derived tables and
    CTEs the search recurses and — with add=True, on probe copies only — adds
    customer_id to a single derived source's projection so the count can climb one
    level. Grouped derived sources are never extended; the caller fails closed instead.
    """
    if depth > 8 or not isinstance(select_node, exp.Select):
        return None
    scope = scopes.get(id(select_node))
    if scope is None:
        return None
    for name, source in scope.sources.items():
        if isinstance(source, exp.Table):
            if source.name.lower() == "customers":
                return f"{name}.customer_id" if name else "customer_id"
            continue
        inner = getattr(source, "expression", None)
        if inner is None or not isinstance(inner, exp.Select):
            continue
        deeper = _customer_id_reference(inner, scopes, add=add, depth=depth + 1)
        if deeper is None:
            continue
        exposed = None
        for projection in inner.expressions:
            column = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(column, exp.Column) and column.name.lower() == "customer_id":
                exposed = projection.alias if isinstance(projection, exp.Alias) else "customer_id"
                break
        if exposed is None and add:
            if inner.args.get("group") is not None:
                continue  # extending a grouped projection is not semantics-preserving
            inner.set("expressions", list(inner.expressions) + [sqlglot.parse_one(deeper, dialect="sqlite")])
            exposed = "customer_id"
        if exposed is not None:
            return f"{name}.{exposed}"
    return None


def _count_distinct_node(reference: str) -> exp.Expression:
    try:
        return sqlglot.parse_one(f"COUNT(DISTINCT {reference})", dialect="sqlite")
    except sqlglot.errors.SqlglotError:
        raise policy._refuse(
            policy.FLOOR_K_ANONYMITY,
            "the aggregate's customer count could not be verified for the k-anonymity floor",
        )


def _strip_result_shape(select_node: exp.Select, keep_group: bool) -> None:
    """Drop output-shaping clauses from a probe copy; row selection (WHERE) is kept."""
    select_node.set("having", None)
    select_node.set("order", None)
    select_node.set("limit", None)
    if not keep_group:
        select_node.set("group", None)


def _execute_probe(sql: str, params: dict[str, str]) -> list[dict]:
    try:
        rows, _ = db.execute_readonly(
            sql,
            params=params,
            timeout_seconds=policy.STATEMENT_TIMEOUT_SECONDS,
            row_cap=policy.ROW_CAP,
        )
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "interrupt" in message:
            raise policy._refuse(policy.TIMEOUT, "statement exceeded the allowed execution time")
        raise policy._refuse(policy.PARSE, "query rejected at execution")
    return rows


# ---------------------------------------------------------------- k=2 floor


def _k_floor(tree: exp.Expression, scopes: dict[int, object], params: dict[str, str]) -> FloorOutcome:
    outcome = FloorOutcome()
    customer_hosts = [
        host
        for host in _aggregate_hosts(tree)
        if _scope_grain(scopes[id(host)], scopes) == "customer"
    ]
    if not customer_hosts:
        return outcome  # row-grain, business-grain, or non-customer statement: exempt

    for index, host in enumerate(customer_hosts):
        probe_tree = tree.copy()  # fresh copy per host: probes never share mutations
        probe_scopes = _scope_map(probe_tree)
        probe_hosts = [
            candidate
            for candidate in _aggregate_hosts(probe_tree)
            if _scope_grain(probe_scopes[id(candidate)], probe_scopes) == "customer"
        ]
        probe_host = probe_hosts[index]
        reference = _customer_id_reference(probe_host, probe_scopes, add=True)
        if reference is None:
            raise policy._refuse(
                policy.FLOOR_K_ANONYMITY,
                "the aggregate's customer count could not be verified for the k-anonymity floor",
            )

        group = probe_host.args.get("group")
        if group is not None and group.expressions:
            keys = [key.copy() for key in group.expressions]
            probe_host.set("expressions", keys + [exp.Alias(this=_count_distinct_node(reference), alias=exp.to_identifier("_k"))])
            _strip_result_shape(probe_host, keep_group=True)
            cells = _execute_probe(probe_tree.sql(dialect="sqlite"), params)
            if not cells:
                continue  # no groups in scope: an honest empty result, nothing to floor
            if all(cell["_k"] < K_MIN for cell in cells):
                raise policy._refuse(
                    policy.FLOOR_K_ANONYMITY,
                    f"every group resolves to fewer than {K_MIN} distinct customers",
                )
            if any(cell["_k"] < K_MIN for cell in cells):
                # Re-verify the reference against the real tree, then drop sub-floor
                # groups at execution time. The note stays generic: no sizes, no names.
                stable = _customer_id_reference(host, scopes, add=False)
                if stable is None:
                    raise policy._refuse(
                        policy.FLOOR_K_ANONYMITY,
                        "per-group customer counts could not be enforced on this shape",
                    )
                predicate = sqlglot.parse_one(f"COUNT(DISTINCT {stable}) >= {K_MIN}", dialect="sqlite")
                existing = host.args.get("having")
                condition = (
                    exp.And(this=existing.this, expression=predicate) if existing is not None else predicate
                )
                host.set("having", exp.Having(this=condition))
                outcome.modified = True
                outcome.rewrites.append("group_k_floor")
                outcome.notes.append(K_SUPPRESSION_NOTE)
        else:
            probe_host.set("expressions", [exp.Alias(this=_count_distinct_node(reference), alias=exp.to_identifier("_k"))])
            _strip_result_shape(probe_host, keep_group=False)
            rows = _execute_probe(probe_tree.sql(dialect="sqlite"), params)
            k = rows[0]["_k"] if rows else 0
            if k < K_MIN:
                raise policy._refuse(
                    policy.FLOOR_K_ANONYMITY,
                    f"the aggregate resolves to {k} distinct customer(s); "
                    f"the k-anonymity floor requires at least {K_MIN}",
                )
    return outcome


# ---------------------------------------------------------------- protected-class rule


def t5_reportable_cells(population: int, cell_sizes: list[int]) -> tuple[list[int], str | None]:
    """Pure protected-class decision: surviving cell sizes plus an optional note.

    Refuses (policy refusal, `floor_protected_class`) when the population is below the
    reporting floor, when every cell is sub-floor, or when suppressing the sub-floor
    cells would leave exactly one of them derivable from the reported rest.
    """
    if population < T5_MIN_POPULATION:
        raise policy._refuse(
            policy.FLOOR_PROTECTED_CLASS,
            f"protected-class breakdowns require a population of at least {T5_MIN_POPULATION} "
            f"distinct customers in scope; {population} in scope",
        )
    suppressed = [size for size in cell_sizes if size < T5_MIN_CELL]
    if cell_sizes and len(suppressed) == len(cell_sizes):
        raise policy._refuse(
            policy.FLOOR_PROTECTED_CLASS,
            f"every protected-class cell is below the reporting floor of {T5_MIN_CELL}",
        )
    if len(suppressed) == 1:
        raise policy._refuse(
            policy.FLOOR_PROTECTED_CLASS,
            "suppressing the sub-floor cell would leave it derivable from the reported "
            "total; the whole breakdown is refused",
        )
    if suppressed:
        return [size for size in cell_sizes if size >= T5_MIN_CELL], T5_SUPPRESSION_NOTE
    return list(cell_sizes), None


def _conjuncts(node: exp.Expression):
    if isinstance(node, exp.And):
        yield from _conjuncts(node.this)
        yield from _conjuncts(node.expression)
    elif isinstance(node, exp.Paren):
        yield from _conjuncts(node.this)
    else:
        yield node


def _sanctioned_filter(conjunct: exp.Expression, resolved_map: dict[int, tuple[str, str]]) -> None:
    """Filters may only constrain region/segment/income_band against literal values.

    Protected-class attributes group but never filter: a T5-filtered count is a
    single-cell breakdown that would bypass the population and cell floors.
    """
    column = None
    if isinstance(conjunct, (exp.EQ, exp.NEQ, exp.GT, exp.LT, exp.GTE, exp.LTE)):
        left, right = conjunct.this, conjunct.expression
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
            column = left
        elif isinstance(right, exp.Column) and isinstance(left, exp.Literal):
            column = right
    elif isinstance(conjunct, exp.Is) and isinstance(conjunct.this, exp.Column):
        if isinstance(conjunct.expression, exp.Null) and conjunct.this.name.lower() == "deleted_at":
            return  # redundant with the row scope itself; nothing is disclosed
    elif isinstance(conjunct, exp.In):
        if conjunct.args.get("query") is not None or not all(
            isinstance(value, exp.Literal) for value in conjunct.expressions
        ):
            column = None
        else:
            column = conjunct.this
    if not isinstance(column, exp.Column):
        raise policy._refuse(
            policy.AGGREGATE_ONLY,
            "sanctioned filters compare region, segment or income_band against literal values",
        )
    resolution = resolved_map.get(id(column))
    if resolution is None or resolution[0] != "customers" or resolution[1] not in SANCTIONED_FILTER_KEYS:
        raise policy._refuse(
            policy.AGGREGATE_ONLY,
            "sanctioned filters are limited to region, segment and income_band",
        )


def _shape_row(
    out_cols: list[tuple[str, str, str | None]], group_keys: list[str], key_values: tuple, count: int
) -> dict:
    lookup = dict(zip(group_keys, key_values))
    row: dict = {}
    for name, kind, key in out_cols:
        row[name] = count if kind == "count" else lookup[key]
    return row


def _sanctioned_path(
    tree: exp.Expression,
    resolved: list[tuple[exp.Column, str, str]],
    params: dict[str, str],
    sql_requested: str,
) -> FloorOutcome:
    """Serve a fair_lending customer-grain statement from server-constructed probes."""
    outcome = FloorOutcome()
    resolved_map = {id(col): (table, column) for col, table, column in resolved}

    def refuse(detail: str) -> None:
        raise policy._refuse(policy.AGGREGATE_ONLY, detail)

    # Shape checks run against the MODEL's statement (re-parsed): the rewritten tree also
    # contains the row-scope wrap's own subqueries, which are not the model's nesting.
    model_tree = sqlglot.parse(sql_requested, dialect="sqlite")[0]
    if not isinstance(model_tree, exp.Select) or len(list(model_tree.find_all(exp.Select))) != 1:
        refuse("the sanctioned path accepts a single plain aggregate statement")
    if tree.args.get("with") is not None or tree.args.get("distinct") is not None:
        refuse("the sanctioned path accepts a single plain aggregate statement")
    if tree.args.get("having") is not None:
        refuse("HAVING is not part of the sanctioned aggregate path")

    out_cols: list[tuple[str, str, str | None]] = []
    counts = 0
    for index, projection in enumerate(tree.expressions):
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        name = projection.alias if isinstance(projection, exp.Alias) else None
        if isinstance(inner, exp.Column):
            resolution = resolved_map.get(id(inner))
            if resolution is None or resolution[0] != "customers" or resolution[1] not in SANCTIONED_GROUPING_KEYS:
                refuse(
                    "sanctioned outputs are grouping keys (region, segment, income_band, "
                    "protected-class attributes) and COUNT(*)"
                )
            out_cols.append((name or resolution[1], "key", resolution[1]))
        elif isinstance(inner, exp.Count) and isinstance(inner.this, exp.Star):
            out_cols.append((name or f"_col_{index}", "count", None))
            counts += 1
        else:
            refuse("only COUNT(*) aggregates are sanctioned over customer data")
    if counts == 0:
        refuse("customer data for role 'fair_lending' is aggregate-only through the sanctioned path")

    group = tree.args.get("group")
    group_keys: list[str] = []
    if group is not None and group.expressions:
        for key_expr in group.expressions:
            resolution = resolved_map.get(id(key_expr)) if isinstance(key_expr, exp.Column) else None
            if resolution is None or resolution[0] != "customers" or resolution[1] not in SANCTIONED_GROUPING_KEYS:
                refuse(
                    "grouping keys are limited to region, segment, income_band and "
                    "the protected-class attributes"
                )
            group_keys.append(resolution[1])
        if any(kind == "key" and key not in group_keys for _, kind, key in out_cols):
            refuse("projected grouping keys must appear in GROUP BY")
    elif any(kind == "key" for _, kind, _ in out_cols):
        refuse("row-grain projection is not sanctioned; aggregate with GROUP BY instead")

    where = tree.args.get("where")
    if where is not None:
        for conjunct in _conjuncts(where.this):
            _sanctioned_filter(conjunct, resolved_map)

    # Population: distinct customers in the filtered scope, grouping ignored.
    pop_tree = tree.copy()
    pop_reference = _customer_id_reference(pop_tree, _scope_map(pop_tree), add=True)
    if pop_reference is None:
        raise policy._refuse(
            policy.FLOOR_K_ANONYMITY,
            "the aggregate's customer count could not be verified for the k-anonymity floor",
        )
    pop_tree.set("expressions", [exp.Alias(this=_count_distinct_node(pop_reference), alias=exp.to_identifier("_pop"))])
    _strip_result_shape(pop_tree, keep_group=False)
    pop_rows = _execute_probe(pop_tree.sql(dialect="sqlite"), params)
    population = pop_rows[0]["_pop"] if pop_rows else 0

    if not group_keys:
        # No grouping means no protected-class breakdown (T5 attributes may only group);
        # the single cell is guarded by the k floor alone.
        if population < K_MIN:
            raise policy._refuse(
                policy.FLOOR_K_ANONYMITY,
                f"the aggregate resolves to {population} distinct customer(s); "
                f"the k-anonymity floor requires at least {K_MIN}",
            )
        outcome.rows = [_shape_row(out_cols, [], (), population)]
        outcome.sql_executed = pop_tree.sql(dialect="sqlite")
        outcome.rewrites.append("sanctioned_aggregate_path")
        return outcome

    # Cells: per-group distinct customer counts from a server-constructed probe.
    cells_tree = tree.copy()
    cells_reference = _customer_id_reference(cells_tree, _scope_map(cells_tree), add=True)
    if cells_reference is None:
        raise policy._refuse(
            policy.FLOOR_K_ANONYMITY,
            "the aggregate's customer count could not be verified for the k-anonymity floor",
        )
    cells_group = cells_tree.args["group"]
    keys = [key.copy() for key in cells_group.expressions]
    cells_tree.set(
        "expressions",
        keys + [exp.Alias(this=_count_distinct_node(cells_reference), alias=exp.to_identifier("_k"))],
    )
    _strip_result_shape(cells_tree, keep_group=True)
    cells_sql = cells_tree.sql(dialect="sqlite")
    cell_rows = _execute_probe(cells_sql, params)
    cells = [
        (tuple(row[key] for key in group_keys), row["_k"])
        for row in cell_rows
    ]
    cells.sort(key=lambda item: tuple(str(value) for value in item[0]))

    if any(key in T5_ATTRIBUTES for key in group_keys):
        _, note = t5_reportable_cells(population, [k for _, k in cells])
        surviving = [item for item in cells if item[1] >= T5_MIN_CELL]
        if note is not None:
            outcome.notes.append(note)
    else:
        sub_floor = [keys_tuple for keys_tuple, k in cells if k < K_MIN]
        if cells and len(sub_floor) == len(cells):
            raise policy._refuse(
                policy.FLOOR_K_ANONYMITY,
                f"every group resolves to fewer than {K_MIN} distinct customers",
            )
        surviving = [item for item in cells if item[1] >= K_MIN]
        if sub_floor:
            outcome.notes.append(K_SUPPRESSION_NOTE)

    outcome.rows = [_shape_row(out_cols, group_keys, keys_tuple, k) for keys_tuple, k in surviving]
    outcome.sql_executed = cells_sql
    outcome.rewrites.append("sanctioned_aggregate_path")
    return outcome


# ---------------------------------------------------------------- entry point


def _customer_grained(tree: exp.Expression, scopes: dict[int, object]) -> bool:
    top = scopes.get(id(tree))
    if top is not None and _scope_grain(top, scopes) == "customer":
        return True
    return any(
        _scope_grain(scopes[id(host)], scopes) == "customer" for host in _aggregate_hosts(tree)
    )


def enforce(
    tree: exp.Expression,
    resolved: list[tuple[exp.Column, str, str]],
    role: str,
    params: dict[str, str],
    sql_requested: str,
) -> FloorOutcome:
    """Run the floors over one rewritten statement; refusals raise policy refusals."""
    scopes = _scope_map(tree)
    if role == "fair_lending" and _customer_grained(tree, scopes):
        return _sanctioned_path(tree, resolved, params, sql_requested)
    if role == "fair_lending":
        return FloorOutcome()  # business-grain metrics: the normal pipeline applies
    return _k_floor(tree, scopes, params)
