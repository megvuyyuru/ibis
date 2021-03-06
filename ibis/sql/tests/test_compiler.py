# Copyright 2014 Cloudera Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pandas as pd

import ibis

from ibis.sql.compiler import build_ast, to_sql
from ibis.expr.tests.mocks import MockConnection
from ibis.compat import unittest
import ibis.common as com

import ibis.expr.api as api
import ibis.expr.operations as ops
import ibis.sql.ddl as ddl

# We are only testing Impala SQL dialect for the time being. At some point if
# we choose to support more SQL dialects we can refactor the test suite to
# check each supported database.


class TestASTBuilder(unittest.TestCase):

    def setUp(self):
        self.con = MockConnection()

    def test_ast_with_projection_join_filter(self):
        table = self.con.table('test1')
        table2 = self.con.table('test2')

        filter_pred = table['f'] > 0

        table3 = table[filter_pred]

        join_pred = table3['g'] == table2['key']

        joined = table2.inner_join(table3, [join_pred])
        result = joined[[table3, table2['value']]]

        ast = build_ast(result)
        stmt = ast.queries[0]

        def foo():
            table3 = table[filter_pred]
            joined = table2.inner_join(table3, [join_pred])
            result = joined[[table3, table2['value']]]
            return result

        assert len(stmt.select_set) == 2
        assert len(stmt.where) == 1
        assert stmt.where[0] is filter_pred

        # Check that the join has been rebuilt to only include the root tables
        tbl = stmt.table_set
        tbl_node = tbl.op()
        assert isinstance(tbl_node, ops.InnerJoin)
        assert tbl_node.left is table2
        assert tbl_node.right is table

        # table expression substitution has been made in the predicate
        assert tbl_node.predicates[0].equals(table['g'] == table2['key'])

    def test_ast_with_aggregation_join_filter(self):
        table = self.con.table('test1')
        table2 = self.con.table('test2')

        filter_pred = table['f'] > 0
        table3 = table[filter_pred]
        join_pred = table3['g'] == table2['key']

        joined = table2.inner_join(table3, [join_pred])

        met1 = (table3['f'] - table2['value']).mean().name('foo')
        result = joined.aggregate([met1, table3['f'].sum().name('bar')],
                                  by=[table3['g'], table2['key']])

        ast = build_ast(result)
        stmt = ast.queries[0]

        # hoisted metrics
        ex_metrics = [(table['f'] - table2['value']).mean().name('foo'),
                      table['f'].sum().name('bar')]
        ex_by = [table['g'], table2['key']]

        # hoisted join and aggregate
        expected_table_set = \
            table2.inner_join(table, [table['g'] == table2['key']])
        assert stmt.table_set.equals(expected_table_set)

        # Check various exprs
        for res, ex in zip(stmt.select_set, ex_by + ex_metrics):
            assert res.equals(ex)

        for res, ex in zip(stmt.group_by, ex_by):
            assert stmt.select_set[res].equals(ex)

        # Check we got the filter
        assert len(stmt.where) == 1
        assert stmt.where[0].equals(filter_pred)

    def test_ast_non_materialized_join(self):
        pass

    def test_sort_by(self):
        table = self.con.table('star1')

        what = table.sort_by('f')
        result = to_sql(what)
        expected = """SELECT *
FROM star1
ORDER BY `f`"""
        assert result == expected

        what = table.sort_by(('f', 0))
        result = to_sql(what)
        expected = """SELECT *
FROM star1
ORDER BY `f` DESC"""
        assert result == expected

        what = table.sort_by(['c', ('f', 0)])
        result = to_sql(what)
        expected = """SELECT *
FROM star1
ORDER BY `c`, `f` DESC"""
        assert result == expected

    def test_limit(self):
        table = self.con.table('star1').limit(10)
        result = to_sql(table)
        expected = """SELECT *
FROM star1
LIMIT 10"""
        assert result == expected

        table = self.con.table('star1').limit(10, offset=5)
        result = to_sql(table)
        expected = """SELECT *
FROM star1
LIMIT 10 OFFSET 5"""
        assert result == expected

        # Put the limit in a couple places in the stack
        table = self.con.table('star1')
        table = table[table.f > 0].limit(10)
        result = to_sql(table)

        expected = """SELECT *
FROM star1
WHERE `f` > 0
LIMIT 10"""

        assert result == expected

        table = self.con.table('star1')

        # Semantically, this should produce a subquery
        table = table.limit(10)
        table = table[table.f > 0]

        result2 = to_sql(table)

        expected2 = """SELECT *
FROM (
  SELECT *
  FROM star1
  LIMIT 10
) t0
WHERE `f` > 0"""

        assert result2 == expected2

    def test_join_with_limited_table(self):
        t1 = self.con.table('star1')
        t2 = self.con.table('star2')

        limited = t1.limit(100)
        joined = (limited.inner_join(t2, [limited.foo_id == t2.foo_id])
                  [[limited]])

        result = to_sql(joined)
        expected = """SELECT t0.*
FROM (
  SELECT *
  FROM star1
  LIMIT 100
) t0
  INNER JOIN star2 t1
    ON t0.`foo_id` = t1.`foo_id`"""

        assert result == expected

    def test_sort_by_on_limit_yield_subquery(self):
        # x.limit(...).sort_by(...)
        #   is semantically different from
        # x.sort_by(...).limit(...)
        #   and will often yield different results
        t = self.con.table('functional_alltypes')
        expr = (t.group_by('string_col')
                .aggregate([t.count().name('nrows')])
                .limit(5)
                .sort_by('string_col'))

        result = to_sql(expr)
        expected = """SELECT *
FROM (
  SELECT `string_col`, count(*) AS `nrows`
  FROM functional_alltypes
  GROUP BY 1
  LIMIT 5
) t0
ORDER BY `string_col`"""
        assert result == expected

    def test_multiple_limits(self):
        t = self.con.table('functional_alltypes')

        expr = t.limit(20).limit(10)
        stmt = build_ast(expr).queries[0]

        assert stmt.limit['n'] == 10

    def test_top_convenience(self):
        # x.top(10, by=field)
        # x.top(10, by=[field1, field2])
        pass

    def test_scalar_aggregate_expr(self):
        # Things like (table.a - table2.b.mean()).sum(), requiring subquery
        # extraction
        pass

    def test_filter_in_between_joins(self):
        # With filter predicates involving only a single
        pass

    def test_self_aggregate_in_predicate(self):
        # Per ibis #43
        pass


class TestNonTabularResults(unittest.TestCase):

    """

    """

    def setUp(self):
        self.con = MockConnection()
        self.table = self.con.table('alltypes')

    def test_simple_scalar_aggregates(self):
        # Things like table.column.{sum, mean, ...}()
        table = self.con.table('alltypes')

        expr = table[table.c > 0].f.sum()

        ast = build_ast(expr)
        query = ast.queries[0]

        sql_query = query.compile()
        expected = """SELECT sum(`f`) AS `tmp`
FROM alltypes
WHERE `c` > 0"""

        assert sql_query == expected

        # Maybe the result handler should act on the cursor. Not sure.
        handler = query.result_handler
        output = pd.DataFrame({'tmp': [5]})
        assert handler(output) == 5

    def test_table_column_unbox(self):
        table = self.table
        m = table.f.sum().name('total')
        agged = table[table.c > 0].group_by('g').aggregate([m])
        expr = agged.g

        ast = build_ast(expr)
        query = ast.queries[0]

        sql_query = query.compile()
        expected = """SELECT `g`, sum(`f`) AS `total`
FROM alltypes
WHERE `c` > 0
GROUP BY 1"""

        assert sql_query == expected

        # Maybe the result handler should act on the cursor. Not sure.
        handler = query.result_handler
        output = pd.DataFrame({'g': ['foo', 'bar', 'baz']})
        assert (handler(output) == output['g']).all()

    def test_complex_array_expr_projection(self):
        # May require finding the base table and forming a projection.
        expr = (self.table.group_by('g')
                .aggregate([self.table.count().name('count')]))
        expr2 = expr.g.cast('double')

        query = to_sql(expr2)
        expected = """SELECT CAST(`g` AS double) AS `tmp`
FROM (
  SELECT `g`, count(*) AS `count`
  FROM alltypes
  GROUP BY 1
) t0"""
        assert query == expected

    def test_scalar_exprs_no_table_refs(self):
        expr1 = ibis.now()
        expected1 = """\
SELECT now() AS `tmp`"""

        expr2 = ibis.literal(1) + ibis.literal(2)
        expected2 = """\
SELECT 1 + 2 AS `tmp`"""

        cases = [
            (expr1, expected1),
            (expr2, expected2)
        ]

        for expr, expected in cases:
            result = to_sql(expr)
            assert result == expected

    def test_expr_list_no_table_refs(self):
        exlist = ibis.api.expr_list([ibis.literal(1).name('a'),
                                     ibis.now().name('b'),
                                     ibis.literal(2).log().name('c')])
        result = to_sql(exlist)
        expected = """\
SELECT 1 AS `a`, now() AS `b`, ln(2) AS `c`"""
        assert result == expected

    def test_isnull_case_expr_rewrite_failure(self):
        # #172, case expression that was not being properly converted into an
        # aggregation
        reduction = self.table.g.isnull().ifelse(1, 0).sum()

        result = to_sql(reduction)
        expected = """\
SELECT sum(CASE WHEN `g` IS NULL THEN 1 ELSE 0 END) AS `tmp`
FROM alltypes"""
        assert result == expected


