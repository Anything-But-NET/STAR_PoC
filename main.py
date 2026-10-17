import sys
import numpy as np

from db import (
    COLLECTION_NAME,
    client,
    query_with_auto_merge,
    reset_db,
    save_node,
    sqlite_cursor,
)
from spliter import parse_markdown_to_tree
from transformer import get_embedding, merge_vectors, print_tree, vectorize_node


def run_pipeline(md_path: str = "README.md") -> list[str]:
    reset_db()
    with open(md_path, "r", encoding="utf-8") as file:
        md_doc = file.read()

    # 1. Парсимо Markdown у дерево сегментів
    tree = parse_markdown_to_tree(md_doc)

    # 2. Створюємо ембедінги для мікро-чанків та вузлів дерева
    vectorized_tree = vectorize_node(tree)
    print_tree(vectorized_tree)

    # 3. Зберігаємо чанки в SQLite, а вектори й метадані (chunks, parent_id, children_count) у Qdrant
    saved_chunk_ids = save_node(vectorized_tree)

    return saved_chunk_ids


def embed_query(query_text: str) -> np.ndarray:
    """Створює ембедінг для текстового запиту."""
    chunks_with_vecs = get_embedding(query_text)
    vectors = [vec for _, vec in chunks_with_vecs]
    return merge_vectors(vectors) if len(vectors) > 1 else vectors[0]


def search_query(
    query_text: str,
    limit: int = 20,
    threshold: float = 0.5,
) -> list[dict]:
    """
    Приймає текстовий запит -> векторизує його -> дістає топ `limit` поінтів із Qdrant ->
    ітеративно замінює дочірні елементи на батька, якщо їх більшість (> threshold) ->
    повертає фінальний список результатів із текстами чанків.
    """
    query_vec = embed_query(query_text)
    return query_with_auto_merge(query_vec, limit=limit, threshold=threshold)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--reindex"]
    force_reindex = "--reindex" in sys.argv[1:]

    qdrant_count = client.count(collection_name=COLLECTION_NAME, exact=True).count
    if qdrant_count == 0 or force_reindex:
        print("📥 Індексуємо README.md у SQLite та Qdrant...")
        chunk_ids = run_pipeline("README.md")
        qdrant_count = client.count(collection_name=COLLECTION_NAME, exact=True).count

    sqlite_cursor.execute("SELECT COUNT(*) FROM chunks")
    sqlite_count = sqlite_cursor.fetchone()[0]

    print("\n" + "═" * 70)
    print(f"✅ Чанків у SQLite : {sqlite_count}")
    print(f"✅ Поінтів у Qdrant: {qdrant_count}")
    print("═" * 70)

    query = " ".join(args) if args else "Run with docker"
    print(f"\n🔎 Запит: \"{query}\"")
    results = search_query(query, limit=20, threshold=0.5)

    print(f"📦 Результатів після авто-обʼєднання (з 20 поінтів): {len(results)}\n")
    for idx, item in enumerate(results, 1):
        preview = item["text"].replace("\n", " ").strip()
        if len(preview) > 140:
            preview = preview
        print(
            f"{idx:2d}. [Score: {item['score']:.4f} | Point: {item['point_id'][:8]} | "
            f"Parent: {str(item['parent_id'])[:8]} | Чанків: {len(item['chunk_ids'])} | "
            f"Символів: {len(item['text'])}]\n"
            f"    💬 {preview}\n"
        )

