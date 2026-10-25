"""The catalog queries of a PostgreSQL dump (`dump.py`). Each reads every object of its kind in the scope at once —
a dump sends a fixed number of queries whatever the number of objects (pre-flight P5). `%(ddl)s` False (a dump
plan) reads which objects there are without their definitions. Pure text."""

FIRST_USER_OID = 16384  # FirstNormalObjectId: what initdb made (pg_catalog, information_schema) is below


def not_extension(catalog: str, oid: str) -> str:
    """The object is not an extension's (deptype e)."""
    return (f"NOT EXISTS (SELECT 1 FROM pg_depend e WHERE e.classid = '{catalog}'::regclass AND e.objid = {oid} "
            "AND e.deptype = 'e')")


SCHEMAS = ("SELECT n.oid, n.nspname::text FROM pg_namespace n WHERE n.nspname NOT LIKE 'pg\\_%%' "
           "AND n.nspname <> 'information_schema' ORDER BY n.nspname")
SCHEMA = "SELECT n.oid, n.nspname::text FROM pg_namespace n WHERE n.nspname = %(name)s"
# `bypass`: this login reads every row whatever the policies say (superuser, BYPASSRLS, an owner without FORCE)
RELATIONS = f"""
SELECT c.oid, n.nspname::text, c.relname::text, c.relkind::text, c.relispartition,
       CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END,
       CASE WHEN c.relkind IN ('r', 'm') THEN pg_total_relation_size(c.oid) END,
       c.relrowsecurity, c.relforcerowsecurity, c.reloptions, c.relispopulated,
       (SELECT r.rolsuper OR r.rolbypassrls FROM pg_roles r WHERE r.rolname = current_user)
         OR (pg_has_role(c.relowner, 'USAGE') AND NOT c.relforcerowsecurity),
       c.relreplident::text,
       (SELECT x.relname::text FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid
        WHERE i.indrelid = c.oid AND i.indisreplident),
       (SELECT x.relname::text FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid
        WHERE i.indrelid = c.oid AND i.indisclustered),
       c.reloftype <> 0
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relnamespace = ANY(%(schemas)s) AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
  AND {not_extension("pg_class", "c.oid")}
ORDER BY c.oid
"""
# Partitions and inheritance children of the scope's relations, wherever they live, and their parents
INHERITS = """
SELECT h.inhrelid, h.inhparent, c.relispartition FROM pg_inherits h JOIN pg_class c ON c.oid = h.inhrelid
WHERE c.relkind IN ('r', 'p', 'f') AND (h.inhparent = ANY(%(relations)s) OR h.inhrelid = ANY(%(relations)s))
"""
# A sequence a column owns: a serial's (deptype a) or an identity's (i, which the table's DDL creates itself)
OWNED_SEQUENCES = """
SELECT d.objid, d.deptype::text, d.refobjid, a.attname::text
FROM pg_depend d JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
WHERE d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
  AND d.objid IN (SELECT oid FROM pg_class WHERE relkind = 'S') AND d.refobjid = ANY(%(tables)s)
"""
ANY_OWNED = ("SELECT objid FROM pg_depend WHERE classid = 'pg_class'::regclass AND deptype IN ('a', 'i') "
             "AND objid = ANY(%(oids)s)")
# Enums, domains, standalone composites and range types, and (to be counted as left out) base types; never an array
# type, a multirange (its range makes it), nor a relation's row type
TYPES = f"""
SELECT t.oid, n.nspname::text, t.typname::text, t.typtype::text
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE t.typnamespace = ANY(%(schemas)s) AND t.typisdefined AND {not_extension("pg_type", "t.oid")}
  AND (t.typtype IN ('e', 'd', 'r') OR (t.typtype = 'b' AND t.typelem = 0)
       OR (t.typtype = 'c' AND (SELECT relkind FROM pg_class WHERE oid = t.typrelid) = 'c'))
ORDER BY t.typtype <> 'e', t.oid
"""
# Every type of the scope's schemas, to read a dependency on an array type as one on its element, on a multirange as
# one on its range (PostgreSQL 14+), and on a row type as one on its relation
TYPE_LINKS = """
SELECT t.oid, CASE WHEN t.typcategory = 'A' THEN t.typelem{multirange} ELSE 0 END, t.typrelid
FROM pg_type t WHERE t.typnamespace = ANY(%(schemas)s)
"""
MULTIRANGE_LINK = ("\n                   WHEN t.typtype = 'm' THEN COALESCE((SELECT r.rngtypid FROM pg_range r "
                   "WHERE r.rngmultitypid = t.oid), 0)")
