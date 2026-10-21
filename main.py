import sys
import uuid

import numpy as np

from db import DbService
from spliter import parse_markdown_to_tree
from transformer import get_embedding, merge_vectors, print_tree, vectorize_node

DEFAULT_MD_PATH = "TEST.md"
DEFAULT_QUERY = "Run with docker"
DEFAULT_LIMIT = 20
DEFAULT_THRESHOLD = 0.5

db = DbService()

def run_pipeline(md_path: str = DEFAULT_MD_PATH) -> list[uuid.UUID]:
    db.reset_db()
    with open(md_path, "r", encoding="utf-8") as file:
        md_doc = file.read()

    # 1. Парсимо Markdown у дерево сегментів
    tree = parse_markdown_to_tree(md_doc)

    # 2. Створюємо ембедінги для мікро-чанків та вузлів дерева
    vectorized_tree = vectorize_node(tree)
    print_tree(vectorized_tree)

    # 3. Зберігаємо чанки та векторні вузли у PostgreSQL
    return db.save_node(vectorized_tree, file_name=md_path)


def embed_query(query_text: str) -> np.ndarray:
    """Створює ембедінг для текстового запиту."""
    chunks_with_vecs = get_embedding(query_text)
    vectors = [vec for _, vec in chunks_with_vecs]
    if not vectors:
        raise ValueError("Query text cannot be empty or produce no embeddings.")
    return merge_vectors(vectors) if len(vectors) > 1 else vectors[0]


def search_query(
    query_text: str,
    limit: int = DEFAULT_LIMIT,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[dict]:
    """
    Приймає текстовий запит -> векторизує його -> дістає топ `limit` вузлів із PostgreSQL ->
    ітеративно замінює дочірні елементи на батька, якщо їх більшість (> threshold) ->
    повертає фінальний список результатів із текстами чанків.
    """
    if not query_text.strip():
        return []
    query_vec = embed_query(query_text)
    return db.query_with_auto_merge(query_vec, limit=limit, threshold=threshold)


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--reindex"]
    force_reindex = "--reindex" in sys.argv[1:]

    points_count = db.count_points()
    if points_count == 0 or force_reindex:
        print(f"📥 Індексуємо {DEFAULT_MD_PATH} у PostgreSQL...")
        run_pipeline(DEFAULT_MD_PATH)
        points_count = db.count_points()

    chunks_count = db.count_chunks()

    print("\n" + "═" * 70)
    print(f"✅ Чанків у PostgreSQL : {chunks_count}")
    print(f"✅ Поінтів у PostgreSQL: {points_count}")
    print("═" * 70)

    query = " ".join(args) if args else DEFAULT_QUERY
    print(f'\n🔎 Запит: "{query}"')
    results = search_query(query, limit=DEFAULT_LIMIT, threshold=DEFAULT_THRESHOLD)

    print(f"📦 Результатів після авто-обʼєднання (з {DEFAULT_LIMIT} поінтів): {len(results)}\n")
    for idx, item in enumerate(results, 1):
        preview = item["text"].replace("\n", " ").strip()
        print(
            f"{idx:2d}. [Score: {item['score']:.4f} | Point: {item['point_id'][:8]} | "
            f"Parent: {str(item['parent_id'])[:8]} | Чанків: {len(item['chunk_ids'])} | "
            f"Символів: {len(item['text'])}]\n"
            f"    💬 {preview}\n"
        )


if __name__ == "__main__":
    main()