class TestDataIngestWorkflows(unittest.TestCase):

    def test_input_source_from_textfile(self):
        pass


def _get_query(expr):
    ast = build_ast(expr)
    return ast.queries[0]

nation = api.table([
    ('n_regionkey', 'int32'),
    ('n_nationkey', 'int32'),
    ('n_name', 'string')
], 'nation')

region = api.table([
    ('r_regionkey', 'int32'),
    ('r_name', 'string')
], 'region')

customer = api.table([
    ('c_nationkey', 'int32'),
    ('c_name', 'string'),
    ('c_acctbal', 'double')
], 'customer')


class TestSelectSQL(unittest.TestCase):

    def setUp(self):
        self.con = MockConnection()

    def test_nameless_table(self):
        # Ensure that user gets some kind of sensible error
        nameless = api.table([('key', 'string')])
        self.assertRaises(com.RelationError, to_sql, nameless)

        with_name = api.table([('key', 'string')], name='baz')
        result = to_sql(with_name)
        assert result == 'SELECT *\nFROM baz'

    def test_physical_table_reference_translate(self):
        # If an expression's table leaves all reference database tables, verify
        # we translate correctly
        table = self.con.table('alltypes')

        query = _get_query(table)
        sql_string = query.compile()
        expected = "SELECT *\nFROM alltypes"
        assert sql_string == expected

    def test_simple_join_formatting(self):
        t1 = self.con.table('star1')
        t2 = self.con.table('star2')

        pred = t1['foo_id'] == t2['foo_id']
        pred2 = t1['bar_id'] == t2['foo_id']
        cases = [
            (t1.inner_join(t2, [pred])[[t1]],
             """SELECT t0.*
FROM star1 t0
  INNER JOIN star2 t1
    ON t0.`foo_id` = t1.`foo_id`"""),
            (t1.left_join(t2, [pred])[[t1]],
             """SELECT t0.*
FROM star1 t0
  LEFT OUTER JOIN star2 t1
    ON t0.`foo_id` = t1.`foo_id`"""),
            (t1.outer_join(t2, [pred])[[t1]],
             """SELECT t0.*
FROM star1 t0
  FULL OUTER JOIN star2 t1
    ON t0.`foo_id` = t1.`foo_id`"""),
            # multiple predicates
            (t1.inner_join(t2, [pred, pred2])[[t1]],
             """SELECT t0.*
FROM star1 t0
  INNER JOIN star2 t1
    ON t0.`foo_id` = t1.`foo_id` AND
       t0.`bar_id` = t1.`foo_id`"""),
        ]

        for expr, expected_sql in cases:
            result_sql = to_sql(expr)
            assert result_sql == expected_sql

    def test_multiple_join_cases(self):
        t1 = self.con.table('star1')
        t2 = self.con.table('star2')
        t3 = self.con.table('star3')

        predA = t1['foo_id'] == t2['foo_id']
        predB = t1['bar_id'] == t3['bar_id']

        what = (t1.left_join(t2, [predA])
                .inner_join(t3, [predB])
                .projection([t1, t2['value1'], t3['value2']]))
        result_sql = to_sql(what)
        expected_sql = """SELECT t0.*, t1.`value1`, t2.`value2`
FROM star1 t0
  LEFT OUTER JOIN star2 t1
    ON t0.`foo_id` = t1.`foo_id`
  INNER JOIN star3 t2
    ON t0.`bar_id` = t2.`bar_id`"""
        assert result_sql == expected_sql

    def test_join_between_joins(self):
        t1 = api.table([
            ('key1', 'string'),
            ('key2', 'string'),
            ('value1', 'double'),
        ], 'first')

        t2 = api.table([
            ('key1', 'string'),
            ('value2', 'double'),
        ], 'second')

        t3 = api.table([
            ('key2', 'string'),
            ('key3', 'string'),
            ('value3', 'double'),
        ], 'third')

        t4 = api.table([
            ('key3', 'string'),
            ('value4', 'double')
        ], 'fourth')

        left = t1.inner_join(t2, [('key1', 'key1')])[t1, t2.value2]
        right = t3.inner_join(t4, [('key3', 'key3')])[t3, t4.value4]

        joined = left.inner_join(right, [('key2', 'key2')])

        # At one point, the expression simplification was resulting in bad refs
        # here (right.value3 referencing the table inside the right join)
        exprs = [left, right.value3, right.value4]
        projected = joined.projection(exprs)

        result = to_sql(projected)
        expected = """SELECT t0.*, t1.`value3`, t1.`value4`
FROM (
  SELECT t2.*, t3.`value2`
  FROM `first` t2
    INNER JOIN second t3
      ON t2.`key1` = t3.`key1`
) t0
  INNER JOIN (
    SELECT t2.*, t3.`value4`
    FROM third t2
      INNER JOIN fourth t3
        ON t2.`key3` = t3.`key3`
  ) t1
    ON t0.`key2` = t1.`key2`"""
        assert result == expected

    def test_join_just_materialized(self):
        t1 = self.con.table('tpch_nation')
        t2 = self.con.table('tpch_region')
        t3 = self.con.table('tpch_customer')

        # GH #491
        joined = (t1.inner_join(t2, t1.n_regionkey == t2.r_regionkey)
                  .inner_join(t3, t1.n_nationkey == t3.c_nationkey))
        result = to_sql(joined)
        expected = """SELECT *
FROM tpch_nation t0
  INNER JOIN tpch_region t1
    ON t0.`n_regionkey` = t1.`r_regionkey`
  INNER JOIN tpch_customer t2
    ON t0.`n_nationkey` = t2.`c_nationkey`"""
        assert result == expected

        result = to_sql(joined.materialize())
        assert result == expected

    def test_join_no_predicates_for_impala(self):
        # Impala requires that joins without predicates be written explicitly
        # as CROSS JOIN, since result sets can accidentally get too large if a
        # query is executed before predicates are written
        t1 = self.con.table('star1')
        t2 = self.con.table('star2')

        joined2 = t1.cross_join(t2)[[t1]]

        expected = """SELECT t0.*
FROM star1 t0
  CROSS JOIN star2 t1"""
        result2 = to_sql(joined2)
        assert result2 == expected

        for jtype in ['inner_join', 'left_join', 'outer_join']:
            joined = getattr(t1, jtype)(t2)[[t1]]

            result = to_sql(joined)
            assert result == expected

    def test_semi_anti_joins(self):
        t1 = self.con.table('star1')
        t2 = self.con.table('star2')

        joined = t1.semi_join(t2, [t1.foo_id == t2.foo_id])[[t1]]

        result = to_sql(joined)
        expected = """SELECT t0.*
FROM star1 t0
  LEFT SEMI JOIN star2 t1
    ON t0.`foo_id` = t1.`foo_id`"""
        assert result == expected

        joined = t1.anti_join(t2, [t1.foo_id == t2.foo_id])[[t1]]
        result = to_sql(joined)
        expected = """SELECT t0.*
FROM star1 t0
  LEFT ANTI JOIN star2 t1
    ON t0.`foo_id` = t1.`foo_id`"""
        assert result == expected

    def test_self_reference_simple(self):
        t1 = self.con.table('star1')

        result_sql = to_sql(t1.view())
        expected_sql = "SELECT *\nFROM star1"
        assert result_sql == expected_sql

    def test_join_self_reference(self):
        t1 = self.con.table('star1')
        t2 = t1.view()

        result = t1.inner_join(t2, [t1.foo_id == t2.bar_id])[[t1]]

        result_sql = to_sql(result)
        expected_sql = """SELECT t0.*
FROM star1 t0
  INNER JOIN star1 t1
    ON t0.`foo_id` = t1.`bar_id`"""
        assert result_sql == expected_sql

    def test_join_projection_subquery_broken_alias(self):
        # From an observed bug, derived from tpch tables
        geo = (nation.inner_join(region, [('n_regionkey', 'r_regionkey')])
               [nation.n_nationkey,
                nation.n_name.name('nation'),
                region.r_name.name('region')])

        expr = (geo.inner_join(customer, [('n_nationkey', 'c_nationkey')])
                [customer, geo])

        result = to_sql(expr)
        expected = """SELECT t1.*, t0.*
FROM (
  SELECT t2.`n_nationkey`, t2.`n_name` AS `nation`, t3.`r_name` AS `region`
  FROM nation t2
    INNER JOIN region t3
      ON t2.`n_regionkey` = t3.`r_regionkey`
) t0
  INNER JOIN customer t1
    ON t0.`n_nationkey` = t1.`c_nationkey`"""
        assert result == expected

    def test_where_simple_comparisons(self):
        t1 = self.con.table('star1')

        what = t1.filter([t1.f > 0, t1.c < t1.f * 2])

        result = to_sql(what)
        expected = """SELECT *
FROM star1
WHERE `f` > 0 AND
      `c` < (`f` * 2)"""
        assert result == expected

    def test_where_in_array_literal(self):
        # e.g.
        # where string_col in (v1, v2, v3)
        raise unittest.SkipTest

    def test_where_with_join(self):
        t1 = self.con.table('star1')
        t2 = self.con.table('star2')

        # This also tests some cases of predicate pushdown
        what = (t1.inner_join(t2, [t1.foo_id == t2.foo_id])
                .projection([t1, t2.value1, t2.value3])
                .filter([t1.f > 0, t2.value3 < 1000]))

        what2 = (t1.inner_join(t2, [t1.foo_id == t2.foo_id])
                 .filter([t1.f > 0, t2.value3 < 1000])
                 .projection([t1, t2.value1, t2.value3]))

        expected_sql = """SELECT t0.*, t1.`value1`, t1.`value3`
FROM star1 t0
  INNER JOIN star2 t1
    ON t0.`foo_id` = t1.`foo_id`
WHERE t0.`f` > 0 AND
      t1.`value3` < 1000"""

        result_sql = to_sql(what)
        assert result_sql == expected_sql

        result2_sql = to_sql(what2)
        assert result2_sql == expected_sql

    def test_where_no_pushdown_possible(self):
        t1 = self.con.table('star1')
        t2 = self.con.table('star2')

        joined = (t1.inner_join(t2, [t1.foo_id == t2.foo_id])
                  [t1, (t1.f - t2.value1).name('diff')])

        filtered = joined[joined.diff > 1]

        # TODO: I'm not sure if this is exactly what we want
        expected_sql = """SELECT *
FROM (
  SELECT t0.*, t0.`f` - t1.`value1` AS `diff`
  FROM star1 t0
    INNER JOIN star2 t1
      ON t0.`foo_id` = t1.`foo_id`
  WHERE t0.`f` > 0 AND
        t1.`value3` < 1000
)
WHERE `diff` > 1"""

        raise unittest.SkipTest

        result_sql = to_sql(filtered)
        assert result_sql == expected_sql

    def test_where_with_between(self):
        t = self.con.table('alltypes')

        what = t.filter([t.a > 0, t.f.between(0, 1)])
        result = to_sql(what)
        expected = """SELECT *
FROM alltypes
WHERE `a` > 0 AND
      `f` BETWEEN 0 AND 1"""
        assert result == expected

    def test_where_analyze_scalar_op(self):
        # root cause of #310

        table = self.con.table('functional_alltypes')

        expr = (table.filter([table.timestamp_col <
                             (ibis.timestamp('2010-01-01') + ibis.month(3)),
                             table.timestamp_col < (ibis.now() +
                                                    ibis.day(10))])
                .count())

        result = to_sql(expr)
        expected = """\
SELECT count(*) AS `tmp`
FROM functional_alltypes
WHERE `timestamp_col` < months_add('2010-01-01 00:00:00', 3) AND
      `timestamp_col` < days_add(now(), 10)"""
        assert result == expected

    def test_simple_aggregate_query(self):
        t1 = self.con.table('star1')

        cases = [
            (t1.aggregate([t1['f'].sum().name('total')],
                          [t1['foo_id']]),
             """SELECT `foo_id`, sum(`f`) AS `total`
FROM star1
GROUP BY 1"""),
            (t1.aggregate([t1['f'].sum().name('total')],
                          ['foo_id', 'bar_id']),
             """SELECT `foo_id`, `bar_id`, sum(`f`) AS `total`
FROM star1
GROUP BY 1, 2""")
        ]
        for expr, expected_sql in cases:
            result_sql = to_sql(expr)
            assert result_sql == expected_sql

    def test_aggregate_having(self):
        # Filtering post-aggregation predicate
        t1 = self.con.table('star1')

        total = t1.f.sum().name('total')
        metrics = [total]

        expr = t1.aggregate(metrics, by=['foo_id'],
                            having=[total > 10])
        result = to_sql(expr)
        expected = """SELECT `foo_id`, sum(`f`) AS `total`
FROM star1
GROUP BY 1
HAVING sum(`f`) > 10"""
        assert result == expected

        expr = t1.aggregate(metrics, by=['foo_id'],
                            having=[t1.count() > 100])
        result = to_sql(expr)
        expected = """SELECT `foo_id`, sum(`f`) AS `total`
FROM star1
GROUP BY 1
HAVING count(*) > 100"""
        assert result == expected

    def test_aggregate_table_count_metric(self):
        expr = self.con.table('star1').count()

        result = to_sql(expr)
        expected = """SELECT count(*) AS `tmp`
FROM star1"""
        assert result == expected

        # count on more complicated table
        region = self.con.table('tpch_region')
        nation = self.con.table('tpch_nation')
        join_expr = region.r_regionkey == nation.n_regionkey
        joined = region.inner_join(nation, join_expr)
        table_ref = joined[nation, region.r_name.name('region')]

        expr = table_ref.count()
        result = to_sql(expr)
        expected = """SELECT count(*) AS `tmp`
FROM (
  SELECT t2.*, t1.`r_name` AS `region`
  FROM tpch_region t1
    INNER JOIN tpch_nation t2
      ON t1.`r_regionkey` = t2.`n_regionkey`
) t0"""
        assert result == expected

    def test_expr_template_field_name_binding(self):
        # Given an expression with no concrete links to actual database tables,
        # indicate a mapping between the distinct unbound table leaves of the
        # expression and some database tables with compatible schemas but
        # potentially different column names
        pass

    def test_no_aliases_needed(self):
        table = api.table([
            ('key1', 'string'),
            ('key2', 'string'),
            ('value', 'double')
        ])

        expr = table.aggregate([table['value'].sum().name('total')],
                               by=['key1', 'key2'])

        query = _get_query(expr)
        context = query.context
        assert not context.need_aliases()

    def test_table_names_overlap_default_aliases(self):
        # see discussion in #104; this actually is not needed for query
        # correctness, and only makes the generated SQL nicer
        raise unittest.SkipTest

        t0 = api.table([
            ('key', 'string'),
            ('v1', 'double')
        ], 't1')

        t1 = api.table([
            ('key', 'string'),
            ('v2', 'double')
        ], 't0')

        expr = t0.join(t1, t0.key == t1.key)[t0.key, t0.v1, t1.v2]

        result = to_sql(expr)
        expected = """\
SELECT t2.`key`, t2.`v1`, t3.`v2`
FROM t0 t2
  INNER JOIN t1 t3
    ON t2.`key` = t3.`key`"""

        assert result == expected

    def test_context_aliases_multiple_join(self):
        t1 = self.con.table('star1')
        t2 = self.con.table('star2')
        t3 = self.con.table('star3')

        expr = (t1.left_join(t2, [t1['foo_id'] == t2['foo_id']])
                .inner_join(t3, [t1['bar_id'] == t3['bar_id']])
                [[t1, t2['value1'], t3['value2']]])

        query = _get_query(expr)
        context = query.context

        assert context.get_alias(t1) == 't0'
        assert context.get_alias(t2) == 't1'
        assert context.get_alias(t3) == 't2'

    def test_fuse_projections(self):
        table = api.table([
            ('foo', 'int32'),
            ('bar', 'int64'),
            ('value', 'double')
        ], name='tbl')

        # Cases where we project in both cases using the base table reference
        f1 = (table['foo'] + table['bar']).name('baz')
        pred = table['value'] > 0

        table2 = table[table, f1]
        table2_filtered = table2[pred]

        f2 = (table2['foo'] * 2).name('qux')
        f3 = (table['foo'] * 2).name('qux')

        table3 = table2.projection([table2, f2])

        # fusion works even if there's a filter
        table3_filtered = table2_filtered.projection([table2, f2])

        expected = table[table, f1, f3]
        expected2 = table[pred][table, f1, f3]

        assert table3.equals(expected)
        assert table3_filtered.equals(expected2)

        ex_sql = """SELECT *, `foo` + `bar` AS `baz`, `foo` * 2 AS `qux`
FROM tbl"""

        ex_sql2 = """SELECT *, `foo` + `bar` AS `baz`, `foo` * 2 AS `qux`
FROM tbl
WHERE `value` > 0"""

        table3_sql = to_sql(table3)
        table3_filt_sql = to_sql(table3_filtered)

        assert table3_sql == ex_sql
        assert table3_filt_sql == ex_sql2

        # Use the intermediate table refs
        table3 = table2.projection([table2, f2])

        # fusion works even if there's a filter
        table3_filtered = table2_filtered.projection([table2, f2])

        expected = table[table, f1, f3]
        expected2 = table[pred][table, f1, f3]

        assert table3.equals(expected)
        assert table3_filtered.equals(expected2)

    def test_bug_project_multiple_times(self):
        # 108
        customer = self.con.table('tpch_customer')
        nation = self.con.table('tpch_nation')
        region = self.con.table('tpch_region')

        joined = (
            customer.inner_join(nation,
                                [customer.c_nationkey == nation.n_nationkey])
            .inner_join(region,
                        [nation.n_regionkey == region.r_regionkey])
        )
        proj1 = [customer, nation.n_name, region.r_name]
        step1 = joined[proj1]

        topk_by = step1.c_acctbal.cast('double').sum()
        pred = step1.n_name.topk(10, by=topk_by)

        proj_exprs = [step1.c_name, step1.r_name, step1.n_name]
        step2 = step1[pred]
        expr = step2.projection(proj_exprs)

        # it works!
        result = to_sql(expr)
        expected = """\
SELECT `c_name`, `r_name`, `n_name`
FROM (
  SELECT t1.*, t2.`n_name`, t3.`r_name`
  FROM tpch_customer t1
    INNER JOIN tpch_nation t2
      ON t1.`c_nationkey` = t2.`n_nationkey`
    INNER JOIN tpch_region t3
      ON t2.`n_regionkey` = t3.`r_regionkey`
    LEFT SEMI JOIN (
      SELECT t2.`n_name`, sum(CAST(t1.`c_acctbal` AS double)) AS `__tmp__`
      FROM tpch_customer t1
        INNER JOIN tpch_nation t2
          ON t1.`c_nationkey` = t2.`n_nationkey`
        INNER JOIN tpch_region t3
          ON t2.`n_regionkey` = t3.`r_regionkey`
      GROUP BY 1
      ORDER BY `__tmp__` DESC
      LIMIT 10
    ) t4
      ON t2.`n_name` = t4.`n_name`
) t0"""
        assert result == expected

    def test_aggregate_projection_subquery(self):
        t = self.con.table('alltypes')

        proj = t[t.f > 0][t, (t.a + t.b).name('foo')]

        def agg(x):
            return x.aggregate([x.foo.sum().name('foo total')], by=['g'])

        # predicate gets pushed down
        filtered = proj[proj.g == 'bar']

        result = to_sql(filtered)
        expected = """SELECT *, `a` + `b` AS `foo`
FROM alltypes
WHERE `f` > 0 AND
      `g` = 'bar'"""
        assert result == expected

        agged = agg(filtered)
        result = to_sql(agged)
        expected = """SELECT `g`, sum(`foo`) AS `foo total`
FROM (
  SELECT *, `a` + `b` AS `foo`
  FROM alltypes
  WHERE `f` > 0 AND
        `g` = 'bar'
) t0
GROUP BY 1"""
        assert result == expected

        # Pushdown is not possible (in Impala, Postgres, others)
        agged2 = agg(proj[proj.foo < 10])

        result = to_sql(agged2)
        expected = """SELECT t0.`g`, sum(t0.`foo`) AS `foo total`
FROM (
  SELECT *, `a` + `b` AS `foo`
  FROM alltypes
  WHERE `f` > 0
) t0
WHERE t0.`foo` < 10
GROUP BY 1"""
        assert result == expected

    def test_subquery_aliased(self):
        t1 = self.con.table('star1')
        t2 = self.con.table('star2')

        agged = t1.aggregate([t1.f.sum().name('total')], by=['foo_id'])
        what = (agged.inner_join(t2, [agged.foo_id == t2.foo_id])
                [agged, t2.value1])

        result = to_sql(what)
        expected = """SELECT t0.*, t1.`value1`
FROM (
  SELECT `foo_id`, sum(`f`) AS `total`
  FROM star1
  GROUP BY 1
) t0
  INNER JOIN star2 t1
    ON t0.`foo_id` = t1.`foo_id`"""
        assert result == expected

    def test_double_nested_subquery_no_aliases(self):
        # We don't require any table aliasing anywhere
        t = api.table([
            ('key1', 'string'),
            ('key2', 'string'),
            ('key3', 'string'),
            ('value', 'double')
        ], 'foo_table')

        agg1 = t.aggregate([t.value.sum().name('total')],
                           by=['key1', 'key2', 'key3'])
        agg2 = agg1.aggregate([agg1.total.sum().name('total')],
                              by=['key1', 'key2'])
        agg3 = agg2.aggregate([agg2.total.sum().name('total')],
                              by=['key1'])

        result = to_sql(agg3)
        expected = """SELECT `key1`, sum(`total`) AS `total`
FROM (
  SELECT `key1`, `key2`, sum(`total`) AS `total`
  FROM (
    SELECT `key1`, `key2`, `key3`, sum(`value`) AS `total`
    FROM foo_table
    GROUP BY 1, 2, 3
  ) t1
  GROUP BY 1, 2
) t0
GROUP BY 1"""
        assert result == expected

    def test_aggregate_projection_alias_bug(self):
        # Observed in use
        t1 = self.con.table('star1')
        t2 = self.con.table('star2')

        what = (t1.inner_join(t2, [t1.foo_id == t2.foo_id])
                [[t1, t2.value1]])

        what = what.aggregate([what.value1.sum().name('total')],
                              by=[what.foo_id])

        # TODO: Not fusing the aggregation with the projection yet
        result = to_sql(what)
        expected = """SELECT `foo_id`, sum(`value1`) AS `total`
FROM (
  SELECT t1.*, t2.`value1`
  FROM star1 t1
    INNER JOIN star2 t2
      ON t1.`foo_id` = t2.`foo_id`
) t0
GROUP BY 1"""
        assert result == expected

    def test_aggregate_fuse_with_projection(self):
        # see above test case
        pass

    def test_subquery_used_for_self_join(self):
        # There could be cases that should look in SQL like
        # WITH t0 as (some subquery)
        # select ...
        # from t0 t1
        #   join t0 t2
        #     on t1.kind = t2.subkind
        # ...
        # However, the Ibis code will simply have an expression (projection or
        # aggregation, say) built on top of the subquery expression, so we need
        # to extract the subquery unit (we see that it appears multiple times
        # in the tree).
        t = self.con.table('alltypes')

        agged = t.aggregate([t.f.sum().name('total')], by=['g', 'a', 'b'])
        view = agged.view()
        metrics = [(agged.total - view.total).max().name('metric')]
        reagged = (agged.inner_join(view, [agged.a == view.b])
                   .aggregate(metrics, by=[agged.g]))

        result = to_sql(reagged)
        expected = """WITH t0 AS (
  SELECT `g`, `a`, `b`, sum(`f`) AS `total`
  FROM alltypes
  GROUP BY 1, 2, 3
)
SELECT t0.`g`, max(t0.`total` - t1.`total`) AS `metric`
FROM t0
  INNER JOIN t0 t1
    ON t0.`a` = t1.`b`
GROUP BY 1"""
        assert result == expected

    def test_subquery_factor_correlated_subquery(self):
        # #173, #183 and other issues
        region = self.con.table('tpch_region')
        nation = self.con.table('tpch_nation')
        customer = self.con.table('tpch_customer')
        orders = self.con.table('tpch_orders')

        fields_of_interest = [customer,
                              region.r_name.name('region'),
                              orders.o_totalprice.name('amount'),
                              orders.o_orderdate
                              .cast('timestamp').name('odate')]

        tpch = (region.join(nation, region.r_regionkey == nation.n_regionkey)
                .join(customer, customer.c_nationkey == nation.n_nationkey)
                .join(orders, orders.o_custkey == customer.c_custkey)
                [fields_of_interest])

        # Self-reference + correlated subquery complicates things
        t2 = tpch.view()
        conditional_avg = t2[t2.region == tpch.region].amount.mean()
        amount_filter = tpch.amount > conditional_avg

        expr = tpch[amount_filter].limit(10)

        result = to_sql(expr)
        expected = """\
WITH t0 AS (
  SELECT t5.*, t1.`r_name` AS `region`, t3.`o_totalprice` AS `amount`,
         CAST(t3.`o_orderdate` AS timestamp) AS `odate`
  FROM tpch_region t1
    INNER JOIN tpch_nation t2
      ON t1.`r_regionkey` = t2.`n_regionkey`
    INNER JOIN tpch_customer t5
      ON t5.`c_nationkey` = t2.`n_nationkey`
    INNER JOIN tpch_orders t3
      ON t3.`o_custkey` = t5.`c_custkey`
)
SELECT t0.*
FROM t0
WHERE t0.`amount` > (
  SELECT avg(t4.`amount`) AS `tmp`
  FROM t0 t4
  WHERE t4.`region` = t0.`region`
)
LIMIT 10"""
        assert result == expected

    def test_self_join_subquery_distinct_equal(self):
        region = self.con.table('tpch_region')
        nation = self.con.table('tpch_nation')

        j1 = (region.join(nation, region.r_regionkey == nation.n_regionkey)
              [region, nation])

        j2 = (region.join(nation, region.r_regionkey == nation.n_regionkey)
              [region, nation].view())

        expr = (j1.join(j2, j1.r_regionkey == j2.r_regionkey)
                [j1.r_name, j2.n_name])

        result = to_sql(expr)
        expected = """\
WITH t0 AS (
  SELECT t2.*, t3.*
  FROM tpch_region t2
    INNER JOIN tpch_nation t3
      ON t2.`r_regionkey` = t3.`n_regionkey`
)
SELECT t0.`r_name`, t1.`n_name`
FROM t0
  INNER JOIN t0 t1
    ON t0.`r_regionkey` = t1.`r_regionkey`"""

        assert result == expected

    def test_limit_with_self_join(self):
        t = self.con.table('functional_alltypes')
        t2 = t.view()

        expr = t.join(t2, t.tinyint_col < t2.timestamp_col.minute()).count()

        # it works
        result = to_sql(expr)
        expected = """\
SELECT count(*) AS `tmp`
FROM functional_alltypes t0
  INNER JOIN functional_alltypes t1
    ON t0.`tinyint_col` < extract(t1.`timestamp_col`, 'minute')"""
        assert result == expected

    def test_cte_factor_distinct_but_equal(self):
        t = self.con.table('alltypes')
        tt = self.con.table('alltypes')

        expr1 = t.group_by('g').aggregate(t.f.sum().name('metric'))
        expr2 = tt.group_by('g').aggregate(tt.f.sum().name('metric')).view()

        expr = expr1.join(expr2, expr1.g == expr2.g)[[expr1]]

        result = to_sql(expr)
        expected = """\
WITH t0 AS (
  SELECT `g`, sum(`f`) AS `metric`
  FROM alltypes
  GROUP BY 1
)
SELECT t0.*
FROM t0
  INNER JOIN t0 t1
    ON t0.`g` = t1.`g`"""

        assert result == expected

    def test_tpch_self_join_failure(self):
        # duplicating the integration test here

        region = self.con.table('tpch_region')
        nation = self.con.table('tpch_nation')
        customer = self.con.table('tpch_customer')
        orders = self.con.table('tpch_orders')

        fields_of_interest = [
            region.r_name.name('region'),
            nation.n_name.name('nation'),
            orders.o_totalprice.name('amount'),
            orders.o_orderdate.cast('timestamp').name('odate')]

        joined_all = (
            region.join(nation, region.r_regionkey == nation.n_regionkey)
            .join(customer, customer.c_nationkey == nation.n_nationkey)
            .join(orders, orders.o_custkey == customer.c_custkey)
            [fields_of_interest])

        year = joined_all.odate.year().name('year')
        total = joined_all.amount.sum().cast('double').name('total')
        annual_amounts = (joined_all
                          .group_by(['region', year])
                          .aggregate(total))

        current = annual_amounts
        prior = annual_amounts.view()

        yoy_change = (current.total - prior.total).name('yoy_change')
        yoy = (current.join(prior, current.year == (prior.year - 1))
               [current.region, current.year, yoy_change])
        to_sql(yoy)

    def test_extract_subquery_nested_lower(self):
        # We may have a join between two tables requiring subqueries, and
        # buried inside these there may be a common subquery. Let's test that
        # we find it and pull it out to the top level to avoid repeating
        # ourselves.
        pass

    def test_subquery_in_filter_predicate(self):
        # E.g. comparing against some scalar aggregate value. See Ibis #43
        t1 = self.con.table('star1')

        pred = t1.f > t1.f.mean()
        expr = t1[pred]

        # This brought out another expression rewriting bug, since the filtered
        # table isn't found elsewhere in the expression.
        pred2 = t1.f > t1[t1.foo_id == 'foo'].f.mean()
        expr2 = t1[pred2]

        result = to_sql(expr)
        expected = """SELECT *
FROM star1
WHERE `f` > (
  SELECT avg(`f`) AS `tmp`
  FROM star1
)"""
        assert result == expected

        result = to_sql(expr2)
        expected = """SELECT *
FROM star1
WHERE `f` > (
  SELECT avg(`f`) AS `tmp`
  FROM star1
  WHERE `foo_id` = 'foo'
)"""
        assert result == expected

    def test_filter_subquery_derived_reduction(self):
        t1 = self.con.table('star1')

        # Reduction can be nested inside some scalar expression
        pred3 = t1.f > t1[t1.foo_id == 'foo'].f.mean().log()
        pred4 = t1.f > (t1[t1.foo_id == 'foo'].f.mean().log() + 1)

        expr3 = t1[pred3]
        result = to_sql(expr3)
        expected = """SELECT *
FROM star1
WHERE `f` > (
  SELECT ln(avg(`f`)) AS `tmp`
  FROM star1
  WHERE `foo_id` = 'foo'
)"""
        assert result == expected

        expr4 = t1[pred4]

        result = to_sql(expr4)
        expected = """SELECT *
FROM star1
WHERE `f` > (
  SELECT ln(avg(`f`)) + 1 AS `tmp`
  FROM star1
  WHERE `foo_id` = 'foo'
)"""
        assert result == expected

    def test_topk_operation_to_semi_join(self):
        # TODO: top K with filter in place

        table = api.table([
            ('foo', 'string'),
            ('bar', 'string'),
            ('city', 'string'),
            ('v1', 'double'),
            ('v2', 'double'),
        ], 'tbl')

        what = table.city.topk(10, by=table.v2.mean())
        filtered = table[what]

        query = to_sql(filtered)
        expected = """SELECT t0.*
FROM tbl t0
  LEFT SEMI JOIN (
    SELECT `city`, avg(`v2`) AS `__tmp__`
    FROM tbl
    GROUP BY 1
    ORDER BY `__tmp__` DESC
    LIMIT 10
  ) t1
    ON t0.`city` = t1.`city`"""
        assert query == expected

        # Test the default metric (count)

        what = table.city.topk(10)
        filtered2 = table[what]
        query = to_sql(filtered2)
        expected = """SELECT t0.*
FROM tbl t0
  LEFT SEMI JOIN (
    SELECT `city`, count(`city`) AS `__tmp__`
    FROM tbl
    GROUP BY 1
    ORDER BY `__tmp__` DESC
    LIMIT 10
  ) t1
    ON t0.`city` = t1.`city`"""
        assert query == expected

    def test_topk_predicate_pushdown_bug(self):
        # Observed on TPCH data
        cplusgeo = (
            customer.inner_join(nation, [customer.c_nationkey ==
                                         nation.n_nationkey])
                    .inner_join(region, [nation.n_regionkey ==
                                         region.r_regionkey])
            [customer, nation.n_name, region.r_name])

        pred = cplusgeo.n_name.topk(10, by=cplusgeo.c_acctbal.sum())
        expr = cplusgeo.filter([pred])

        result = to_sql(expr)
        expected = """\
SELECT t0.*, t1.`n_name`, t2.`r_name`
FROM customer t0
  INNER JOIN nation t1
    ON t0.`c_nationkey` = t1.`n_nationkey`
  INNER JOIN region t2
    ON t1.`n_regionkey` = t2.`r_regionkey`
  LEFT SEMI JOIN (
    SELECT t1.`n_name`, sum(t0.`c_acctbal`) AS `__tmp__`
    FROM customer t0
      INNER JOIN nation t1
        ON t0.`c_nationkey` = t1.`n_nationkey`
      INNER JOIN region t2
        ON t1.`n_regionkey` = t2.`r_regionkey`
    GROUP BY 1
    ORDER BY `__tmp__` DESC
    LIMIT 10
  ) t3
    ON t1.`n_name` = t3.`n_name`"""
        assert result == expected

    def test_topk_analysis_bug(self):
        # GH #398
        airlines = ibis.table([('dest', 'string'),
                               ('origin', 'string'),
                               ('arrdelay', 'int32')], 'airlines')

        dests = ['ORD', 'JFK', 'SFO']
        t = airlines[airlines.dest.isin(dests)]
        delay_filter = t.dest.topk(10, by=t.arrdelay.mean())
        expr = t[delay_filter].group_by('origin').size()

        result = to_sql(expr)
        expected = """\
SELECT t0.`origin`, count(*) AS `count`
FROM airlines t0
  LEFT SEMI JOIN (
    SELECT `dest`, avg(`arrdelay`) AS `__tmp__`
    FROM airlines
    WHERE `dest` IN ('ORD', 'JFK', 'SFO')
    GROUP BY 1
    ORDER BY `__tmp__` DESC
    LIMIT 10
  ) t1
    ON t0.`dest` = t1.`dest`
WHERE t0.`dest` IN ('ORD', 'JFK', 'SFO')
GROUP BY 1"""

        assert result == expected

    def test_bottomk(self):
        pass

    def test_topk_antijoin(self):
        # Get the "other" category somehow
        pass

    def test_case_in_projection(self):
        t = self.con.table('alltypes')

        expr = (t.g.case()
                .when('foo', 'bar')
                .when('baz', 'qux')
                .else_('default').end())

        expr2 = (api.case()
                 .when(t.g == 'foo', 'bar')
                 .when(t.g == 'baz', t.g)
                 .end())

        proj = t[expr.name('col1'), expr2.name('col2'), t]

        result = to_sql(proj)
        expected = """SELECT
  CASE `g`
    WHEN 'foo' THEN 'bar'
    WHEN 'baz' THEN 'qux'
    ELSE 'default'
  END AS `col1`,
  CASE
    WHEN `g` = 'foo' THEN 'bar'
    WHEN `g` = 'baz' THEN `g`
    ELSE NULL
  END AS `col2`, *
FROM alltypes"""
        assert result == expected

    def test_identifier_quoting(self):
        data = api.table([
            ('date', 'int32'),
            ('explain', 'string')
        ], 'table')

        expr = data[data.date.name('else'), data.explain.name('join')]

        result = to_sql(expr)
        expected = """SELECT `date` AS `else`, `explain` AS `join`
FROM `table`"""
        assert result == expected


