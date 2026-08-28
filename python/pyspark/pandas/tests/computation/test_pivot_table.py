#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import numpy as np
import pandas as pd

from pyspark import pandas as ps
from pyspark.errors import ParseException
from pyspark.testing.pandasutils import PandasOnSparkTestCase


class PivotTableMixin:
    def test_pivot_table(self):
        pdf = pd.DataFrame(
            {
                "a": [4, 2, 3, 4, 8, 6],
                "b": [1, 2, 2, 4, 2, 4],
                "e": [10, 20, 20, 40, 20, 40],
                "c": [1, 2, 9, 4, 7, 4],
                "d": [-1, -2, -3, -4, -5, -6],
            },
            index=np.random.rand(6),
        )
        psdf = ps.from_pandas(pdf)

        self.assert_eq(
            psdf.pivot_table(columns="a", values="b").sort_index(),
            pdf.pivot_table(columns="a", values="b").sort_index(),
            almost=True,
        )

        self.assert_eq(
            psdf.pivot_table(index=["c"], columns="a", values="b").sort_index(),
            pdf.pivot_table(index=["c"], columns="a", values="b").sort_index(),
            almost=True,
        )

        self.assert_eq(
            psdf.pivot_table(index=["c"], columns="a", values="b", aggfunc="sum").sort_index(),
            pdf.pivot_table(index=["c"], columns="a", values="b", aggfunc="sum").sort_index(),
            almost=True,
        )

        self.assert_eq(
            psdf.pivot_table(index=["c"], columns="a", values=["b"], aggfunc="sum").sort_index(),
            pdf.pivot_table(index=["c"], columns="a", values=["b"], aggfunc="sum").sort_index(),
            almost=True,
        )

        self.assert_eq(
            psdf.pivot_table(
                index=["c"], columns="a", values=["b", "e"], aggfunc="sum"
            ).sort_index(),
            pdf.pivot_table(
                index=["c"], columns="a", values=["b", "e"], aggfunc="sum"
            ).sort_index(),
            almost=True,
        )

    def test_pivot_table_aggfunc_name(self):
        pdf = pd.DataFrame(
            {"a": [4, 2, 3, 4], "b": [1, 2, 2, 4], "c": [1, 2, 9, 4], "e": [10, 20, 20, 40]}
        )
        psdf = ps.from_pandas(pdf)

        # A Spark SQL function name names the function to apply, whatever its case, and it may
        # be qualified with a catalog and a database.
        for aggfunc in ["SUM", "system.builtin.sum"]:
            with self.subTest(aggfunc=aggfunc):
                self.assert_eq(
                    psdf.pivot_table(
                        index=["c"], columns="a", values="b", aggfunc=aggfunc
                    ).sort_index(),
                    pdf.pivot_table(
                        index=["c"], columns="a", values="b", aggfunc="sum"
                    ).sort_index(),
                    almost=True,
                )

        # ... but the name is given to Spark as a name, not as a fragment of the aggregate
        # expression, so a value carrying anything further does not parse as one. What matters
        # here is that each way of passing an aggfunc names it the same way; the shapes that do
        # not parse are enumerated in GroupbyAggregateMixin.test_aggregate_func_name.
        aggfunc = "first((SELECT 1)) as `b` -- "
        with self.assertRaises(ParseException):
            psdf.pivot_table(index=["c"], columns="a", values="b", aggfunc=aggfunc).to_pandas()
        with self.assertRaises(ParseException):
            psdf.pivot_table(index=["c"], columns="a", values=["b"], aggfunc=aggfunc).to_pandas()
        with self.assertRaises(ParseException):
            psdf.pivot_table(
                index=["c"], columns="a", values=["b"], aggfunc={"b": aggfunc}
            ).to_pandas()
        # every entry of the dict names its own aggregation, not just the first one
        with self.assertRaises(ParseException):
            psdf.pivot_table(
                index=["c"], columns="a", values=["b", "e"], aggfunc={"b": "sum", "e": aggfunc}
            ).to_pandas()


class PivotTableTests(
    PivotTableMixin,
    PandasOnSparkTestCase,
):
    pass


if __name__ == "__main__":
    from pyspark.testing import main

    main()
