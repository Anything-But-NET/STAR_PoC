from dataclasses import dataclass, field
import re
from typing import Self

import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

from spliter import SegmentNode, parse_markdown_to_tree

MODEL_NAME = "BAAI/bge-m3"
SENTENCE_SPLIT_PATTERN = re.compile(
    r"([^\n]*\n(?:```[\s\S]*?```|~~~[\s\S]*?~~~))|(?<=[.!?])\s+|\n+"
)
SUBTOPIC_PREFIX = "**"
SUBTOPIC_MIN_LEN = 25

device = "mps" if torch.backends.mps.is_available() else "cpu"
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
model = AutoModel.from_pretrained(MODEL_NAME).to(device)


@dataclass
class VectorizedSegmentNode:
    name: str
    level: int
    embedding: np.ndarray | None
    content: list[tuple[str, np.ndarray]]
    childrens: list[Self] = field(default_factory=list)


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def _split_oversized_sentences(
    sentences: list[str],
    max_chunk_size: int,
    chunk_overlap: int,
) -> list[tuple[str, int]]:
    """
    Гарантує, що жоден атомарний елемент не перевищує `max_chunk_size` токенів:
    1. Якщо елемент (наприклад, кодовий блок) більший за `max_chunk_size` і містить переноси рядків,
       спочатку пробуємо розбити його по рядках `\n`.
    2. Якщо навіть окремий рядок перевищує `max_chunk_size`, нарізаємо його токени вікном
       розміром `max_chunk_size` (з кроком `max_chunk_size - chunk_overlap`).
    """
    token_ids_list = tokenizer(sentences, add_special_tokens=False)["input_ids"]
    step = max(1, max_chunk_size - max(0, min(chunk_overlap, max_chunk_size - 1)))

    result: list[tuple[str, int]] = []
    for sentence, token_ids in zip(sentences, token_ids_list):
        s_len = len(token_ids)
        if s_len <= max_chunk_size:
            result.append((sentence, s_len))
            continue

        lines = [line.strip() for line in sentence.splitlines() if line.strip()]
        if len(lines) > 1:
            result.extend(_split_oversized_sentences(lines, max_chunk_size, chunk_overlap))
            continue

        for start_idx in range(0, s_len, step):
            slice_ids = token_ids[start_idx : start_idx + max_chunk_size]
            slice_text = tokenizer.decode(slice_ids, skip_special_tokens=True).strip()
            if slice_text:
                result.append((slice_text, len(slice_ids)))
            if start_idx + max_chunk_size >= s_len:
                break

    return result


def micro_split_chunks(
    text: str,
    max_chunk_size: int = 100,
    chunk_overlap: int = 0,
) -> list[tuple[str, int, int]]:
    res: list[tuple[str, int, int]] = []

    raw_sentences = [
        s.strip()
        for s in SENTENCE_SPLIT_PATTERN.split(text)
        if s and s.strip()
    ]
    if not raw_sentences:
        return res

    sentences_with_lens = _split_oversized_sentences(
        raw_sentences,
        max_chunk_size=max_chunk_size,
        chunk_overlap=chunk_overlap,
    )

    current_len = 0
    current_sentences: list[tuple[str, int]] = []
    start_token_idx = 1

    for sentence, s_len in sentences_with_lens:
        is_subtopic = sentence.startswith(SUBTOPIC_PREFIX) and current_len >= SUBTOPIC_MIN_LEN
        if not is_subtopic and current_len + s_len <= max_chunk_size:
            current_sentences.append((sentence, s_len))
            current_len += s_len
        else:
            if current_sentences:
                chunk_text = "\n".join(s for s, _ in current_sentences)
                res.append((chunk_text, start_token_idx, start_token_idx + current_len))

            while (
                is_subtopic or current_len > chunk_overlap or current_len + s_len > max_chunk_size
            ) and current_sentences:
                _, popped_len = current_sentences.pop(0)
                current_len -= popped_len
                start_token_idx += popped_len
                if is_subtopic and current_len == 0:
                    break

            current_len += s_len
            current_sentences.append((sentence, s_len))

    if current_sentences:
        chunk_text = "\n".join(s for s, _ in current_sentences)
        res.append((chunk_text, start_token_idx, start_token_idx + current_len))

    return res


