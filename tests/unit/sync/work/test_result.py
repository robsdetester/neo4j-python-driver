# Copyright (c) "Neo4j"
# Neo4j Sweden AB [http://neo4j.com]
#
# This file is part of Neo4j.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from unittest import mock
import warnings

import pandas as pd
import pytest

from neo4j import (
    Address,
    Record,
    Result,
    ResultSummary,
    ServerInfo,
    SummaryCounters,
    Version,
)
from neo4j._async_compat.util import Util
from neo4j.data import (
    DataHydrator,
    Node,
    Relationship,
)
from neo4j.exceptions import (
    ResultConsumedError,
    ResultNotSingleError,
)
from neo4j.packstream import Structure

from ...._async_compat import mark_sync_test


class Records:
    def __init__(self, fields, records):
        self.fields = tuple(fields)
        self.records = tuple(records)
        assert all(len(self.fields) == len(r) for r in self.records)

    def __len__(self):
        return self.records.__len__()

    def __iter__(self):
        return self.records.__iter__()

    def __getitem__(self, item):
        return self.records.__getitem__(item)


class ConnectionStub:
    class Message:
        def __init__(self, message, *args, **kwargs):
            self.message = message
            self.args = args
            self.kwargs = kwargs

        def _cb(self, cb_name, *args, **kwargs):
            # print(self.message, cb_name.upper(), args, kwargs)
            cb = self.kwargs.get(cb_name)
            Util.callback(cb, *args, **kwargs)

        def on_success(self, metadata):
            self._cb("on_success", metadata)

        def on_summary(self):
            self._cb("on_summary")

        def on_records(self, records):
            self._cb("on_records", records)

        def __eq__(self, other):
            return self.message == other

        def __repr__(self):
            return "Message(%s)" % self.message

    def __init__(self, records=None, run_meta=None, summary_meta=None,
                 force_qid=False):
        self._multi_result = isinstance(records, (list, tuple))
        if self._multi_result:
            self._records = records
            self._use_qid = True
        else:
            self._records = records,
            self._use_qid = force_qid
        self.fetch_idx = 0
        self._qid = -1
        self.most_recent_qid = None
        self.record_idxs = [0] * len(self._records)
        self.to_pull = [None] * len(self._records)
        self._exhausted = [False] * len(self._records)
        self.queued = []
        self.sent = []
        self.run_meta = run_meta
        self.summary_meta = summary_meta
        ConnectionStub.server_info.update({"server": "Neo4j/4.3.0"})
        self.unresolved_address = None

    def send_all(self):
        self.sent += self.queued
        self.queued = []

    def fetch_message(self):
        if self.fetch_idx >= len(self.sent):
            pytest.fail("Waits for reply to never sent message")
        msg = self.sent[self.fetch_idx]
        if msg == "RUN":
            self.fetch_idx += 1
            self._qid += 1
            meta = {"fields": self._records[self._qid].fields,
                    **(self.run_meta or {})}
            if self._use_qid:
                meta.update(qid=self._qid)
            msg.on_success(meta)
        elif msg == "DISCARD":
            self.fetch_idx += 1
            qid = msg.kwargs.get("qid", -1)
            if qid < 0:
                qid = self._qid
            self.record_idxs[qid] = len(self._records[qid])
            msg.on_success(self.summary_meta or {})
            msg.on_summary()
        elif msg == "PULL":
            qid = msg.kwargs.get("qid", -1)
            if qid < 0:
                qid = self._qid
            if self._exhausted[qid]:
                pytest.fail("PULLing exhausted result")
            if self.to_pull[qid] is None:
                n = msg.kwargs.get("n", -1)
                if n < 0:
                    n = len(self._records[qid])
                self.to_pull[qid] = \
                    min(n, len(self._records[qid]) - self.record_idxs[qid])
                # if to == len(self._records):
                #     self.fetch_idx += 1
            if self.to_pull[qid] > 0:
                record = self._records[qid][self.record_idxs[qid]]
                self.record_idxs[qid] += 1
                self.to_pull[qid] -= 1
                msg.on_records([record])
            elif self.to_pull[qid] == 0:
                self.to_pull[qid] = None
                self.fetch_idx += 1
                if self.record_idxs[qid] < len(self._records[qid]):
                    msg.on_success({"has_more": True})
                else:
                    msg.on_success(
                        {"bookmark": "foo", **(self.summary_meta or {})}
                    )
                    self._exhausted[qid] = True
                    msg.on_summary()

    def fetch_all(self):
        while self.fetch_idx < len(self.sent):
            self.fetch_message()

    def run(self, *args, **kwargs):
        self.queued.append(ConnectionStub.Message("RUN", *args, **kwargs))

    def discard(self, *args, **kwargs):
        self.queued.append(ConnectionStub.Message("DISCARD", *args, **kwargs))

    def pull(self, *args, **kwargs):
        self.queued.append(ConnectionStub.Message("PULL", *args, **kwargs))

    server_info = ServerInfo(Address(("bolt://localhost", 7687)), Version(4, 3))

    def defunct(self):
        return False


