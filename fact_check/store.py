from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET
import zipfile

import numpy as np

from spliter import parse_markdown_to_tree
from transformer import VectorizedSegmentNode, vectorize_node

SHARP_CHILD_SCORE_DIFF = 0.05


@dataclass
class MemChunk:
    id: uuid.UUID
    text: str
    file_name: str


@dataclass
class MemVectorNode:
    id: uuid.UUID
    parent_id: uuid.UUID | None
    children_count: int
    embedding: np.ndarray
    file_name: str
    chunks: list[MemChunk] = field(default_factory=list)


def load_document_text(path: str | Path) -> str:
    """Читає .md або .docx файл у Markdown-формат із заголовками та таблицями."""
    p = Path(path)
    if p.suffix.lower() == ".md":
        return p.read_text(encoding="utf-8")

    if p.suffix.lower() == ".docx":
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        lines: list[str] = []
        with zipfile.ZipFile(p) as z:
            root = ET.fromstring(z.read("word/document.xml"))
            body = root.find("w:body", ns)
            if body is None:
                return ""
            for elem in body:
                tag = elem.tag.split("}")[-1]
                if tag == "p":
                    p_style = elem.find(".//w:pStyle", ns)
                    style_val = (
                        p_style.attrib.get(f"{{{ns['w']}}}val", "")
                        if p_style is not None
                        else ""
                    )
                    txt = "".join(t.text or "" for t in elem.findall(".//w:t", ns)).strip()
                    if not txt:
                        continue
                    if "Heading1" in style_val or style_val == "1":
                        lines.append(f"# {txt}")
                    elif "Heading2" in style_val or style_val == "2":
                        lines.append(f"## {txt}")
                    elif "Heading3" in style_val or style_val == "3":
                        lines.append(f"### {txt}")
                    else:
                        lines.append(txt)
                elif tag == "tbl":
                    for row in elem.findall(".//w:tr", ns):
                        cells = [
                            "".join(t.text or "" for t in cell.findall(".//w:t", ns)).strip()
                            for cell in row.findall(".//w:tc", ns)
                        ]
                        if any(cells):
                            lines.append(" | ".join(cells))
        return "\n".join(lines)

    raise ValueError(f"Unsupported file format: {p}")