# Never a routine another object made for itself (deptype i: a range type's constructors)
ROUTINES = f"""
SELECT p.oid, n.nspname::text, p.proname::text, p.prokind::text, pg_get_function_identity_arguments(p.oid)
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.pronamespace = ANY(%(schemas)s) AND {not_extension("pg_proc", "p.oid")}
  AND NOT EXISTS (SELECT 1 FROM pg_depend i WHERE i.classid = 'pg_proc'::regclass AND i.objid = p.oid AND i.deptype = 'i')
ORDER BY p.oid
"""
# User collations (CREATE COLLATION): their columns differ between versions, so the row comes as JSON
COLLATIONS = f"""
SELECT c.oid, n.nspname::text, c.collname::text, to_jsonb(c) FROM pg_collation c
JOIN pg_namespace n ON n.oid = c.collnamespace
WHERE c.collnamespace = ANY(%(schemas)s) AND c.oid >= {FIRST_USER_OID} AND {not_extension("pg_collation", "c.oid")}
ORDER BY c.oid
"""
# The objects of the scope's schemas no dump writes (pre-flight H6, P1): counted for the header, and named where
# something the dump writes uses one
OTHER_KINDS = (  # (plural, singular, catalog, namespace column)
    ("operators", "operator", "pg_operator", "oprnamespace"),
    ("operator classes", "operator class", "pg_opclass", "opcnamespace"),
    ("operator families", "operator family", "pg_opfamily", "opfnamespace"),
    ("conversions", "conversion", "pg_conversion", "connamespace"),
    ("text search configurations", "text search configuration", "pg_ts_config", "cfgnamespace"),
    ("text search dictionaries", "text search dictionary", "pg_ts_dict", "dictnamespace"),
    ("text search parsers", "text search parser", "pg_ts_parser", "prsnamespace"),
    ("text search templates", "text search template", "pg_ts_template", "tmplnamespace"),
)
OTHERS = "\nUNION ALL\n".join(
    f"SELECT {i}, o.oid, (pg_identify_object('{catalog}'::regclass, o.oid, 0)).identity FROM {catalog} o "
    f"WHERE o.{column} = ANY(%(schemas)s) AND {not_extension(catalog, 'o.oid')}"
    for i, (_, _, catalog, column) in enumerate(OTHER_KINDS))