class TestUnions(unittest.TestCase):

    def setUp(self):
        self.con = MockConnection()

        table = self.con.table('functional_alltypes')

        self.t1 = (table[table.int_col > 0]
                   [table.string_col.name('key'),
                    table.float_col.cast('double').name('value')])
        self.t2 = (table[table.int_col <= 0]
                   [table.string_col.name('key'),
                    table.double_col.name('value')])

        self.union1 = self.t1.union(self.t2)

    def test_union(self):
        result = to_sql(self.union1)
        expected = """\
SELECT `string_col` AS `key`, CAST(`float_col` AS double) AS `value`
FROM functional_alltypes
WHERE `int_col` > 0
UNION ALL
SELECT `string_col` AS `key`, `double_col` AS `value`
FROM functional_alltypes
WHERE `int_col` <= 0"""
        assert result == expected

    def test_union_distinct(self):
        union = self.t1.union(self.t2, distinct=True)
        result = to_sql(union)
        expected = """\
SELECT `string_col` AS `key`, CAST(`float_col` AS double) AS `value`
FROM functional_alltypes
WHERE `int_col` > 0
UNION
SELECT `string_col` AS `key`, `double_col` AS `value`
FROM functional_alltypes
WHERE `int_col` <= 0"""
        assert result == expected

    def test_union_project_column(self):
        # select a column, get a subquery
        expr = self.union1[[self.union1.key]]
        result = to_sql(expr)
        expected = """SELECT `key`
FROM (
  SELECT `string_col` AS `key`, CAST(`float_col` AS double) AS `value`
  FROM functional_alltypes
  WHERE `int_col` > 0
  UNION ALL
  SELECT `string_col` AS `key`, `double_col` AS `value`
  FROM functional_alltypes
  WHERE `int_col` <= 0
) t0"""
        assert result == expected

    def test_union_extract_with_block(self):
        pass

    def test_union_in_subquery(self):
        pass


