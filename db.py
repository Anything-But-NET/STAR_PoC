import os
import uuid
from collections import defaultdict, deque
from collections.abc import Iterable
from typing import TYPE_CHECKING, Optional

import numpy as np
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Column,
    Engine,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Uuid,
    create_engine,
    delete,
    func,
    select,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    selectinload,
    sessionmaker,
)

if TYPE_CHECKING:
    from transformer import VectorizedSegmentNode

VECTOR_SIZE = 1024
SHARP_CHILD_SCORE_DIFF = 0.06
DEFAULT_SCORE_DECAY = 0.001
DEFAULT_DB_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"


def _to_uuid(val: uuid.UUID | str) -> uuid.UUID:
    return val if isinstance(val, uuid.UUID) else uuid.UUID(str(val))


def _to_vector_list(vector: np.ndarray | list[float]) -> list[float]:
    return vector.tolist() if isinstance(vector, np.ndarray) else [float(x) for x in vector]

class Base(DeclarativeBase):
    pass


vector_chunk_association = Table(
    "vector_chunk_association",
    Base.metadata,
    Column(
        "vector_node_id",
        Uuid,
        ForeignKey("vector_nodes.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "chunk_id",
        Uuid,
        ForeignKey("chunks.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("position", Integer, nullable=False, default=0),
)


class Chunks(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    text: Mapped[str] = mapped_column(String, nullable=False)
    file_name: Mapped[str] = mapped_column(String, nullable=False)


class VectorNode(Base):
    __tablename__ = "vector_nodes"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    chunks: Mapped[list["Chunks"]] = relationship(
        secondary=vector_chunk_association,
        order_by=vector_chunk_association.c.position,
    )

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "vector_nodes.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )

    parent: Mapped[Optional["VectorNode"]] = relationship(
        "VectorNode",
        back_populates="children",
        remote_side=[id],
    )
    children: Mapped[list["VectorNode"]] = relationship(
        "VectorNode",
        back_populates="parent",
    )

    children_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    embedding: Mapped[list[float]] = mapped_column(Vector(VECTOR_SIZE), nullable=False)

    __table_args__ = (
        Index(
            "embedding_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class DbService:
    def __init__(self, db_url: str | Engine = DEFAULT_DB_URL) -> None:
        self.engine: Engine = (
             create_engine(db_url) if isinstance(db_url, str) else db_url
        )
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "postgresql":
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            Base.metadata.create_all(conn)

    def reset_db(self) -> None:
        """Очищує таблиці в базі даних."""
        with self.engine.begin() as conn:
            Base.metadata.drop_all(conn)
            Base.metadata.create_all(conn)

    def count_chunks(self) -> int:
        with self.session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(Chunks)) or 0)

    def count_points(self) -> int:
        with self.session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(VectorNode)) or 0)

    def add_chunk(
        self,
        text_content: str,
        file_name: str,
        chunk_id: uuid.UUID | str | None = None,
    ) -> uuid.UUID:
        """Зберігає текстовий чанк у таблицю chunks."""
        cid = _to_uuid(chunk_id) if chunk_id is not None else uuid.uuid4()
        with self.session_factory() as session:
            chunk = Chunks(id=cid, text=text_content, file_name=file_name)
            session.merge(chunk)
            session.commit()
        return cid

    def get_chunks(self, chunk_ids: Iterable[uuid.UUID | str]) -> list[str]:
        """Дістає тексти чанків за списком їхніх ID (зі збереженням порядку)."""
        ids = [_to_uuid(cid) for cid in chunk_ids]
        if not ids:
            return []
        with self.session_factory() as session:
            rows = session.execute(
                select(Chunks.id, Chunks.text).where(Chunks.id.in_(ids))
            ).all()
            row_map = {row.id: row.text for row in rows}
        return [row_map[cid] for cid in ids if cid in row_map]

    def add_point(
        self,
        vector: np.ndarray | list[float],
        chunk_ids: Iterable[uuid.UUID | str],
        point_id: uuid.UUID | str | None = None,
        parent_id: uuid.UUID | str | None = None,
        children_count: int = 0,
    ) -> uuid.UUID:
        """
        Зберігає вузол VectorNode та його зв'язки з чанками (зі збереженням їхнього порядку).
        """
        pid = _to_uuid(point_id) if point_id is not None else uuid.uuid4()
        parent_pid = _to_uuid(parent_id) if parent_id is not None else None
        vec_list = _to_vector_list(vector)
        ordered_chunk_ids = [_to_uuid(cid) for cid in chunk_ids]

        with self.session_factory() as session:
            node = VectorNode(
                id=pid,
                parent_id=parent_pid,
                children_count=children_count,
                embedding=vec_list,
            )
            session.merge(node)
            session.flush()

            session.execute(
                delete(vector_chunk_association).where(
                    vector_chunk_association.c.vector_node_id == pid
                )
            )
            if ordered_chunk_ids:
                session.execute(
                    vector_chunk_association.insert(),
                    [
                        {
                            "vector_node_id": pid,
                            "chunk_id": cid,
                            "position": pos,
                        }
                        for pos, cid in enumerate(ordered_chunk_ids)
                    ],
                )
            session.commit()

        return pid

    def get_point(self, point_id: uuid.UUID | str) -> VectorNode | None:
        """Отримує VectorNode за його ID разом із впорядкованими чанками."""
        pid = _to_uuid(point_id)
        with self.session_factory() as session:
            stmt = (
                select(VectorNode)
                .options(selectinload(VectorNode.chunks))
                .where(VectorNode.id == pid)
            )
            return session.scalars(stmt).first()

    def get_parent_children_count(self, parent_id: uuid.UUID | str) -> int:
        """
        Повертає загальну кількість дочірніх елементів у батьківського вузла.
        Спочатку дивиться в поле children_count самого батька,
        а якщо його немає — рахує записи з відповідним parent_id.
        """
        pid = _to_uuid(parent_id)
        with self.session_factory() as session:
            parent_children_count = session.scalar(
                select(VectorNode.children_count).where(VectorNode.id == pid)
            )
            if parent_children_count is not None:
                return int(parent_children_count)
            else:
                raise ValueError("Node not found")

    def _save_leaf_chunks(
        self,
        session: Session,
        valid_content: list[tuple[str, np.ndarray]],
        file_name: str,
        parent_id: uuid.UUID | None,
    ) -> list[uuid.UUID]:
        chunk_ids: list[uuid.UUID] = []
        for chunk_text, chunk_vec in valid_content:
            chunk_id = uuid.uuid4()
            chunk = Chunks(id=chunk_id, text=chunk_text, file_name=file_name)
            leaf_node = VectorNode(
                id=chunk_id,
                parent_id=parent_id,
                children_count=0,
                embedding=_to_vector_list(chunk_vec),
            )
            session.add(chunk)
            session.add(leaf_node)
            session.flush()
            session.execute(
                vector_chunk_association.insert().values(
                    vector_node_id=chunk_id,
                    chunk_id=chunk_id,
                    position=0,
                )
            )
            chunk_ids.append(chunk_id)
        return chunk_ids

    def _save_node_recursive(
        self,
        session: Session,
        node: "VectorizedSegmentNode",
        file_name: str,
        parent_id: uuid.UUID | None = None,
    ) -> list[uuid.UUID]:
        valid_content = [(text_str, vec) for text_str, vec in node.content if text_str.strip()]
        valid_children = [child for child in node.childrens if child.embedding is not None]

        # Штучний корінь документа (level=0) ніколи не зберігаємо як окремий VectorNode,
        # щоб він не агрегував усі чанки файлу і не протікав у видачу.
        if getattr(node, "level", None) == 0:
            all_chunk_ids = self._save_leaf_chunks(
                session=session,
                valid_content=valid_content,
                file_name=file_name,
                parent_id=None
            )
            for child in valid_children:
                all_chunk_ids.extend(
                    self._save_node_recursive(
                        session=session,
                        node=child,
                        file_name=file_name,
                        parent_id=None,
                    )
                )
            return all_chunk_ids

        if node.embedding is None:
            return []

        node_id = uuid.uuid4()
        direct_children_count = len(valid_content) + len(valid_children)
        all_chunk_ids: list[uuid.UUID] = []

        parent_node = VectorNode(
            id=node_id,
            parent_id=parent_id,
            children_count=direct_children_count,
            embedding=_to_vector_list(node.embedding),
        )
        session.add(parent_node)
        session.flush()

        # Якщо батьківський заголовок не має власного тексту перед підзаголовками,
        # зберігаємо сам рядок заголовка як структурний чанк, щоб він не губився при склеюванні
        if not valid_content and getattr(node, "level", 0) > 0 and getattr(node, "name", "").strip():
            header_chunk_id = uuid.uuid4()
            header_chunk = Chunks(
                id=header_chunk_id,
                text=f"{'#' * node.level} {node.name.strip()}",
                file_name=file_name,
            )
            session.add(header_chunk)
            session.flush()
            all_chunk_ids.append(header_chunk_id)

        all_chunk_ids.extend(
            self._save_leaf_chunks(
                session=session,
                valid_content=valid_content,
                file_name=file_name,
                parent_id=node_id,
            )
        )

        for child in valid_children:
            child_chunk_ids = self._save_node_recursive(
                session=session,
                node=child,
                file_name=file_name,
                parent_id=node_id,
            )
            all_chunk_ids.extend(child_chunk_ids)

        if all_chunk_ids:
            session.execute(
                vector_chunk_association.insert(),
                [
                    {
                        "vector_node_id": node_id,
                        "chunk_id": cid,
                        "position": pos,
                    }
                    for pos, cid in enumerate(all_chunk_ids)
                ],
            )
        else:
            session.delete(parent_node)

        return all_chunk_ids

    def save_node(
        self,
        node: "VectorizedSegmentNode",
        file_name: str,
        parent_id: uuid.UUID | str | None = None,
    ) -> list[uuid.UUID]:
        """
        Рекурсивно зберігає дерево VectorizedSegmentNode у PostgreSQL (таблиці chunks та vector_nodes).
        Повертає список усіх UUID чанків, що входять до цього вузла (включно з дітьми).
        """
        parent_uuid = _to_uuid(parent_id) if parent_id is not None else None
        with self.session_factory() as session:
            chunk_ids = self._save_node_recursive(
                session=session,
                node=node,
                file_name=file_name,
                parent_id=parent_uuid,
            )
            session.commit()
        return chunk_ids

    def search_points(
        self,
        vector: np.ndarray | list[float],
        limit: int = 20,
        only_leaves: bool = True,
    ) -> list[tuple[VectorNode, float]]:
        """
        Шукає топ-N найближчих вузлів у PostgreSQL за вектором запиту (cosine similarity).
        Повертає список кортежів (VectorNode, score).
        """
        vec_list = _to_vector_list(vector)
        distance_expr = VectorNode.embedding.cosine_distance(vec_list)
        score_expr = (1.0 - distance_expr).label("score")

        stmt = (
            select(VectorNode, score_expr)
            .options(selectinload(VectorNode.chunks))
            .order_by(distance_expr)
            .limit(limit)
        )
        if only_leaves:
            stmt = stmt.where(VectorNode.children_count == 0)

        with self.session_factory() as session:
            rows = session.execute(stmt).all()
            return [(row[0], float(row[1])) for row in rows]

    def auto_merge_points(
        self,
        points: Iterable[tuple[VectorNode, float] | VectorNode],
        query_vector: np.ndarray | list[float],
        threshold: float = 0.5,
    ) -> list[tuple[VectorNode, float]]:
        """
        Ітеративно обʼєднує дочірні вузли в батьківський:
        1. Якщо більшість (> threshold) дочірніх елементів батька присутні у вибірці, АБО
        2. Якщо вектор самого батька пасує до запиту краще за знайдені дочірні чанки,
           тоді дочірні вузли замінюються на батьківський.
        Якщо ж один вузький мікро-чанк значно точніший за вектор розмитого батька — він не склеюється.
        """
        q_vec = _to_vector_list(query_vector)

        current_points: dict[uuid.UUID, VectorNode] = {}
        nodes_cache: dict[uuid.UUID, VectorNode] = {}
        active_children: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        queue: deque[uuid.UUID] = deque()
        in_queue: set[uuid.UUID] = set()
        scores: dict[uuid.UUID, float] = {}
        leaf_scores: dict[uuid.UUID, float] = {}

        for idx, item in enumerate(points):
            if isinstance(item, tuple):
                point, initial_score = item[0], float(item[1])
            else:
                point = item
                initial_score = float(1.0 - idx * DEFAULT_SCORE_DECAY)

            pid = point.id
            current_points[pid] = point
            nodes_cache[pid] = point
            scores[pid] = initial_score
            leaf_scores[pid] = initial_score

            if point.parent_id is not None:
                active_children[point.parent_id].add(pid)
                if point.parent_id not in in_queue:
                    queue.append(point.parent_id)
                    in_queue.add(point.parent_id)

        global_best_score = max(scores.values()) if scores else 0.0

        while queue:
            parent_id = queue.popleft()
            in_queue.discard(parent_id)

            children_ids = active_children.get(parent_id)
            if not children_ids:
                continue

            parent_point = nodes_cache.get(parent_id) or self.get_point(parent_id)
            if parent_point is None:
                continue
            nodes_cache[parent_id] = parent_point

            total_children = parent_point.children_count
            if not total_children:
                continue

            best_child_score = max(scores.get(cid, 0.0) for cid in children_ids)
            best_raw_leaf_score = max(leaf_scores.get(cid, 0.0) for cid in children_ids)

            parent_score = float(np.dot(q_vec, parent_point.embedding))
            
            has_quorum = (len(children_ids) / total_children) > threshold
            is_competitive_with_top = (global_best_score - parent_score) <= SHARP_CHILD_SCORE_DIFF
            parent_is_better = (parent_score > best_child_score) and is_competitive_with_top
            
            child_is_much_sharper = (best_child_score - parent_score) > SHARP_CHILD_SCORE_DIFF

            if (has_quorum and not child_is_much_sharper) or parent_is_better:
                for cid in children_ids:
                    current_points.pop(cid, None)
                    scores.pop(cid, None)
                    leaf_scores.pop(cid, None)

                del active_children[parent_id]

                current_points[parent_id] = parent_point
                scores[parent_id] = max(best_child_score, parent_score)
                leaf_scores[parent_id] = best_raw_leaf_score

                grandpa_id = parent_point.parent_id
                if grandpa_id is not None:
                    active_children[grandpa_id].add(parent_id)
                    if grandpa_id not in in_queue:
                        queue.append(grandpa_id)
                        in_queue.add(grandpa_id)

        sorted_by_score = sorted(
                    current_points.values(),
                    key=lambda p: scores.get(p.id, 0.0),
                    reverse=True
                )
        
        final_ids = []
        covered_chunks: set[uuid.UUID] = set()

        for p in sorted_by_score:
            p_chunk_ids = {c.id for c in p.chunks}
            
            if p_chunk_ids and not p_chunk_ids.isdisjoint(covered_chunks):
                continue
                
            final_ids.append(p.id)
            covered_chunks.update(p_chunk_ids)

        return [(current_points[pid], scores[pid]) for pid in final_ids]

    def query_with_auto_merge(
        self,
        vector: np.ndarray | list[float],
        limit: int = 20,
        threshold: float = 0.5,
    ) -> list[dict]:
        """
        Виконує пошук топ `limit` вузлів у PostgreSQL, ітеративно згортає їх до батьківських
        вузлів та формує результати разом із текстами чанків.
        """
        initial_points = self.search_points(vector, limit=limit, only_leaves=True)
        merged_points = self.auto_merge_points(
            initial_points,
            query_vector=vector,
            threshold=threshold,
        )

        results: list[dict] = []
        for point, score in merged_points:
            chunk_ids = [str(c.id) for c in point.chunks]
            chunk_texts = [c.text for c in point.chunks]
            file_names = list(dict.fromkeys(c.file_name for c in point.chunks))
            results.append(
                {
                    "point_id": str(point.id),
                    "parent_id": str(point.parent_id) if point.parent_id else None,
                    "score": score,
                    "children_count": point.children_count,
                    "chunk_ids": chunk_ids,
                    "chunks": chunk_texts,
                    "file_name": file_names[0] if file_names else None,
                    "text": "\n".join(chunk_texts),
                }
            )

        return results