class HydratorStub(DataHydrator):
    def hydrate(self, values):
        return values


def noop(*_, **__):
    pass


def fetch_and_compare_all_records(
    result, key, expected_records, method, limit=None
):
    received_records = []
    if method == "for loop":
        for record in result:
            assert isinstance(record, Record)
            received_records.append([record.data().get(key, None)])
            if limit is not None and len(received_records) == limit:
                break
        if limit is None:
            assert result._exhausted
    elif method == "next":
        n = len(expected_records) if limit is None else limit
        for _ in range(n):
            record = Util.next(result)
            received_records.append([record.get(key, None)])
        if limit is None:
            with pytest.raises(StopIteration):
                Util.next(result)
            assert result._exhausted
    elif method == "one iter":
        iter_ = Util.iter(result)
        n = len(expected_records) if limit is None else limit
        for _ in range(n):
            record = Util.next(iter_)
            received_records.append([record.get(key, None)])
        if limit is None:
            with pytest.raises(StopIteration):
                Util.next(iter_)
            assert result._exhausted
    elif method == "new iter":
        n = len(expected_records) if limit is None else limit
        for _ in range(n):
            iter_ = Util.iter(result)
            record = Util.next(iter_)
            received_records.append([record.get(key, None)])
        if limit is None:
            iter_ = Util.iter(result)
            with pytest.raises(StopIteration):
                Util.next(iter_)
            assert result._exhausted
    else:
        raise ValueError()
    assert received_records == expected_records


@pytest.mark.parametrize("method",
                         ("for loop", "next", "one iter",  "new iter"))