def _create_table(table_name, expr, database=None, overwrite=False,
                  format='parquet'):
    ast = build_ast(expr)
    select = ast.queries[0]
    statement = ddl.CTAS(table_name, select,
                         database=database,
                         format=format,
                         overwrite=overwrite)
    return statement


def _get_select(expr):
    ast = build_ast(expr)
    select = ast.queries[0]
    context = ast.context

    return select, context


class TestDropTable(unittest.TestCase):

    def test_must_exist(self):
        statement = ddl.DropTable('foo', database='bar', must_exist=True)
        query = statement.compile()
        expected = "DROP TABLE bar.`foo`"
        assert query == expected

        statement = ddl.DropTable('foo', database='bar', must_exist=False)
        query = statement.compile()
        expected = "DROP TABLE IF EXISTS bar.`foo`"
        assert query == expected


class TestInsert(unittest.TestCase):

    def setUp(self):
        self.con = MockConnection()
        self.t = self.con.table('functional_alltypes')

    def test_select_basics(self):
        name = 'testing123456'

        expr = self.t.limit(10)
        select, _ = _get_select(expr)

        stmt = ddl.InsertSelect(name, select, database='foo')
        result = stmt.compile()

        expected = """\
INSERT INTO foo.`testing123456`
SELECT *
FROM functional_alltypes
LIMIT 10"""
        assert result == expected

        stmt = ddl.InsertSelect(name, select, database='foo', overwrite=True)
        result = stmt.compile()

        expected = """\
INSERT OVERWRITE foo.`testing123456`
SELECT *
FROM functional_alltypes
LIMIT 10"""
        assert result == expected

    def test_select_overwrite(self):
        pass


