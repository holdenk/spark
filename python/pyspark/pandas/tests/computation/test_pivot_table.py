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

        # A Spark SQL function name names the function to apply, whatever its case.
        self.assert_eq(
            psdf.pivot_table(index=["c"], columns="a", values="b", aggfunc="SUM").sort_index(),
            pdf.pivot_table(index=["c"], columns="a", values="b", aggfunc="sum").sort_index(),
            almost=True,
        )

        # ... but a value that is more, or less, than a function name is rejected rather than
        # ending up as part of the aggregate expression.
        expected_error_message = (
            r"aggregate function must be a Spark SQL function name such as 'sum', "
            r"optionally qualified with a catalog and a database, got "
        )
        # the rejected shapes themselves are enumerated in test_validate_agg_func_name; what
        # matters here is that each way of passing an aggfunc reaches the check
        aggfunc = "first((SELECT 1)) as `b` -- "
        with self.assertRaisesRegex(ValueError, expected_error_message):
            psdf.pivot_table(index=["c"], columns="a", values="b", aggfunc=aggfunc)
        with self.assertRaisesRegex(ValueError, expected_error_message):
            psdf.pivot_table(index=["c"], columns="a", values=["b"], aggfunc=aggfunc)
        with self.assertRaisesRegex(ValueError, expected_error_message):
            psdf.pivot_table(index=["c"], columns="a", values="b", aggfunc={"b": aggfunc})
        # every entry of the dict is checked, not just the first one
        with self.assertRaisesRegex(ValueError, expected_error_message):
            psdf.pivot_table(
                index=["c"], columns="a", values=["b", "e"], aggfunc={"b": "sum", "e": aggfunc}
            )


class PivotTableTests(
    PivotTableMixin,
    PandasOnSparkTestCase,
):
    pass


if __name__ == "__main__":
    from pyspark.testing import main

    main()
