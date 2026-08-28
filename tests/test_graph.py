# -*- coding: utf-8 -*-
"""GraphStore 测试：实体/关系 CRUD、边端点校验、语义检索 top_k 排序。

使用固定 hash 分词向量函数作为 embed_callable，使语义检索结果确定可复现。
"""

import hashlib

import pytest

from lite.graph import GraphStore
from lite.memory.vector_store import InMemoryVectorStore


def fixed_embed(text, dim=16):
    """固定 hash 分词向量：按词划分并散列到固定维度（确定性，与模型无关）。"""
    vector = [0.0] * dim
    words = text.split()
    for word in words:
        digest = int(hashlib.sha256(word.encode()).hexdigest(), 16)
        vector[digest % dim] += 1.0
    return vector


@pytest.fixture()
def graph():
    """返回使用 :memory: 数据库 + InMemoryVectorStore + 固定向量的 GraphStore。"""
    store = GraphStore(db_path=":memory:", embed_callable=fixed_embed)
    yield store
    store.close()


# ------------------------------------------------------------------ #
# 实体 CRUD
# ------------------------------------------------------------------ #
def test_upsert_and_get_node(graph):
    graph.upsert_node("n1", "Alice", "person", {"age": 30})
    node = graph.get_node("n1")
    assert node is not None
    assert node["id"] == "n1"
    assert node["name"] == "Alice"
    assert node["type"] == "person"
    assert node["properties"] == {"age": 30}
    assert node["created_at"] and node["updated_at"]


def test_upsert_overwrite_keeps_id(graph):
    graph.upsert_node("n1", "Alice", "person", {"age": 30})
    graph.upsert_node("n1", "Alice2", "person", {"age": 31})
    node = graph.get_node("n1")
    assert node["name"] == "Alice2"
    assert node["properties"] == {"age": 31}


def test_get_missing_node(graph):
    assert graph.get_node("ghost") is None


def test_list_nodes(graph):
    graph.upsert_node("n1", "Alice", "person")
    graph.upsert_node("n2", "Bob", "person")
    names = [n["name"] for n in graph.list_nodes()]
    assert set(names) == {"Alice", "Bob"}
    assert len(graph.list_nodes()) == 2


# ------------------------------------------------------------------ #
# 边 CRUD 与校验
# ------------------------------------------------------------------ #
def test_add_edge_and_get(graph):
    graph.upsert_node("n1", "Alice", "person")
    graph.upsert_node("n2", "Bob", "person")
    edge = graph.add_edge("e1", "n1", "n2", "knows", {"weight": 1})
    assert edge["source"] == "n1"
    assert edge["target"] == "n2"
    assert edge["relation"] == "knows"
    assert edge["properties"] == {"weight": 1}


def test_add_edge_missing_source_raises(graph):
    graph.upsert_node("n2", "Bob", "person")
    with pytest.raises(ValueError):
        graph.add_edge("e1", "n1", "n2", "knows")


def test_add_edge_missing_target_raises(graph):
    graph.upsert_node("n1", "Alice", "person")
    with pytest.raises(ValueError):
        graph.add_edge("e1", "n1", "n2", "knows")


def test_get_edges_by_node(graph):
    graph.upsert_node("n1", "Alice", "person")
    graph.upsert_node("n2", "Bob", "person")
    graph.upsert_node("n3", "Carol", "person")
    graph.add_edge("e1", "n1", "n2", "knows")
    graph.add_edge("e2", "n3", "n1", "mentions")
    edges = graph.get_edges_by_node("n1")
    assert len(edges) == 2
    relations = {e["relation"] for e in edges}
    assert relations == {"knows", "mentions"}


def test_delete_edge(graph):
    graph.upsert_node("n1", "Alice", "person")
    graph.upsert_node("n2", "Bob", "person")
    graph.add_edge("e1", "n1", "n2", "knows")
    assert graph.delete_edge("e1") is True
    assert graph.delete_edge("e1") is False
    assert graph.get_edges_by_node("n1") == []


# ------------------------------------------------------------------ #
# 节点删除连带删除边
# ------------------------------------------------------------------ #
def test_delete_node_cascades_edges(graph):
    graph.upsert_node("n1", "Alice", "person")
    graph.upsert_node("n2", "Bob", "person")
    graph.add_edge("e1", "n1", "n2", "knows")
    assert graph.delete_node("n1") is True
    # 节点已删除
    assert graph.get_node("n1") is None
    # 关联边被连带删除
    assert graph.get_edges_by_node("n2") == []
    assert graph.get_edge("e1") is None


def test_delete_missing_node_returns_false(graph):
    assert graph.delete_node("ghost") is False