class TestCacheTable(unittest.TestCase):

    def test_pool_name(self):
        statement = ddl.CacheTable('foo', database='bar')
        query = statement.compile()
        expected = "ALTER TABLE bar.`foo` SET CACHED IN 'default'"
        assert query == expected

        statement = ddl.CacheTable('foo', database='bar', pool='my_pool')
        query = statement.compile()
        expected = "ALTER TABLE bar.`foo` SET CACHED IN 'my_pool'"
        assert query == expected


class TestCreateTable(unittest.TestCase):

    def setUp(self):
        self.con = MockConnection()

        self.t = t = self.con.table('functional_alltypes')
        self.expr = t[t.bigint_col > 0]

    def test_create_external_table_as(self):
        path = '/path/to/table'
        select = build_ast(self.con.table('test1')).queries[0]
        statement = ddl.CTAS('another_table',
                             select,
                             external=True,
                             overwrite=True,
                             path=path,
                             database='foo')
        result = statement.compile()

        expected = """\
CREATE EXTERNAL TABLE foo.`another_table`
STORED AS PARQUET
LOCATION '{0}'
AS
SELECT *
FROM test1""".format(path)
        assert result == expected

    def test_create_table_with_location(self):
        path = '/path/to/table'
        schema = ibis.schema([('foo', 'string'),
                              ('bar', 'int8'),
                              ('baz', 'int16')])
        select = build_ast(self.con.table('test1')).queries[0]
        statement = ddl.CreateTableWithSchema('another_table', schema,
                                              ddl.NoFormat(), overwrite=True,
                                              path=path, database='foo')
        result = statement.compile()

        expected = """\
CREATE TABLE foo.`another_table`
(`foo` STRING,
 `bar` TINYINT,
 `baz` SMALLINT)
LOCATION '{0}'""".format(path)
        assert result == expected

    def test_create_table_like_parquet(self):
        directory = '/path/to/'
        path = '/path/to/parquetfile'
        statement = ddl.CreateTableParquet('new_table',
                                           directory,
                                           example_file=path,
                                           overwrite=False,
                                           database='foo')

        result = statement.compile()
        expected = """\
CREATE EXTERNAL TABLE IF NOT EXISTS foo.`new_table`
LIKE PARQUET '{0}'
STORED AS PARQUET
LOCATION '{1}'""".format(path, directory)

        assert result == expected

    def test_create_table_parquet_like_other(self):
        # alternative to "LIKE PARQUET"
        directory = '/path/to/'
        example_table = 'db.other'

        statement = ddl.CreateTableParquet('new_table',
                                           directory,
                                           example_table=example_table,
                                           overwrite=False,
                                           database='foo')

        result = statement.compile()
        expected = """\
CREATE EXTERNAL TABLE IF NOT EXISTS foo.`new_table`
LIKE {0}
STORED AS PARQUET
LOCATION '{1}'""".format(example_table, directory)

        assert result == expected

    def test_create_table_parquet_with_schema(self):
        directory = '/path/to/'

        schema = ibis.schema([('foo', 'string'),
                              ('bar', 'int8'),
                              ('baz', 'int16')])

        statement = ddl.CreateTableParquet('new_table',
                                           directory,
                                           schema=schema,
                                           external=True,
                                           database='foo')

        result = statement.compile()
        expected = """\
CREATE EXTERNAL TABLE IF NOT EXISTS foo.`new_table`
(`foo` STRING,
 `bar` TINYINT,
 `baz` SMALLINT)
STORED AS PARQUET
LOCATION '{0}'""".format(directory)

        assert result == expected

    def test_create_table_delimited(self):
        path = '/path/to/files/'
        schema = ibis.schema([('a', 'string'),
                              ('b', 'int32'),
                              ('c', 'double'),
                              ('d', 'decimal(12,2)')])

        stmt = ddl.CreateTableDelimited('new_table', path, schema,
                                        delimiter='|',
                                        escapechar='\\',
                                        lineterminator='\0',
                                        database='foo')

        result = stmt.compile()
        expected = """\
CREATE EXTERNAL TABLE IF NOT EXISTS foo.`new_table`
(`a` STRING,
 `b` INT,
 `c` DOUBLE,
 `d` DECIMAL(12,2))
ROW FORMAT DELIMITED
FIELDS TERMINATED BY '|'
ESCAPED BY '\\'
LINES TERMINATED BY '\0'
LOCATION '{0}'""".format(path)
        assert result == expected

    def test_create_external_table_avro(self):
        path = '/path/to/files/'

        avro_schema = {
            'fields': [
                {'name': 'a', 'type': 'string'},
                {'name': 'b', 'type': 'int'},
                {'name': 'c', 'type': 'double'},
                {"type": "bytes",
                 "logicalType": "decimal",
                 "precision": 4,
                 "scale": 2,
                 'name': 'd'}
            ],
            'name': 'my_record',
            'type': 'record'
        }

        stmt = ddl.CreateTableAvro('new_table', path, avro_schema,
                                   database='foo')

        result = stmt.compile()
        expected = """\
CREATE EXTERNAL TABLE IF NOT EXISTS foo.`new_table`
STORED AS AVRO
LOCATION '%s'
TBLPROPERTIES ('avro.schema.literal'='{
  "fields": [
    {
      "name": "a",
      "type": "string"
    },
    {
      "name": "b",
      "type": "int"
    },
    {
      "name": "c",
      "type": "double"
    },
    {
      "logicalType": "decimal",
      "name": "d",
      "precision": 4,
      "scale": 2,
      "type": "bytes"
    }
  ],
  "name": "my_record",
  "type": "record"
}')""" % path
        assert result == expected

    def test_create_table_parquet(self):
        statement = _create_table('some_table', self.expr,
                                  database='bar',
                                  overwrite=True)
        result = statement.compile()

        expected = """\
CREATE TABLE bar.`some_table`
STORED AS PARQUET
AS
SELECT *
FROM functional_alltypes
WHERE `bigint_col` > 0"""
        assert result == expected

    def test_no_overwrite(self):
        statement = _create_table('tname', self.expr,
                                  overwrite=False)
        result = statement.compile()

        expected = """\
CREATE TABLE IF NOT EXISTS `tname`
STORED AS PARQUET
AS
SELECT *
FROM functional_alltypes
WHERE `bigint_col` > 0"""
        assert result == expected

    def test_avro_other_formats(self):
        statement = _create_table('tname', self.t, format='avro')
        result = statement.compile()
        expected = """\
CREATE TABLE IF NOT EXISTS `tname`
STORED AS AVRO
AS
SELECT *
FROM functional_alltypes"""
        assert result == expected

        self.assertRaises(ValueError, _create_table, 'tname', self.t,
                          format='foo')

    def test_partition_by(self):
        pass