def get_embedding(
    data: str,
    macro_chunk_size: int = 7500,
    macro_chunk_overlap: int = 500,
) -> list[tuple[str, np.ndarray]]:
    if not data.strip():
        return []

    macro_chunks = micro_split_chunks(
        data,
        max_chunk_size=macro_chunk_size,
        chunk_overlap=macro_chunk_overlap,
    )

    res: list[tuple[str, np.ndarray]] = []
    prev_macro_end = 1

    for macro_idx, (macro_text, macro_start, macro_end) in enumerate(macro_chunks):
        if not macro_text.strip():
            continue

        inputs = tokenizer(macro_text, return_tensors="pt")
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            embeddings = model(**inputs).last_hidden_state  # (1, seq_len, 1024)

        seq_len = embeddings.shape[1]

        # Беремо мікро-чанки тільки після перетину з попереднім макро-блоком
        overlap_offset = (prev_macro_end - macro_start) if macro_idx > 0 else 0
        min_micro_start = 1 + max(0, overlap_offset)

        micro_chunks = [
            (m_text, m_start, m_end)
            for m_text, m_start, m_end in micro_split_chunks(macro_text)
            if m_text.strip() and m_start >= min_micro_start
        ]
        if not micro_chunks:
            prev_macro_end = macro_end
            continue

        batch_inputs = tokenizer(
            [m_text for m_text, _, _ in micro_chunks],
            padding=True,
            truncation=True,
            return_tensors="pt",
        ).to(device)
        with torch.no_grad():
            batch_cls = model(**batch_inputs).last_hidden_state[:, 0].cpu().numpy()

        for (micro_text, micro_start, micro_end), chunk_cls in zip(micro_chunks, batch_cls):
            s_idx = min(max(micro_start, 1), seq_len - 1)
            e_idx = min(max(micro_end, s_idx + 1), seq_len)

            late_span_vec = _l2_normalize(embeddings[0, s_idx:e_idx].mean(dim=0).cpu().numpy())
            vec = _l2_normalize(_l2_normalize(chunk_cls) + late_span_vec)
            res.append((micro_text, vec))

        prev_macro_end = macro_end

    return res


def merge_vectors(vectors: list[np.ndarray], temperature: float = 0.1) -> np.ndarray:
    matrix = np.array(vectors)

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    normalized = matrix / np.where(norms == 0, 1, norms)

    attention_matrix = normalized @ normalized.T
    scores = np.mean(attention_matrix, axis=1).flatten()

    exp_scores = np.exp((scores - np.max(scores)) / temperature)
    weights = exp_scores / np.sum(exp_scores)

    parent_vector = weights @ normalized
    return _l2_normalize(parent_vector)


def vectorize_node(node: SegmentNode) -> VectorizedSegmentNode:
    raw_content = node.content.strip()
    if raw_content and node.level > 0:
        raw_content = f"{'#' * node.level} {node.name}\n{raw_content}"
    content = get_embedding(raw_content)

    content_vectors = [embedding for _, embedding in content]
    all_vectors: list[np.ndarray] = []

    if node.level > 0 and node.name.strip():
        name_embeddings = get_embedding(node.name)
        if name_embeddings:
            all_vectors.append(name_embeddings[0][1])

    if content_vectors:
        all_vectors.append(merge_vectors(content_vectors))

    children = [vectorize_node(child) for child in node.childrens]
    all_vectors.extend(child.embedding for child in children if child.embedding is not None)

    result = merge_vectors(all_vectors) if all_vectors else None
    return VectorizedSegmentNode(node.name, node.level, result, content, childrens=children)


def print_tree(node: VectorizedSegmentNode, indent: int = 0) -> None:
    prefix = "  " * indent
    emb_info = f"shape={node.embedding.shape}" if node.embedding is not None else "None"
    print(f"{prefix}- [{node.level}] {node.name} (Emb: {emb_info}, Children: {len(node.childrens)})")

    for text, vec in node.content:
        short_text = (text[:40] + "...") if len(text) > 40 else text
        print(f"{prefix}    * chunk: '{short_text}' (emb: {vec.shape})")

    for child in node.childrens:
        print_tree(child, indent + 1)


if __name__ == "__main__":
    with open("README.md", "r") as file:
        md_doc = file.read()
        tree = parse_markdown_to_tree(md_doc)
        print_tree(vectorize_node(tree))