# What an object of the scope uses (deptype n; the rest are an object's own parts), each object read as the one a
# dump writes: a column, a default, a constraint, a rule or an index as its relation or domain, a composite's relation
# as its type, a trigger as itself. `phase`: create — needed when the owner is created; index — by its relation's
# index; later — by its foreign key, policy, statistics, rule or trigger (written after everything they could use).
# `part`: which of a table's columns (a:), defaults (d:) or constraints (k:) it is
DEPENDS = """
SELECT CASE WHEN c.relkind = 'c' OR k.contypid <> 0 OR d.classid = 'pg_type'::regclass THEN 't'
            WHEN c.relkind = 'S' THEN 's' WHEN d.classid = 'pg_proc'::regclass THEN 'f'
            WHEN g.oid IS NOT NULL THEN 'g' ELSE 'r' END,
       CASE WHEN c.relkind = 'c' THEN c.reltype WHEN c.relkind IN ('i', 'I') THEN i.indrelid
            WHEN c.oid IS NOT NULL OR d.classid IN ('pg_type'::regclass, 'pg_proc'::regclass) OR g.oid IS NOT NULL
              THEN d.objid
            WHEN a.oid IS NOT NULL THEN a.adrelid WHEN k.contypid <> 0 THEN k.contypid WHEN k.oid IS NOT NULL THEN k.conrelid
            WHEN w.oid IS NOT NULL THEN w.ev_class WHEN p.oid IS NOT NULL THEN p.polrelid ELSE s.stxrelid END,
       CASE WHEN c.relkind IN ('i', 'I') THEN 'index'
            WHEN p.oid IS NOT NULL OR s.oid IS NOT NULL OR k.contype = 'f' OR w.rulename <> '_RETURN'
                 OR g.oid IS NOT NULL THEN 'later'
            ELSE 'create' END,
       CASE WHEN d.refclassid = 'pg_class'::regclass THEN 'c' WHEN d.refclassid = 'pg_type'::regclass THEN 't'
            WHEN d.refclassid = 'pg_proc'::regclass THEN 'f' WHEN d.refclassid = 'pg_collation'::regclass THEN 'C'
            ELSE 'x' END,
       d.refobjid,
       CASE WHEN c.relkind IN ('r', 'p') AND d.objsubid > 0
              THEN 'a:' || (SELECT attname FROM pg_attribute WHERE attrelid = d.objid AND attnum = d.objsubid)
            WHEN a.oid IS NOT NULL
              THEN 'd:' || (SELECT attname FROM pg_attribute WHERE attrelid = a.adrelid AND attnum = a.adnum)
            WHEN k.conrelid <> 0 THEN 'k:' || k.conname END
FROM pg_depend d
LEFT JOIN pg_class c ON d.classid = 'pg_class'::regclass AND c.oid = d.objid
LEFT JOIN pg_index i ON c.relkind IN ('i', 'I') AND i.indexrelid = c.oid
LEFT JOIN pg_attrdef a ON d.classid = 'pg_attrdef'::regclass AND a.oid = d.objid
LEFT JOIN pg_constraint k ON d.classid = 'pg_constraint'::regclass AND k.oid = d.objid
LEFT JOIN pg_rewrite w ON d.classid = 'pg_rewrite'::regclass AND w.oid = d.objid
LEFT JOIN pg_policy p ON d.classid = 'pg_policy'::regclass AND p.oid = d.objid
LEFT JOIN pg_statistic_ext s ON d.classid = 'pg_statistic_ext'::regclass AND s.oid = d.objid
LEFT JOIN pg_trigger g ON d.classid = 'pg_trigger'::regclass AND g.oid = d.objid
WHERE d.deptype = 'n' AND d.refobjid = ANY(%(refs)s)
  AND d.refclassid IN ('pg_class'::regclass, 'pg_type'::regclass, 'pg_proc'::regclass, 'pg_collation'::regclass,
                       'pg_operator'::regclass, 'pg_opclass'::regclass, 'pg_opfamily'::regclass,
                       'pg_ts_config'::regclass, 'pg_ts_dict'::regclass, 'pg_ts_parser'::regclass,
                       'pg_ts_template'::regclass)
  AND d.classid IN ('pg_class'::regclass, 'pg_type'::regclass, 'pg_proc'::regclass, 'pg_attrdef'::regclass,
                    'pg_constraint'::regclass, 'pg_rewrite'::regclass, 'pg_policy'::regclass,
                    'pg_statistic_ext'::regclass, 'pg_trigger'::regclass)
"""
EXTENSIONS = """
SELECT e.extname::text, n.nspname::text FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
WHERE e.extnamespace = ANY(%(schemas)s) AND e.extname <> 'plpgsql' ORDER BY e.oid
"""
EXTENSION_OBJECTS = """
SELECT count(*) FROM pg_depend d WHERE d.deptype = 'e' AND (
    (d.classid = 'pg_class'::regclass AND d.objid IN (SELECT oid FROM pg_class WHERE relnamespace = ANY(%(schemas)s)))
 OR (d.classid = 'pg_proc'::regclass AND d.objid IN (SELECT oid FROM pg_proc WHERE pronamespace = ANY(%(schemas)s)))
 OR (d.classid = 'pg_type'::regclass
     AND d.objid IN (SELECT oid FROM pg_type WHERE typnamespace = ANY(%(schemas)s) AND typelem = 0)))
"""
# What the dump's objects use that it does not create (pre-flight L6, H6, P4): a type, function or sequence of an
# extension not in the dump, or a type, function, sequence, table or view of a schema not in it. The users: the
# relations' columns, defaults, checks, indexes, view queries, policies and statistics, the triggers written, the
# routines' signatures and bodies, the domains' base types and checks; an array type counts as its element. A table
# or view is counted only where a view, routine, default or policy reads it (a foreign key to one is said apart, an
# inheritance parent is not needed)
USED_ELSEWHERE = f"""
WITH used AS (
    SELECT DISTINCT x.refclassid, COALESCE((SELECT NULLIF(t.typelem, 0) FROM pg_type t
                                            WHERE x.refclassid = 'pg_type'::regclass AND t.oid = x.refobjid
                                              AND t.typcategory = 'A'), x.refobjid) AS oid,
           CASE WHEN x.refclassid = 'pg_class'::regclass
                THEN (SELECT relkind::text FROM pg_class WHERE oid = x.refobjid) END AS relkind
    FROM pg_depend x
    WHERE x.deptype = 'n' AND x.refobjid >= {FIRST_USER_OID}
      AND x.refclassid IN ('pg_type'::regclass, 'pg_proc'::regclass, 'pg_class'::regclass)
      AND ((x.classid = 'pg_class'::regclass
            AND (x.objid = ANY(%(relations)s)
                 OR x.objid IN (SELECT indexrelid FROM pg_index WHERE indrelid = ANY(%(relations)s))))
        OR (x.classid = 'pg_attrdef'::regclass
            AND x.objid IN (SELECT oid FROM pg_attrdef WHERE adrelid = ANY(%(relations)s)))
        OR (x.classid = 'pg_constraint'::regclass AND x.objid IN (
            SELECT oid FROM pg_constraint WHERE (conrelid = ANY(%(relations)s) AND contype <> 'f')
                                             OR contypid = ANY(%(types)s)))
        OR (x.classid = 'pg_rewrite'::regclass
            AND x.objid IN (SELECT oid FROM pg_rewrite WHERE ev_class = ANY(%(relations)s)))
        OR (x.classid = 'pg_proc'::regclass AND x.objid = ANY(%(routines)s))
        OR (x.classid = 'pg_type'::regclass AND x.objid = ANY(%(types)s))
        OR (x.classid = 'pg_trigger'::regclass AND x.objid = ANY(%(triggers)s))
        OR (x.classid = 'pg_policy'::regclass AND x.objid IN (SELECT oid FROM pg_policy WHERE polrelid = ANY(%(relations)s)))
        OR (x.classid = 'pg_statistic_ext'::regclass
            AND x.objid IN (SELECT oid FROM pg_statistic_ext WHERE stxrelid = ANY(%(relations)s))))
      AND (x.refclassid <> 'pg_class'::regclass OR (SELECT relkind FROM pg_class WHERE oid = x.refobjid) = 'S'
           OR x.classid IN ('pg_rewrite'::regclass, 'pg_proc'::regclass, 'pg_attrdef'::regclass, 'pg_policy'::regclass))
)
SELECT e.extname::text, CASE WHEN u.refclassid = 'pg_type'::regclass THEN 'types'
                             WHEN u.refclassid = 'pg_proc'::regclass THEN 'functions'
                             WHEN u.relkind = 'S' THEN 'sequences' ELSE 'tables and views' END, count(*)
FROM used u
LEFT JOIN pg_depend m ON m.classid = u.refclassid AND m.objid = u.oid AND m.deptype = 'e'
LEFT JOIN pg_extension e ON e.oid = m.refobjid
WHERE CASE WHEN e.oid IS NULL THEN u.oid <> ALL(%(scope)s) ELSE e.extname <> ALL(%(extensions)s) END
GROUP BY 1, 2 ORDER BY 1 NULLS LAST, 2 DESC
"""
# Indexes no constraint owns; a partition's index attached to its parent's is made again by the parent's, written ON
# (not ON ONLY) so that it reaches every partition
INDEXES = """
SELECT i.indexrelid, i.indrelid, n.nspname::text, c.relname::text,
       CASE WHEN %(ddl)s THEN pg_get_indexdef(i.indexrelid) END
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE i.indrelid = ANY(%(relations)s)
  AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid AND k.conrelid = i.indrelid)
  AND NOT EXISTS (SELECT 1 FROM pg_inherits h WHERE h.inhrelid = i.indexrelid)
ORDER BY i.indexrelid
"""
# The indexes the tables' keys and exclusion constraints own: only their comments are written (P4)
CONSTRAINT_INDEXES = """
SELECT k.conrelid, k.conindid, n.nspname::text, c.relname::text FROM pg_constraint k
JOIN pg_class c ON c.oid = k.conindid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE k.conrelid = ANY(%(tables)s) AND k.conindid <> 0 AND k.contype IN ('p', 'u', 'x')
"""
# A partition's copy of its parent's foreign key has a parent (conparentid): the parent's makes it again
FOREIGN_KEYS = """
SELECT k.oid, k.conrelid, k.confrelid, k.conname::text, CASE WHEN %(ddl)s THEN pg_get_constraintdef(k.oid) END,
       k.confrelid::regclass::text
FROM pg_constraint k WHERE k.contype = 'f' AND k.conrelid = ANY(%(tables)s) AND k.conparentid = 0 ORDER BY k.oid
"""
# NOT VALID constraints: written inline they would be validated against rows that may break them (H6); a partition's
# own only (its parent's come with PARTITION OF)
NOT_VALID = """
SELECT k.oid, k.conrelid, k.conname::text, CASE WHEN %(ddl)s THEN pg_get_constraintdef(k.oid) END
FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid
WHERE k.contype = 'c' AND NOT k.convalidated AND k.conrelid = ANY(%(tables)s)
  AND (NOT c.relispartition OR (k.conislocal AND k.coninhcount = 0))
ORDER BY k.oid
"""
DOMAIN_NOT_VALID = """
SELECT k.oid, k.contypid, k.conname::text FROM pg_constraint k
WHERE k.contype = 'c' AND NOT k.convalidated AND k.contypid = ANY(%(types)s) ORDER BY k.oid
"""
# A partition's copy of its parent's trigger has a parent (tgparentid, 13+): the parent's makes it again
TRIGGERS = """
SELECT t.oid, t.tgrelid, t.tgname::text, t.tgfoid, t.tgenabled::text,
       CASE WHEN %(ddl)s THEN pg_get_triggerdef(t.oid, false) END
FROM pg_trigger t WHERE NOT t.tgisinternal AND t.tgrelid = ANY(%(relations)s){parent} ORDER BY t.oid
"""
# A partition's copy of a trigger whose state is not its parent's (ALTER TABLE <partition> DISABLE TRIGGER, P4)
TRIGGER_STATES = """
SELECT t.tgrelid, t.tgname::text, t.tgenabled::text FROM pg_trigger t JOIN pg_trigger p ON p.oid = t.tgparentid
WHERE t.tgrelid = ANY(%(tables)s) AND t.tgenabled <> p.tgenabled ORDER BY t.oid
"""
RULES = """
SELECT r.oid, r.ev_class, r.rulename::text, r.ev_enabled::text, CASE WHEN %(ddl)s THEN pg_get_ruledef(r.oid) END
FROM pg_rewrite r WHERE r.ev_class = ANY(%(relations)s) AND r.rulename <> '_RETURN' ORDER BY r.oid
"""
STATISTICS = """
SELECT s.oid, s.stxrelid, n.nspname::text, s.stxname::text, CASE WHEN %(ddl)s THEN pg_get_statisticsobjdef(s.oid) END,
       {target}
FROM pg_statistic_ext s JOIN pg_namespace n ON n.oid = s.stxnamespace
WHERE s.stxrelid = ANY(%(relations)s) ORDER BY s.oid
"""
STATISTICS_TARGET = "CASE WHEN s.stxstattarget >= 0 THEN s.stxstattarget END"  # 13+
POLICIES = """
SELECT p.oid, p.polrelid, p.polname::text, p.polcmd::text, p.polpermissive, 0 = ANY(p.polroles),
       ARRAY(SELECT r.rolname::text FROM unnest(p.polroles) WITH ORDINALITY u (oid, i)
             JOIN pg_roles r ON r.oid = u.oid ORDER BY u.i),
       pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid)
FROM pg_policy p WHERE p.polrelid = ANY(%(tables)s) ORDER BY p.oid
"""
COMMENTS = f"""
SELECT (SELECT relname FROM pg_class WHERE oid = d.classoid)::text, d.objoid, d.objsubid,
       (SELECT attname FROM pg_attribute WHERE attrelid = d.objoid AND attnum = d.objsubid AND d.objsubid > 0)::text,
       d.description
FROM pg_description d WHERE d.objoid >= {FIRST_USER_OID}
"""
FUNCTION_DEFINITIONS = "SELECT p.oid, pg_get_functiondef(p.oid) FROM pg_proc p WHERE p.oid = ANY(%(oids)s)"
# Non-pretty (P6): the pretty mode's dropped parentheses may not read back the same
VIEW_DEFINITIONS = "SELECT c.oid, pg_get_viewdef(c.oid, false) FROM pg_class c WHERE c.oid = ANY(%(oids)s)"
VIEW_DEFAULTS = """
SELECT d.adrelid, a.attname::text, pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d
JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
WHERE d.adrelid = ANY(%(views)s) ORDER BY d.adrelid, a.attnum
"""
AGGREGATES = """
SELECT a.aggfnoid::oid, a.aggkind::text, a.aggtransfn::regproc::text, format_type(a.aggtranstype, NULL), a.aggtransspace,
       NULLIF(a.aggfinalfn::regproc::text, '-'), a.aggfinalextra, a.aggfinalmodify::text,
       NULLIF(a.aggcombinefn::regproc::text, '-'), NULLIF(a.aggserialfn::regproc::text, '-'),
       NULLIF(a.aggdeserialfn::regproc::text, '-'), a.agginitval,
       NULLIF(a.aggmtransfn::regproc::text, '-'), NULLIF(a.aggminvtransfn::regproc::text, '-'),
       CASE WHEN a.aggmtranstype <> 0 THEN format_type(a.aggmtranstype, NULL) END, a.aggmtransspace,
       NULLIF(a.aggmfinalfn::regproc::text, '-'), a.aggmfinalextra, a.aggmfinalmodify::text, a.aggminitval,
       CASE WHEN a.aggsortop <> 0 THEN ARRAY[(SELECT nspname::text FROM pg_namespace WHERE oid = o.oprnamespace),
                                             o.oprname::text] END,
       p.proparallel::text
FROM pg_aggregate a JOIN pg_proc p ON p.oid = a.aggfnoid LEFT JOIN pg_operator o ON o.oid = a.aggsortop
WHERE a.aggfnoid::oid = ANY(%(oids)s)
"""
DATABASE_OBJECTS = ("event triggers", "publications", "large objects", "casts")
# A whole database's objects that belong to no schema (H6)
DATABASE_COUNTS = f"""
SELECT (SELECT count(*) FROM pg_event_trigger o WHERE {not_extension("pg_event_trigger", "o.oid")}),
       (SELECT count(*) FROM pg_publication), (SELECT count(*) FROM pg_largeobject_metadata),
       (SELECT count(*) FROM pg_cast o WHERE o.oid >= {FIRST_USER_OID}
          AND {not_extension("pg_cast", "o.oid")}
          AND NOT EXISTS (SELECT 1 FROM pg_depend i WHERE i.classid = 'pg_cast'::regclass AND i.objid = o.oid
                          AND i.deptype = 'i'))  -- not a range's own cast to its multirange
"""
# What would stop a `drop_first` file's DROP … IF EXISTS of a relation, routine, type or collation the dump writes
# (06b I2: no dump writes CASCADE, 2BP01): every object that depends on it (deptype n), wherever it is, with what its
# own DROP goes with — the relation it is part of (a column, an index, a view's or another rule, a default, a
# constraint, a trigger, a policy, statistics), a composite's or a domain constraint's type, or itself (a type, a
# routine) — so that the dump tells apart those it drops itself. Named as pg_identify_object names it (a view's
# _RETURN rule and columns as the view); a foreign key and a column default in words of their own. A partition's copy of a foreign
# key or a trigger goes with its parent's.
DROP_BLOCKERS = """
SELECT DISTINCT
       CASE WHEN d.refclassid = 'pg_class'::regclass THEN 'r' WHEN d.refclassid = 'pg_proc'::regclass THEN 'f'
            WHEN d.refclassid = 'pg_type'::regclass THEN 't' ELSE 'k' END,
       d.refobjid,
       CASE WHEN c.relkind = 'c' THEN NULL WHEN c.relkind IN ('i', 'I') THEN i.indrelid WHEN c.oid IS NOT NULL THEN c.oid
            WHEN w.oid IS NOT NULL THEN w.ev_class WHEN a.oid IS NOT NULL THEN a.adrelid
            WHEN k.oid IS NOT NULL THEN NULLIF(k.conrelid, 0) WHEN g.oid IS NOT NULL THEN g.tgrelid
            WHEN p.oid IS NOT NULL THEN p.polrelid WHEN s.oid IS NOT NULL THEN s.stxrelid END,
       CASE WHEN c.relkind = 'c' THEN c.reltype WHEN k.contypid <> 0 THEN k.contypid
            WHEN d.classid = 'pg_type'::regclass THEN d.objid END,
       CASE WHEN d.classid = 'pg_proc'::regclass THEN d.objid END,
       CASE WHEN k.contype = 'f' THEN 'foreign key' WHEN a.oid IS NOT NULL THEN 'column default' ELSE o.type END,
       CASE WHEN a.oid IS NOT NULL THEN regexp_replace(o.identity, '^for ', '') ELSE o.identity END
FROM pg_depend d
LEFT JOIN pg_class c ON d.classid = 'pg_class'::regclass AND c.oid = d.objid
LEFT JOIN pg_index i ON c.relkind IN ('i', 'I') AND i.indexrelid = c.oid
LEFT JOIN pg_rewrite w ON d.classid = 'pg_rewrite'::regclass AND w.oid = d.objid
LEFT JOIN pg_attrdef a ON d.classid = 'pg_attrdef'::regclass AND a.oid = d.objid
LEFT JOIN pg_constraint k ON d.classid = 'pg_constraint'::regclass AND k.oid = d.objid
LEFT JOIN pg_trigger g ON d.classid = 'pg_trigger'::regclass AND g.oid = d.objid
LEFT JOIN pg_policy p ON d.classid = 'pg_policy'::regclass AND p.oid = d.objid
LEFT JOIN pg_statistic_ext s ON d.classid = 'pg_statistic_ext'::regclass AND s.oid = d.objid
CROSS JOIN LATERAL pg_identify_object(CASE WHEN w.rulename = '_RETURN' THEN 'pg_class'::regclass ELSE d.classid END,
                                      CASE WHEN w.rulename = '_RETURN' THEN w.ev_class ELSE d.objid END,
                                      CASE WHEN w.rulename = '_RETURN' OR c.relkind IN ('v', 'm') THEN 0
                                           ELSE d.objsubid END) o
WHERE d.deptype = 'n'
  AND ((d.refclassid = 'pg_class'::regclass AND d.refobjid = ANY(%(relations)s))
    OR (d.refclassid = 'pg_proc'::regclass AND d.refobjid = ANY(%(routines)s))
    OR (d.refclassid = 'pg_type'::regclass AND d.refobjid = ANY(%(types)s))
    OR (d.refclassid = 'pg_collation'::regclass AND d.refobjid = ANY(%(collations)s)))
  AND (k.oid IS NULL OR k.conparentid = 0){triggers}
"""
TRIGGER_NOT_CLONED = " AND (g.oid IS NULL OR g.tgparentid = 0)"  # 13+: a partition's copy of its parent's trigger