class TestDistinct(unittest.TestCase):

    def setUp(self):
        self.con = MockConnection()

    def test_simple_table_distinct(self):
        t = self.con.table('functional_alltypes')

        expr = t[t.string_col, t.int_col].distinct()

        result = to_sql(expr)
        expected = """SELECT DISTINCT `string_col`, `int_col`
FROM functional_alltypes"""
        assert result == expected

    def test_array_distinct(self):
        t = self.con.table('functional_alltypes')
        expr = t.string_col.distinct()

        result = to_sql(expr)
        expected = """SELECT DISTINCT `string_col`
FROM functional_alltypes"""
        assert result == expected

    def test_count_distinct(self):
        t = self.con.table('functional_alltypes')

        metric = t.int_col.nunique().name('nunique')
        expr = t[t.bigint_col > 0].group_by('string_col').aggregate([metric])

        result = to_sql(expr)
        expected = """SELECT `string_col`, COUNT(DISTINCT `int_col`) AS `nunique`
FROM functional_alltypes
WHERE `bigint_col` > 0
GROUP BY 1"""
        assert result == expected

    def test_multiple_count_distinct(self):
        # Impala and some other databases will not execute multiple
        # count-distincts in a single aggregation query. This error reporting
        # will be left to the database itself, for now.
        t = self.con.table('functional_alltypes')
        metrics = [t.int_col.nunique().name('int_card'),
                   t.smallint_col.nunique().name('smallint_card')]

        expr = t.group_by('string_col').aggregate(metrics)

        result = to_sql(expr)
        expected = """SELECT `string_col`, COUNT(DISTINCT `int_col`) AS `int_card`,
       COUNT(DISTINCT `smallint_col`) AS `smallint_card`
FROM functional_alltypes
GROUP BY 1"""
        assert result == expected


