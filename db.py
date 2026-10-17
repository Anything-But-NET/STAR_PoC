import sqlite3
import uuid
from typing import Iterable

import numpy as np
from qdrant_client import QdrantClient, models

COLLECTION_NAME = "demo_collection"
QDRANT_PATH = "./qdrant_db"
SQLITE_PATH = "./chunks.db"

# Ініціалізація Qdrant у файловому режимі
client = QdrantClient(path=QDRANT_PATH)

if not client.collection_exists(COLLECTION_NAME):
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=models.VectorParams(
            size=1024,
            distance=models.Distance.COSINE,
        ),
    )

# Ініціалізація SQLite для збереження текстових чанків
sqlite_conn = sqlite3.connect(SQLITE_PATH)
sqlite_cursor = sqlite_conn.cursor()
sqlite_cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS chunks (
        id TEXT PRIMARY KEY,
        text TEXT NOT NULL
    )
    """
)
sqlite_conn.commit()


def add_chunk(chunk_id: uuid.UUID | str, text: str) -> str:
    """Зберігає текстовий чанк у SQLite за переданим UUID."""
    cid = str(chunk_id)
    sqlite_cursor.execute(
        "INSERT OR REPLACE INTO chunks (id, text) VALUES (?, ?)",
        (cid, text),
    )
    sqlite_conn.commit()
    return cid


def get_chunks(chunk_ids: Iterable[uuid.UUID | str]) -> list[str]:
    """Дістає тексти чанків із SQLite за списком їхніх ID (зі збереженням порядку)."""
    ids = [str(cid) for cid in chunk_ids]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sqlite_cursor.execute(
        f"SELECT id, text FROM chunks WHERE id IN ({placeholders})",
        ids,
    )
    rows = dict(sqlite_cursor.fetchall())
    return [rows[cid] for cid in ids if cid in rows]


def add_point(
    vector: np.ndarray | list[float],
    chunk_ids: Iterable[uuid.UUID | str],
    point_id: uuid.UUID | str | None = None,
    parent_id: uuid.UUID | str | None = None,
    children_count: int = 0,
    parent_children_count: int | None = None,
) -> str:
    """
    Зберігає вектор у Qdrant, а в payload записує:
    - chunks: список ID чанків у SQLite, з яких складається цей поінт
    - parent_id: ID батьківського поінта в Qdrant (або None для кореня)
    - children_count: кількість прямих дочірніх елементів цього поінта
    - parent_children_count: загальна кількість дочірніх елементів у батька цього поінта
    """
    pid = str(point_id or uuid.uuid4())
    parent_pid = str(parent_id) if parent_id is not None else None
    vec_list = vector.tolist() if isinstance(vector, np.ndarray) else list(vector)
    chunks_payload = [str(cid) for cid in chunk_ids]

    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            models.PointStruct(
                id=pid,
                vector=vec_list,
                payload={
                    "chunks": chunks_payload,
                    "parent_id": parent_pid,
                    "children_count": children_count,
                    "parent_children_count": parent_children_count,
                },
            )
        ],
    )
    return pid


def get_point(point_id: uuid.UUID | str, with_vectors: bool = True) -> models.Record | None:
    """Отримує поінт із Qdrant за його ID."""
    records = client.retrieve(
        collection_name=COLLECTION_NAME,
        ids=[str(point_id)],
        with_payload=True,
        with_vectors=with_vectors,
    )
    return records[0] if records else None


def get_parent_children_count(parent_id: uuid.UUID | str) -> int:
    """
    Повертає загальну кількість дочірніх елементів у батьківського поінта.
    Спочатку дивиться в payload самого батька (children_count),
    а якщо його немає — рахує поінти з відповідним parent_id у Qdrant.
    """
    parent_record = get_point(parent_id, with_vectors=False)
    if parent_record and parent_record.payload and "children_count" in parent_record.payload:
        return int(parent_record.payload["children_count"])

    count_res = client.count(
        collection_name=COLLECTION_NAME,
        count_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="parent_id",
                    match=models.MatchValue(value=str(parent_id)),
                )
            ]
        ),
        exact=True,
    )
    return count_res.count


def reset_db() -> None:
    """Очищує SQLite та перестворює колекцію в Qdrant."""
    sqlite_cursor.execute("DELETE FROM chunks")
    sqlite_conn.commit()
    if client.collection_exists(COLLECTION_NAME):
        client.delete_collection(COLLECTION_NAME)
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=models.VectorParams(
            size=1024,
            distance=models.Distance.COSINE,
        ),
    )


def save_node(
    node,
    parent_id: uuid.UUID | str | None = None,
    parent_children_count: int | None = None,
) -> list[str]:
    """
    Рекурсивно зберігає дерево VectorizedSegmentNode у SQLite та Qdrant:
    1. Кожен текстовий мікро-чанк вузла зберігається у SQLite (таблиця chunks)
       та як листовий поінт у Qdrant (із parent_id = ID поточного вузла).
    2. Усі дочірні вузли рекурсивно зберігаються з parent_id = ID поточного вузла.
    3. Сам вузол зберігається в Qdrant зі своїм змердженим вектором, списком усіх
       ID чанків піддерева (chunks), parent_id, children_count та parent_children_count.
    Повертає список усіх ID чанків, що входять до цього вузла (включно з дітьми).
    """
    valid_content = [(text, vec) for text, vec in node.content if text.strip()]
    valid_children = [child for child in node.childrens if child.embedding is not None]

    # Штучний корінь документа (level=0) ніколи не зберігаємо як окремий поінт у Qdrant,
    # щоб він не агрегував усі чанки файлу і не протікав у видачу.
    if getattr(node, "level", None) == 0:
        all_chunk_ids: list[str] = []
        for chunk_text, chunk_vec in valid_content:
            chunk_id = str(uuid.uuid4())
            add_chunk(chunk_id, chunk_text)
            add_point(
                vector=chunk_vec,
                chunk_ids=[chunk_id],
                point_id=chunk_id,
                parent_id=None,
                children_count=0,
                parent_children_count=None,
            )
            all_chunk_ids.append(chunk_id)

        for child in valid_children:
            child_chunk_ids = save_node(
                child,
                parent_id=None,
                parent_children_count=None,
            )
            all_chunk_ids.extend(child_chunk_ids)

        return all_chunk_ids

    node_id = str(uuid.uuid4())

    # Скільки прямих дочірніх поінтів має цей вузол (мікро-чанки + дочірні секції)
    direct_children_count = len(valid_content) + len(valid_children)

    all_chunk_ids: list[str] = []

    # Якщо батьківський заголовок не має власного тексту перед підзаголовками,
    # зберігаємо сам рядок заголовка як структурний чанк у SQLite, щоб він не губився при склеюванні
    if not valid_content and getattr(node, "level", 0) > 0 and getattr(node, "name", "").strip():
        header_chunk_id = str(uuid.uuid4())
        add_chunk(header_chunk_id, f"{'#' * node.level} {node.name.strip()}")
        all_chunk_ids.append(header_chunk_id)

    for chunk_text, chunk_vec in valid_content:
        chunk_id = str(uuid.uuid4())
        add_chunk(chunk_id, chunk_text)
        add_point(
            vector=chunk_vec,
            chunk_ids=[chunk_id],
            point_id=chunk_id,
            parent_id=node_id,
            children_count=0,
            parent_children_count=direct_children_count,
        )
        all_chunk_ids.append(chunk_id)

    # 2. Рекурсивно зберігаємо дочірні вузли
    for child in valid_children:
        child_chunk_ids = save_node(
            child,
            parent_id=node_id,
            parent_children_count=direct_children_count,
        )
        all_chunk_ids.extend(child_chunk_ids)

    # 3. Зберігаємо сам вузол (батьківський/проміжний поінт) у Qdrant
    if node.embedding is not None and all_chunk_ids:
        add_point(
            vector=node.embedding,
            chunk_ids=all_chunk_ids,
            point_id=node_id,
            parent_id=parent_id,
            children_count=direct_children_count,
            parent_children_count=parent_children_count,
        )

    return all_chunk_ids


def search_points(
    vector: np.ndarray | list[float],
    limit: int = 20,
    only_leaves: bool = True,
) -> list[models.ScoredPoint]:
    """
    Шукає топ-N найближчих поінтів у Qdrant за вектором запиту.
    """
    vec_list = vector.tolist() if isinstance(vector, np.ndarray) else list(vector)
    query_filter = (
        models.Filter(
            must=[
                models.FieldCondition(
                    key="children_count",
                    match=models.MatchValue(value=0),
                )
            ]
        )
        if only_leaves
        else None
    )
    response = client.query_points(
        collection_name=COLLECTION_NAME,
        query=vec_list,
        query_filter=query_filter,
        limit=limit,
        with_payload=True,
    )
    points = response.points
    if only_leaves:
        points = [p for p in points if (p.payload or {}).get("children_count", 0) == 0]
    return points


def auto_merge_points(
    points: Iterable[models.ScoredPoint | models.Record],
    query_vector: np.ndarray | list[float] | None = None,
    threshold: float = 0.5,
) -> list[tuple[models.ScoredPoint | models.Record, float]]:
    """
    Ітеративно обʼєднує дочірні поінти в батьківський:
    1. Якщо більшість (> threshold) дочірніх елементів батька присутні у вибірці, АБО
    2. Якщо вектор самого батька пасує до запиту краще за знайдені дочірні чанки (широкий запит про всю секцію),
       тоді дочірні поінти замінюються на батьківський.
    Якщо ж один вузький мікро-чанк значно точніший за вектор розмитого батька — він не склеюється.
    """
    from collections import defaultdict

    q_vec = np.asarray(query_vector, dtype=np.float32) if query_vector is not None else None
    if q_vec is not None:
        q_norm = np.linalg.norm(q_vec)
        if q_norm > 0:
            q_vec = q_vec / q_norm

    current_points: dict[str, models.ScoredPoint | models.Record] = {}
    scores: dict[str, float] = {}

    for idx, p in enumerate(points):
        pid = str(p.id)
        current_points[pid] = p
        scores[pid] = float(getattr(p, "score", 1.0 - idx * 0.001))

    while True:
        by_parent: dict[str, list[models.ScoredPoint | models.Record]] = defaultdict(list)
        for p in current_points.values():
            payload = p.payload or {}
            parent_id = payload.get("parent_id")
            if parent_id is not None:
                by_parent[str(parent_id)].append(p)

        merged_in_iteration = False

        for parent_id, children in by_parent.items():
            first_payload = children[0].payload or {}
            total_children = first_payload.get("parent_children_count")
            if total_children is None:
                total_children = get_parent_children_count(parent_id)
            if not total_children:
                continue

            parent_point = current_points.get(parent_id) or get_point(parent_id, with_vectors=True)
            if parent_point is None:
                continue

            # Не згортаємо в самий корінь усього документа (верхній контейнер із parent_id=None)
            parent_payload = parent_point.payload or {}
            if parent_payload.get("parent_id") is None and total_children > 1:
                continue

            parent_chunks = set(parent_payload.get("chunks", []))
            descendant_scores = [
                scores.get(str(p.id), 0.0)
                for p in current_points.values()
                if parent_chunks and set((p.payload or {}).get("chunks", [])).issubset(parent_chunks)
            ]
            best_child_score = max(
                descendant_scores if descendant_scores else [scores.get(str(ch.id), 0.0) for ch in children]
            )
            parent_score = best_child_score

            if q_vec is not None and getattr(parent_point, "vector", None) is not None:
                p_vec = np.asarray(parent_point.vector, dtype=np.float32)
                p_norm = np.linalg.norm(p_vec)
                if p_norm > 0:
                    p_vec = p_vec / p_norm
                parent_score = float(np.dot(q_vec, p_vec))

            has_quorum = (len(children) / total_children) > threshold
            parent_is_better = parent_score > best_child_score
            child_is_much_sharper = (best_child_score - parent_score) > 0.06

            if (has_quorum and not child_is_much_sharper) or parent_is_better:
                for ch in children:
                    cid = str(ch.id)
                    current_points.pop(cid, None)
                    scores.pop(cid, None)

                parent_pid = str(parent_point.id)
                current_points[parent_pid] = parent_point
                scores[parent_pid] = max(best_child_score, parent_score)
                merged_in_iteration = True

        if not merged_in_iteration:
            break

    sorted_ids = sorted(current_points.keys(), key=lambda pid: scores.get(pid, 0.0), reverse=True)
    return [(current_points[pid], scores[pid]) for pid in sorted_ids]


def query_with_auto_merge(
    vector: np.ndarray | list[float],
    limit: int = 20,
    threshold: float = 0.5,
) -> list[dict]:
    """
    Виконує пошук топ `limit` поінтів у Qdrant, ітеративно згортає їх до батьківських
    поінтів та підтягує тексти чанків із SQLite.
    """
    initial_points = search_points(vector, limit=limit, only_leaves=True)
    merged_points = auto_merge_points(initial_points, query_vector=vector, threshold=threshold)

    results: list[dict] = []
    for p, score in merged_points:
        payload = p.payload or {}
        chunk_ids = payload.get("chunks", [])
        chunk_texts = get_chunks(chunk_ids)
        results.append(
            {
                "point_id": str(p.id),
                "parent_id": payload.get("parent_id"),
                "score": score,
                "children_count": payload.get("children_count", 0),
                "chunk_ids": chunk_ids,
                "chunks": chunk_texts,
                "text": "\n".join(chunk_texts),
            }
        )

    return results