# ------------------------------------------------------------------ #
# 语义检索
# ------------------------------------------------------------------ #
def test_search_nodes_returns_most_relevant(graph):
    # 注入与查询强相关的内容，构造可区分的向量
    graph.upsert_node("cat", "猫科动物", "animal", {"habitat": "森林"})
    graph.upsert_node("dog", "犬科动物", "animal", {"habitat": "草原"})
    graph.upsert_node("rocket", "火箭", "vehicle", {"propulsion": "燃料"})

    hits = graph.search_nodes("猫 森林 动物", top_k=3)
    assert len(hits) == 3
    # 语义最接近的节点应排在最前
    assert hits[0]["node"]["id"] == "cat"
    # 每个结果都附带节点与其关系
    assert "node" in hits[0] and "edges" in hits[0] and "score" in hits[0]


def test_search_nodes_returns_related_edges(graph):
    graph.upsert_node("alice", "Alice", "person", {"role": "工程师"})
    graph.upsert_node("project", "知识图谱项目", "project", {})
    graph.add_edge("e1", "alice", "project", "participates")

    hits = graph.search_nodes("Alice 项目", top_k=2)
    assert len(hits) == 2
    alice_hit = next(h for h in hits if h["node"]["id"] == "alice")
    # 关联边随节点一并返回
    assert len(alice_hit["edges"]) == 1
    assert alice_hit["edges"][0]["relation"] == "participates"


def test_search_nodes_respects_top_k(graph):
    for i in range(6):
        graph.upsert_node(f"node{i}", f"item {i} tag", "entity")
    hits = graph.search_nodes("item tag", top_k=3)
    assert len(hits) == 3


def testsearch_nodes_without_embed_raises():
    """未注入 embed_callable 的纯图存储调用语义检索应报错。"""
    store = GraphStore(db_path=":memory:", embed_callable=None)
    try:
        with pytest.raises(ValueError):
            store.search_nodes("anything", top_k=3)
    finally:
        store.close()


def test_vector_written_to_injected_vector_store():
    """验证节点向量确实写入外部注入的 VectorStore。"""
    vs = InMemoryVectorStore()
    store = GraphStore(db_path=":memory:", embed_callable=fixed_embed, vector_store=vs)
    try:
        store.upsert_node("n1", "Alice", "person", {"age": 30})
        assert vs._vectors.get("n1") is not None
    finally:
        store.close()


# ------------------------------------------------------------------ #
# G-7：读侧 properties JSON 解析防线                                   #
# ------------------------------------------------------------------ #

def _inject_dirty_row(graph, table, row_id, dirty_props):
    """绕过 upsert 序列化，直接向表写入非法 JSON 的 properties 脏数据。"""
    if table == "nodes":
        graph._conn.execute(
            "INSERT OR REPLACE INTO nodes (id, name, type, properties, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (row_id, "Dirty", "person", dirty_props, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
    else:
        # 边需要合法的端点节点
        graph._conn.execute(
            "INSERT OR REPLACE INTO nodes (id, name, type, properties, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("src", "Src", "person", None, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        graph._conn.execute(
            "INSERT OR REPLACE INTO edges (id, source, target, relation, properties, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (row_id, "src", "src", "likes", dirty_props, "2026-01-01T00:00:00"),
        )
    graph._conn.commit()


def test_corrupt_node_properties_tolerated_as_empty_dict(graph):
    """G-7：nodes.properties 为非法 JSON 时读侧按空 dict 容忍，get/list 不再崩溃。"""
    _inject_dirty_row(graph, "nodes", "dirty", "{invalid json")

    node = graph.get_node("dirty")
    assert node is not None
    assert node["properties"] == {}

    # 列表链路同样容忍
    assert any(n["id"] == "dirty" and n["properties"] == {} for n in graph.list_nodes())


def test_search_chain_survives_dirty_rows(graph):
    """G-7：存在脏 properties 行时语义检索链整体不中断（正常节点照常召回）。"""
    graph.upsert_node("clean1", "clean item alpha", "entity")
    _inject_dirty_row(graph, "nodes", "dirty", "{invalid json")

    # 脏行未写入向量库（绕过 upsert），检索不命中属预期；关键是不抛异常，
    # 且正常节点的召回与边读取不受影响
    hits = graph.search_nodes("clean item alpha", top_k=5)
    assert any(h["node"]["id"] == "clean1" for h in hits)
    for h in hits:
        assert isinstance(h["edges"], list)


def test_corrupt_edge_properties_tolerated_as_empty_dict(graph):
    """G-7：edges.properties 为非法 JSON 时 get_edges_by_node 按空 dict 容忍。"""
    _inject_dirty_row(graph, "edges", "dirty_edge", "not-json[")

    edges = graph.get_edges_by_node("src")
    dirty = [e for e in edges if e["id"] == "dirty_edge"]
    assert len(dirty) == 1
    assert dirty[0]["properties"] == {}