class TestSubqueriesEtc(unittest.TestCase):

    def setUp(self):
        self.foo = api.table(
            [
                ('job', 'string'),
                ('dept_id', 'string'),
                ('year', 'int32'),
                ('y', 'double')
            ], 'foo')

        self.bar = api.table([
            ('x', 'double'),
            ('job', 'string')
        ], 'bar')

        self.t1 = api.table([
            ('key1', 'string'),
            ('key2', 'string'),
            ('value1', 'double')
        ], 'foo')

        self.t2 = api.table([
            ('key1', 'string'),
            ('key2', 'string')
        ], 'bar')

    def test_scalar_subquery_different_table(self):
        t1, t2 = self.foo, self.bar
        expr = t1[t1.y > t2.x.max()]

        result = to_sql(expr)
        expected = """SELECT *
FROM foo
WHERE `y` > (
  SELECT max(`x`) AS `tmp`
  FROM bar
)"""
        assert result == expected

    def test_where_uncorrelated_subquery(self):
        expr = self.foo[self.foo.job.isin(self.bar.job)]

        result = to_sql(expr)
        expected = """SELECT *
FROM foo
WHERE `job` IN (
  SELECT `job`
  FROM bar
)"""
        assert result == expected

    def test_where_correlated_subquery(self):
        t1 = self.foo
        t2 = t1.view()

        stat = t2[t1.dept_id == t2.dept_id].y.mean()
        expr = t1[t1.y > stat]

        result = to_sql(expr)
        expected = """SELECT t0.*
FROM foo t0
WHERE t0.`y` > (
  SELECT avg(t1.`y`) AS `tmp`
  FROM foo t1
  WHERE t0.`dept_id` = t1.`dept_id`
)"""
        assert result == expected

    def test_where_array_correlated(self):
        # Test membership in some record-dependent values, if this is supported
        pass

    def test_exists_semi_join_case(self):
        t1, t2 = self.t1, self.t2

        cond = (t1.key1 == t2.key1).any()
        expr = t1[cond]

        result = to_sql(expr)
        expected = """SELECT t0.*
FROM foo t0
WHERE EXISTS (
  SELECT 1
  FROM bar t1
  WHERE t0.`key1` = t1.`key1`
)"""
        assert result == expected

        cond2 = ((t1.key1 == t2.key1) & (t2.key2 == 'foo')).any()
        expr2 = t1[cond2]

        result = to_sql(expr2)
        expected = """SELECT t0.*
FROM foo t0
WHERE EXISTS (
  SELECT 1
  FROM bar t1
  WHERE t0.`key1` = t1.`key1` AND
        t1.`key2` = 'foo'
)"""
        assert result == expected

    def test_not_exists_anti_join_case(self):
        t1, t2 = self.t1, self.t2

        cond = (t1.key1 == t2.key1).any()
        expr = t1[-cond]

        result = to_sql(expr)
        expected = """SELECT t0.*
FROM foo t0
WHERE NOT EXISTS (
  SELECT 1
  FROM bar t1
  WHERE t0.`key1` = t1.`key1`
)"""
        assert result == expected