@pytest.mark.parametrize("records", (
    [],
    [[42]],
    [[1], [2], [3], [4], [5]],
))
@mark_sync_test
def test_result_iteration(method, records):
    connection = ConnectionStub(records=Records(["x"], records))
    result = Result(connection, HydratorStub(), 2, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    fetch_and_compare_all_records(result, "x", records, method)


@mark_sync_test
def test_result_iteration_mixed_methods():
    records = [[i] for i in range(10)]
    connection = ConnectionStub(records=Records(["x"], records))
    result = Result(connection, HydratorStub(), 4, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    iter1 = Util.iter(result)
    iter2 = Util.iter(result)
    assert (Util.next(iter1)).get("x") == records[0][0]
    assert (Util.next(iter2)).get("x") == records[1][0]
    assert (Util.next(iter2)).get("x") == records[2][0]
    assert (Util.next(iter1)).get("x") == records[3][0]
    assert (Util.next(iter1)).get("x") == records[4][0]
    assert (Util.next(result)).get("x") == records[5][0]
    assert (Util.next(iter2)).get("x") == records[6][0]
    assert (Util.next(iter1)).get("x") == records[7][0]
    assert ((Util.next(Util.iter(result))).get("x")
            == records[8][0])
    assert [r.get("x") for r in result] == [records[9][0]]
    with pytest.raises(StopIteration):
        Util.next(iter1)
    with pytest.raises(StopIteration):
        Util.next(iter2)
    with pytest.raises(StopIteration):
        Util.next(result)
    with pytest.raises(StopIteration):
        Util.next(Util.iter(result))
    assert [r.get("x") for r in result] == []


@pytest.mark.parametrize("method",
                         ("for loop", "next", "one iter",  "new iter"))
@pytest.mark.parametrize("invert_fetch", (True, False))
@mark_sync_test
def test_parallel_result_iteration(method, invert_fetch):
    records1 = [[i] for i in range(1, 6)]
    records2 = [[i] for i in range(6, 11)]
    connection = ConnectionStub(
        records=(Records(["x"], records1), Records(["x"], records2))
    )
    result1 = Result(connection, HydratorStub(), 2, noop, noop)
    result1._run("CYPHER1", {}, None, None, "r", None)
    result2 = Result(connection, HydratorStub(), 2, noop, noop)
    result2._run("CYPHER2", {}, None, None, "r", None)
    if invert_fetch:
        fetch_and_compare_all_records(
            result2, "x", records2, method
        )
        fetch_and_compare_all_records(
            result1, "x", records1, method
        )
    else:
        fetch_and_compare_all_records(
            result1, "x", records1, method
        )
        fetch_and_compare_all_records(
            result2, "x", records2, method
        )


@pytest.mark.parametrize("method",
                         ("for loop", "next", "one iter",  "new iter"))
@pytest.mark.parametrize("invert_fetch", (True, False))
@mark_sync_test
def test_interwoven_result_iteration(method, invert_fetch):
    records1 = [[i] for i in range(1, 10)]
    records2 = [[i] for i in range(11, 20)]
    connection = ConnectionStub(
        records=(Records(["x"], records1), Records(["y"], records2))
    )
    result1 = Result(connection, HydratorStub(), 2, noop, noop)
    result1._run("CYPHER1", {}, None, None, "r", None)
    result2 = Result(connection, HydratorStub(), 2, noop, noop)
    result2._run("CYPHER2", {}, None, None, "r", None)
    start = 0
    for n in (1, 2, 3, 1, None):
        end = n if n is None else start + n
        if invert_fetch:
            fetch_and_compare_all_records(
                result2, "y", records2[start:end], method, n
            )
            fetch_and_compare_all_records(
                result1, "x", records1[start:end], method, n
            )
        else:
            fetch_and_compare_all_records(
                result1, "x", records1[start:end], method, n
            )
            fetch_and_compare_all_records(
                result2, "y", records2[start:end], method, n
            )
        start = end


@pytest.mark.parametrize("records", ([[1], [2]], [[1]], []))
@pytest.mark.parametrize("fetch_size", (1, 2))
@mark_sync_test
def test_result_peek(records, fetch_size):
    connection = ConnectionStub(records=Records(["x"], records))
    result = Result(connection, HydratorStub(), fetch_size, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    for i in range(len(records) + 1):
        record = result.peek()
        if i == len(records):
            assert record is None
        else:
            assert isinstance(record, Record)
            assert record.get("x") == records[i][0]
            iter_ = Util.iter(result)
            Util.next(iter_)  # consume the record


@pytest.mark.parametrize("records", ([[1], [2]], [[1]], []))
@pytest.mark.parametrize("fetch_size", (1, 2))
@pytest.mark.parametrize("default", (True, False))
@mark_sync_test
def test_result_single_non_strict(records, fetch_size, default):
    kwargs = {}
    if not default:
        kwargs["strict"] = False

    connection = ConnectionStub(records=Records(["x"], records))
    result = Result(connection, HydratorStub(), fetch_size, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    if len(records) == 0:
        assert result.single(**kwargs) is None
    else:
        if len(records) == 1:
            record = result.single(**kwargs)
        else:
            with pytest.warns(Warning, match="multiple"):
                record = result.single(**kwargs)
        assert isinstance(record, Record)
        assert record.get("x") == records[0][0]


@pytest.mark.parametrize("records", ([[1], [2]], [[1]], []))
@pytest.mark.parametrize("fetch_size", (1, 2))
@mark_sync_test
def test_result_single_strict(records, fetch_size):
    connection = ConnectionStub(records=Records(["x"], records))
    result = Result(connection, HydratorStub(), fetch_size, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    try:
        record = result.single(strict=True)
    except ResultNotSingleError as exc:
        assert len(records) != 1
        if len(records) == 0:
            assert exc is not None
            assert "no records" in str(exc).lower()
        elif len(records) > 1:
            assert exc is not None
            assert "more than one record" in str(exc).lower()

    else:
        assert len(records) == 1
        assert isinstance(record, Record)
        assert record.get("x") == records[0][0]


@pytest.mark.parametrize("records", (
    [[1], [2], [3]], [[1]], [], [[i] for i in range(100)]
))
@pytest.mark.parametrize("fetch_size", (1, 2))
@pytest.mark.parametrize("strict", (True, False))
@mark_sync_test
def test_result_single_exhausts_records(records, fetch_size, strict):
    connection = ConnectionStub(records=Records(["x"], records))
    result = Result(connection, HydratorStub(), fetch_size, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result.single(strict=strict)
    except ResultNotSingleError:
        pass

    assert not result.closed()  # close has nothing to do with being exhausted
    assert [r for r in result] == []
    assert not result.closed()


@pytest.mark.parametrize("records", (
    [[1], [2], [3]], [[1]], [], [[i] for i in range(100)]
))
@pytest.mark.parametrize("fetch_size", (1, 2))
@pytest.mark.parametrize("strict", (True, False))
@mark_sync_test
def test_result_fetch(records, fetch_size, strict):
    connection = ConnectionStub(records=Records(["x"], records))
    result = Result(connection, HydratorStub(), fetch_size, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    assert result.fetch(0) == []
    assert result.fetch(-1) == []
    assert [[r.get("x")] for r in result.fetch(2)] == records[:2]
    assert [[r.get("x")] for r in result.fetch(1)] == records[2:3]
    assert [[r.get("x")] for r in result] == records[3:]


@mark_sync_test
def test_keys_are_available_before_and_after_stream():
    connection = ConnectionStub(records=Records(["x"], [[1], [2]]))
    result = Result(connection, HydratorStub(), 1, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    assert list(result.keys()) == ["x"]
    Util.list(result)
    assert list(result.keys()) == ["x"]


@pytest.mark.parametrize("records", ([[1], [2]], [[1]], []))
@pytest.mark.parametrize("consume_one", (True, False))
@pytest.mark.parametrize("summary_meta", (None, {"database": "foobar"}))
@pytest.mark.parametrize("consume_times", (1, 2))
@mark_sync_test
def test_consume(records, consume_one, summary_meta, consume_times):
    connection = ConnectionStub(
        records=Records(["x"], records), summary_meta=summary_meta
    )
    result = Result(connection, HydratorStub(), 1, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    if consume_one:
        try:
            Util.next(Util.iter(result))
        except StopIteration:
            pass
    for _ in range(consume_times):
        summary = result.consume()
        assert isinstance(summary, ResultSummary)
        if summary_meta and "db" in summary_meta:
            assert summary.database == summary_meta["db"]
        else:
            assert summary.database is None
        server_info = summary.server
        assert isinstance(server_info, ServerInfo)
        assert server_info.protocol_version == Version(4, 3)
        assert isinstance(summary.counters, SummaryCounters)


@pytest.mark.parametrize("t_first", (None, 0, 1, 123456789))
@pytest.mark.parametrize("t_last", (None, 0, 1, 123456789))
@mark_sync_test
def test_time_in_summary(t_first, t_last):
    run_meta = None
    if t_first is not None:
        run_meta = {"t_first": t_first}
    summary_meta = None
    if t_last is not None:
        summary_meta = {"t_last": t_last}
    connection = ConnectionStub(
        records=Records(["n"], [[i] for i in range(100)]), run_meta=run_meta,
        summary_meta=summary_meta
    )

    result = Result(connection, HydratorStub(), 1, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    summary = result.consume()

    if t_first is not None:
        assert isinstance(summary.result_available_after, int)
        assert summary.result_available_after == t_first
    else:
        assert summary.result_available_after is None
    if t_last is not None:
        assert isinstance(summary.result_consumed_after, int)
        assert summary.result_consumed_after == t_last
    else:
        assert summary.result_consumed_after is None
    assert not hasattr(summary, "t_first")
    assert not hasattr(summary, "t_last")


@mark_sync_test
def test_counts_in_summary():
    connection = ConnectionStub(records=Records(["n"], [[1], [2]]))

    result = Result(connection, HydratorStub(), 1, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    summary = result.consume()

    assert isinstance(summary.counters, SummaryCounters)


@pytest.mark.parametrize("query_type", ("r", "w", "rw", "s"))
@mark_sync_test
def test_query_type(query_type):
    connection = ConnectionStub(
        records=Records(["n"], [[1], [2]]), summary_meta={"type": query_type}
    )

    result = Result(connection, HydratorStub(), 1, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    summary = result.consume()

    assert isinstance(summary.query_type, str)
    assert summary.query_type == query_type


@pytest.mark.parametrize("num_records", range(0, 5))
@mark_sync_test
def test_data(num_records):
    connection = ConnectionStub(
        records=Records(["n"], [[i + 1] for i in range(num_records)])
    )

    result = Result(connection, HydratorStub(), 1, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    result._buffer_all()
    records = result._record_buffer.copy()
    assert len(records) == num_records
    expected_data = []
    for i, record in enumerate(records):
        record.data = mock.Mock()
        expected_data.append("magic_return_%s" % i)
        record.data.return_value = expected_data[-1]
    assert result.data("hello", "world") == expected_data
    for record in records:
        assert record.data.called_once_with("hello", "world")


@pytest.mark.parametrize(
    ("keys", "values", "types", "instances"),
    (
        (["i"], list(zip(range(5))), ["int64"], None),
        (["x"], list(zip((n - .5) / 5 for n in range(5))), ["float64"], None),
        (["s"], list(zip(("foo", "bar", "baz", "foobar"))), ["object"], None),
        (["l"], list(zip(([1, 2], [3, 4]))), ["object"], None),
        (
            ["n"],
            list(zip((
                Structure(b"N", 0, ["LABEL_A"], {"a": 1, "b": 2}),
                Structure(b"N", 2, ["LABEL_B"], {"a": 1, "c": 1.2}),
                Structure(b"N", 1, ["LABEL_A", "LABEL_B"], {"a": [1, "a"]}),
                Structure(b"N", None, ["LABEL_A", "LABEL_B"], {"a": [1, "a"]},
                          "cool_id"),
            ))),
            ["object"],
            [Node]
        ),
        (
            ["r"],
            list(zip((
                Structure(b"R", 0, 1, 2, "TYPE", {"a": 1, "b": 2}),
                Structure(b"R", 420, 1337, 69, "HYPE", {"all memes": True}),
                Structure(b"R", None, None, None, "HYPE", {"all memes": True},
                          "420", "1337", "69"),
            ))),
            ["object"],
            [Relationship]
        ),
    )
)
@pytest.mark.parametrize("test_default_expand", (True, False))
@mark_sync_test
def test_to_df(keys, values, types, instances, test_default_expand):
    connection = ConnectionStub(records=Records(keys, values))
    result = Result(connection, DataHydrator(), 1, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    if test_default_expand:
        df = result.to_df()
    else:
        df = result.to_df(expand=False)

    assert isinstance(df, pd.DataFrame)
    assert df.keys().to_list() == keys
    assert len(df) == len(values)
    assert df.dtypes.to_list() == types

    expected_df = pd.DataFrame(
        {k: [v[i] for v in values] for i, k in enumerate(keys)}
    )

    if instances:
        for i, k in enumerate(keys):
            assert all(isinstance(v, instances[i]) for v in df[k])
    else:
        assert df.equals(expected_df)


@pytest.mark.parametrize(
    ("keys", "values", "expected_columns", "expected_rows", "expected_types"),
    (
        (
            ["i"],
            list(zip(range(5))),
            ["i"],
            [[0], [1], [2], [3], [4]],
            ["int64"],
        ),
        # test variable name escaping
        (
            ["i.[]->.().{}.\\"],
            list(zip(range(5))),
            ["i\\.[]->\\.()\\.{}\\.\\\\"],
            [[0], [1], [2], [3], [4]],
            ["int64"],
        ),
        (
            ["x"],
            list(zip((n - .5) / 5 for n in range(5))),
            ["x"],
            [[-0.1], [0.1], [0.3], [0.5], [0.7]],
            ["float64"],
        ),
        (
            ["s"],
            list(zip(("foo", "bar", "baz", "foobar"))),
            ["s"],
            [["foo"], ["bar"], ["baz"], ["foobar"]],
            ["object"],
        ),
        (
            ["l"],
            list(zip(([1, 2], [3, 4]))),
            ["l[].0", "l[].1"],
            [[1, 2], [3, 4]],
            ["int64", "int64"],
        ),
        (
            ["l"],
            list(zip(([1, 2], [3, 4, 5], [6]))),
            ["l[].0", "l[].1", "l[].2"],
            [[1, 2, None], [3, 4, 5], [6, None, None]],
            # pandas turns None in int columns into NaN
            # which requires the column to become a float column
            ["int64", "float64", "float64"],
        ),
        (
            ["d"],
            list(zip(({"a": 1, "b": 2}, {"a": 3, "b": 4, "": 0}))),
            ["d{}.a", "d{}.b", "d{}."],
            [[1, 2, None], [3, 4, 0]],
            ["int64", "int64", "float64"],
        ),
        # test key escaping
        (
            ["d"],
            list(zip(({"a.[]\\{}->.().{}.": 1, "b": 2},))),
            ["d{}.a\\.[]\\\\{}->\\.()\\.{}\\.", "d{}.b"],
            [[1, 2]],
            ["int64", "int64"],
        ),
        (
            ["d"],
            list(zip(({"a": 1, "b": 2}, {"a": 3, "c": 4}))),
            ["d{}.a", "d{}.b", "d{}.c"],
            [[1, 2, None], [3, None, 4]],
            # pandas turns None in int columns into NaN
            # which requires the column to become a float column
            ["int64", "float64", "float64"],
        ),
        (
            ["x"],
            list(zip(([{"foo": "bar", "baz": [42, 0.1]}, "foobar"],))),
            ["x[].0{}.foo", "x[].0{}.baz[].0", "x[].0{}.baz[].1", "x[].1"],
            [["bar",  42, 0.1, "foobar"]],
            ["object", "int64", "float64", "object"],
        ),
        (
            ["n"],
            list(zip((
                Structure(b"N", 0, ["LABEL_A"],
                          {"a": 1, "b": 2, "d": 1}, "00"),
                Structure(b"N", 2, ["LABEL_B"],
                          {"a": 1, "c": 1.2, "d": 2}, "02"),
                Structure(b"N", 1, ["LABEL_A", "LABEL_B"],
                          {"a": [1, "a"], "d": 3}, "01"),
            ))),
            [
                "n().element_id", "n().labels", "n().prop.a", "n().prop.b",
                "n().prop.c", "n().prop.d"
            ],
            [
                ["00", frozenset(("LABEL_A",)), 1, 2, None, 1],
                ["02", frozenset(("LABEL_B",)), 1, None, 1.2, 2],
                [
                    "01", frozenset(("LABEL_A", "LABEL_B")),
                    [1, "a"], None, None, 3
                ],
            ],
            ["object", "object", "object", "float64", "float64", "int64"],
        ),
        (
            ["r"],
            list(zip((
                Structure(b"R", 0, 1, 2, "TYPE", {"a": 1, "all memes": False},
                          "r-0", "r-1", "r-2"),
                Structure(b"R", 420, 1337, 69, "HYPE", {"all memes": True},
                          "r-420", "r-1337", "r-69"),
            ))),
            [
                "r->.element_id", "r->.start.element_id", "r->.end.element_id",
                "r->.type", "r->.prop.a", "r->.prop.all memes"
            ],
            [
                ["r-0", "r-1", "r-2", "TYPE", 1, False],
                ["r-420", "r-1337", "r-69", "HYPE", None, True],
            ],
            ["object", "object", "object", "object", "float64", "bool"],
        ),
    )
)
@mark_sync_test
def test_to_df_expand(keys, values, expected_columns, expected_rows,
                            expected_types):
    connection = ConnectionStub(records=Records(keys, values))
    result = Result(connection, DataHydrator(), 1, noop, noop)
    result._run("CYPHER", {}, None, None, "r", None)
    df = result.to_df(expand=True)

    assert isinstance(df, pd.DataFrame)
    assert len(set(expected_columns)) == len(expected_columns)
    assert set(df.keys().to_list()) == set(expected_columns)

    # We don't expect the columns to be in a specific order.
    # Hence, we need to sort them before comparing.
    new_order = [df.keys().get_loc(ex_c) for ex_c in expected_columns]
    expected_rows = [
        [row[i] for i in new_order]
        for row in expected_rows
    ]
    expected_types = [expected_types[i] for i in new_order]
    expected_columns = [expected_columns[i] for i in new_order]

    assert len(df) == len(values)
    assert df.dtypes.to_list() == expected_types

    expected_df = pd.DataFrame(expected_rows, columns=expected_columns)
    assert df.equals(expected_df)