class InMemoryStarStore:
    """
    In-memory сховище дерева STAR (1-в-1 логіка з db.py: листя + батьківські вузли + auto_merge_points).
    Дозволяє ганяти фоновий факт-чекінг між файлами на інжесті без піднятого Docker/Postgres.
    """

    def __init__(self) -> None:
        self.nodes: dict[uuid.UUID, MemVectorNode] = {}
        self.chunks: dict[uuid.UUID, MemChunk] = {}

    def _save_leaf_chunks(
        self,
        valid_content: list[tuple[str, np.ndarray]],
        file_name: str,
        parent_id: uuid.UUID | None,
    ) -> list[uuid.UUID]:
        chunk_ids: list[uuid.UUID] = []
        for chunk_text, chunk_vec in valid_content:
            cid = uuid.uuid4()
            chunk = MemChunk(id=cid, text=chunk_text, file_name=file_name)
            leaf_node = MemVectorNode(
                id=cid,
                parent_id=parent_id,
                children_count=0,
                embedding=chunk_vec,
                file_name=file_name,
                chunks=[chunk],
            )
            self.chunks[cid] = chunk
            self.nodes[cid] = leaf_node
            chunk_ids.append(cid)
        return chunk_ids

    def _save_node_recursive(
        self,
        node: VectorizedSegmentNode,
        file_name: str,
        parent_id: uuid.UUID | None = None,
    ) -> list[uuid.UUID]:
        valid_content = [(t, v) for t, v in node.content if t.strip()]
        valid_children = [c for c in node.childrens if c.embedding is not None]

        if getattr(node, "level", None) == 0:
            all_cids = self._save_leaf_chunks(valid_content, file_name, None)
            for child in valid_children:
                all_cids.extend(self._save_node_recursive(child, file_name, None))
            return all_cids

        if node.embedding is None:
            return []

        node_id = uuid.uuid4()
        direct_children_count = len(valid_content) + len(valid_children)
        all_cids: list[uuid.UUID] = []

        parent_node = MemVectorNode(
            id=node_id,
            parent_id=parent_id,
            children_count=direct_children_count,
            embedding=node.embedding,
            file_name=file_name,
        )
        self.nodes[node_id] = parent_node

        if (
            not valid_content
            and getattr(node, "level", 0) > 0
            and getattr(node, "name", "").strip()
        ):
            hdr_id = uuid.uuid4()
            hdr_chunk = MemChunk(
                id=hdr_id,
                text=f"{'#' * node.level} {node.name.strip()}",
                file_name=file_name,
            )
            self.chunks[hdr_id] = hdr_chunk
            all_cids.append(hdr_id)

        all_cids.extend(self._save_leaf_chunks(valid_content, file_name, node_id))

        for child in valid_children:
            all_cids.extend(self._save_node_recursive(child, file_name, node_id))

        if all_cids:
            parent_node.chunks = [self.chunks[cid] for cid in all_cids]
        else:
            del self.nodes[node_id]

        return all_cids

    def index_document(self, file_name: str, md_text: str) -> list[MemVectorNode]:
        """Індексує документ у дерево STAR і повертає всі його листові вузли (Late Chunked)."""
        tree = parse_markdown_to_tree(md_text)
        vec_tree = vectorize_node(tree)
        self._save_node_recursive(vec_tree, file_name=file_name)
        return self.get_document_leaves(file_name)

    def get_document_leaves(self, file_name: str) -> list[MemVectorNode]:
        return [
            node
            for node in self.nodes.values()
            if node.file_name == file_name and node.children_count == 0
        ]

    def search_points(
        self,
        query_vec: np.ndarray,
        exclude_file_name: str | None = None,
        only_file_name: str | None = None,
        limit: int = 10,
        only_leaves: bool = True,
    ) -> list[tuple[MemVectorNode, float]]:
        scored: list[tuple[MemVectorNode, float]] = []
        for node in self.nodes.values():
            if exclude_file_name and node.file_name == exclude_file_name:
                continue
            if only_file_name and node.file_name != only_file_name:
                continue
            if only_leaves and node.children_count != 0:
                continue
            score = float(np.dot(query_vec, node.embedding))
            scored.append((node, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def auto_merge_points(
        self,
        points: list[tuple[MemVectorNode, float]],
        query_vector: np.ndarray,
        threshold: float = 0.5,
    ) -> list[tuple[MemVectorNode, float, str]]:
        """Dual-Trigger Auto-Merge (quorum OR parent_is_better) ідентичний rag_poc/db.py."""
        current_points: dict[uuid.UUID, MemVectorNode] = {}
        active_children: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        queue: deque[uuid.UUID] = deque()
        in_queue: set[uuid.UUID] = set()
        scores: dict[uuid.UUID, float] = {}
        leaf_scores: dict[uuid.UUID, float] = {}
        merge_reason: dict[uuid.UUID, str] = {}

        for point, initial_score in points:
            pid = point.id
            current_points[pid] = point
            scores[pid] = float(initial_score)
            leaf_scores[pid] = float(initial_score)
            merge_reason[pid] = "leaf"

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

            parent_point = self.nodes.get(parent_id)
            if parent_point is None or not parent_point.children_count:
                continue

            total_children = parent_point.children_count
            best_child_score = max(scores.get(cid, 0.0) for cid in children_ids)
            best_raw_leaf_score = max(leaf_scores.get(cid, 0.0) for cid in children_ids)
            parent_score = float(np.dot(query_vector, parent_point.embedding))

            has_quorum = (len(children_ids) / total_children) > threshold
            is_competitive_with_top = (global_best_score - parent_score) <= SHARP_CHILD_SCORE_DIFF
            parent_is_better = (parent_score > best_child_score) and is_competitive_with_top
            child_is_much_sharper = (best_child_score - parent_score) > SHARP_CHILD_SCORE_DIFF

            if (has_quorum and not child_is_much_sharper) or parent_is_better:
                reason = (
                    f"parent_is_better ({parent_score:.3f} > {best_child_score:.3f})"
                    if parent_is_better
                    else f"quorum ({len(children_ids)}/{total_children})"
                )
                for cid in children_ids:
                    current_points.pop(cid, None)
                    scores.pop(cid, None)
                    leaf_scores.pop(cid, None)

                del active_children[parent_id]

                current_points[parent_id] = parent_point
                scores[parent_id] = max(best_child_score, parent_score)
                leaf_scores[parent_id] = best_raw_leaf_score
                merge_reason[parent_id] = reason

                grandpa_id = parent_point.parent_id
                if grandpa_id is not None:
                    active_children[grandpa_id].add(parent_id)
                    if grandpa_id not in in_queue:
                        queue.append(grandpa_id)
                        in_queue.add(grandpa_id)

        sorted_by_score = sorted(
            current_points.values(),
            key=lambda p: scores.get(p.id, 0.0),
            reverse=True,
        )

        final_results: list[tuple[MemVectorNode, float, str]] = []
        covered_chunks: set[uuid.UUID] = set()
        for p in sorted_by_score:
            p_cids = {c.id for c in p.chunks}
            if p_cids and not p_cids.isdisjoint(covered_chunks):
                continue
            final_results.append((p, scores[p.id], merge_reason[p.id]))
            covered_chunks.update(p_cids)

        return final_results

    def build_hierarchical_context(
        self,
        target_node: MemVectorNode,
        query_vec: np.ndarray,
    ) -> tuple[str, MemChunk]:
        """
        Детерміновано будує контекст для NLI по дереву STAR (без сліпого вибору випадкових чанків):
          1. Знаходить найкращий листовий чанк L усередині target_node.
          2. Бере в порядку документа (без дублів):
             - Початок змердженого вузла (target_node.chunks[0] — заголовок/вступ верхньої секції)
             - Початок безпосередньої підсекції листа (parent(L).chunks[0])
             - Попередній чанк перед L (позиція idx - 1, щоб не загубити локальне "Він / It")
             - Сам цільовий лист L
        Повертає (зібраний_текст_контексту, цільовий_лист_L).
        """
        # 1. Визначаємо цільовий лист L усередині target_node
        if target_node.children_count == 0:
            best_leaf_node = target_node
        else:
            leaf_candidates: list[tuple[float, MemVectorNode]] = []
            for c in target_node.chunks:
                ln = self.nodes.get(c.id)
                if ln is not None and ln.children_count == 0:
                    leaf_candidates.append((float(np.dot(query_vec, ln.embedding)), ln))
            if leaf_candidates:
                leaf_candidates.sort(key=lambda x: x[0], reverse=True)
                best_leaf_node = leaf_candidates[0][1]
            else:
                best_leaf_node = target_node

        best_leaf_chunk = best_leaf_node.chunks[0]

        # 2. Якщо сама секція компактна (<= 3 чанків), віддаємо її цілком
        section_node = (
            self.nodes.get(best_leaf_node.parent_id)
            if best_leaf_node.parent_id is not None
            else target_node
        ) or target_node

        if len(target_node.chunks) <= 3 and target_node.children_count > 0:
            return "\n".join(c.text for c in target_node.chunks), best_leaf_chunk

        # 3. Збираємо ієрархічний шлях контексту в порядку документа (без дублів)
        selected_chunks: list[MemChunk] = []
        seen_ids: set[uuid.UUID] = set()

        def add_chunk(c: MemChunk | None) -> None:
            if c is not None and c.id not in seen_ids:
                seen_ids.add(c.id)
                selected_chunks.append(c)

        # (a) Початок змердженого STAR-вузла (наприклад, H1/H2 заголовок)
        if target_node.chunks:
            add_chunk(target_node.chunks[0])

        # (b) Початок безпосередньої підсекції (H3 заголовок + перше речення)
        if section_node.chunks:
            add_chunk(section_node.chunks[0])

        # (c) Сусідній чанк прямо перед L у цій підсекції (idx - 1)
        sec_chunk_ids = [c.id for c in section_node.chunks]
        if best_leaf_chunk.id in sec_chunk_ids:
            idx = sec_chunk_ids.index(best_leaf_chunk.id)
            if idx > 0:
                add_chunk(section_node.chunks[idx - 1])

        # (d) Сам цільовий листовий чанк L
        add_chunk(best_leaf_chunk)

        return "\n".join(c.text for c in selected_chunks), best_leaf_chunk