class TestUDFStatements(unittest.TestCase):

    def setUp(self):
        self.con = MockConnection()
        self.name = 'test_name'
        self.inputs = ['string', 'string']
        self.output = 'int64'

    def test_create_udf(self):
        stmt = ddl.CreateFunction('/foo/bar.so', 'testFunc', self.inputs,
                                  self.output, self.name)
        result = stmt.compile()
        expected = ("CREATE FUNCTION test_name(string, string) returns bigint "
                    "location '/foo/bar.so' symbol='testFunc'")
        assert result == expected

    def test_create_udf_type_conversions(self):
        stmt = ddl.CreateFunction('/foo/bar.so', 'testFunc',
                                  ['string', 'int8', 'int16', 'int32'],
                                  self.output, self.name)
        result = stmt.compile()
        expected = ("CREATE FUNCTION test_name(string, tinyint, "
                    "smallint, int) returns bigint "
                    "location '/foo/bar.so' symbol='testFunc'")
        assert result == expected

    def test_delete_udf_simple(self):
        stmt = ddl.DropFunction(self.name, self.inputs)
        result = stmt.compile()
        expected = "DROP FUNCTION test_name(string, string)"
        assert result == expected

    def test_delete_udf_if_exists(self):
        stmt = ddl.DropFunction(self.name, self.inputs, must_exist=False)
        result = stmt.compile()
        expected = "DROP FUNCTION IF EXISTS test_name(string, string)"
        assert result == expected

    def test_delete_udf_aggregate(self):
        stmt = ddl.DropFunction(self.name, self.inputs, aggregate=True)
        result = stmt.compile()
        expected = "DROP AGGREGATE FUNCTION test_name(string, string)"
        assert result == expected

    def test_delete_udf_db(self):
        stmt = ddl.DropFunction(self.name, self.inputs, database='test')
        result = stmt.compile()
        expected = "DROP FUNCTION test.test_name(string, string)"
        assert result == expected

    def test_create_uda(self):
        stmt = ddl.CreateAggregateFunction('/foo/bar.so', self.inputs,
                                           self.output, 'Init', 'Update',
                                           'Merge', 'Finalize', self.name)
        result = stmt.compile()
        expected = ("CREATE AGGREGATE FUNCTION test_name(string, string)"
                    " returns bigint location '/foo/bar.so'"
                    " init_fn='Init' update_fn='Update'"
                    " merge_fn='Merge' finalize_fn='Finalize'")
        assert result == expected

    def test_list_udf(self):
        stmt = ddl.ListFunction('test')
        result = stmt.compile()
        expected = 'SHOW FUNCTIONS IN test'
        assert result == expected

    def test_list_udfs_like(self):
        stmt = ddl.ListFunction('test', like='identity')
        result = stmt.compile()
        expected = "SHOW FUNCTIONS IN test LIKE 'identity'"
        assert result == expected

    def test_list_udafs(self):
        stmt = ddl.ListFunction('test', aggregate=True)
        result = stmt.compile()
        expected = 'SHOW AGGREGATE FUNCTIONS IN test'
        assert result == expected

    def test_list_udafs_like(self):
        stmt = ddl.ListFunction('test', like='identity', aggregate=True)
        result = stmt.compile()
        expected = "SHOW AGGREGATE FUNCTIONS IN test LIKE 'identity'"
        assert result == expected
