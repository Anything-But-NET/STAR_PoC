# `fact_check` — Background Document Conflict Detection (PoC)

Модуль для фонового факт-чекінгу між документами під час інжесту на базі існуючого дерева **STAR** (`spliter.py` + `transformer.py` з `BAAI/bge-m3` Late Chunking + `auto_merge_points`) та локальної 3-класової мультимовної NLI-моделі (`MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7`).

## Як працює пайплайн

1. **Індексація нового документа (`InMemoryStarStore.index_document`):**
   Документ парситься в AST-дерево і векторизується через `BAAI/bge-m3` (Late Chunking). Кожен листовий чанк одразу має контекстний вектор.
2. **Пошук перетинів в інших документах (`search_points` + `auto_merge_points`):**
   Для кожного листового чанка нового документа шукаємо найближчі листи в усіх *інших* документах у базі (`exclude_file_name`) і запускаємо STAR Dual-Trigger Auto-Merge (`quorum` або `parent_is_better`). Пари з `star_score < min_star_score` та дублікати відсікаються без виклику NLI.
3. **Ієрархічна збірка контексту (`build_hierarchical_context`):**
   Для знайденого вузла STAR збираємо компактний контекст у порядку документа (без дублів):
   - початок змердженого вузла (`target_node.chunks[0]`),
   - початок безпосередньої підсекції (`parent(L).chunks[0]`),
   - попередній сусідній чанк (`idx - 1`),
   - сам цільовий лист `L`.
4. **Двонаправлена NLI-перевірка (`IngestConflictChecker`):**
   Пари проганяються батчем через `mDeBERTa-v3` в обох напрямках (`existing -> incoming` та `incoming -> existing`). Якщо `p_contradicted >= contradict_threshold` і `p_contradicted > p_supported`, генерується `ConflictTicket` для адмін-дашборду.

## Запуск демо

З кореня `rag_poc/`:

```bash
.venv/bin/python -m fact_check.demo
```

Для детального виводу всіх перевірених пар кандидатів (включно з тими, що не пройшли поріг тікета):

```bash
.venv/bin/python -m fact_check.demo --verbose
```

## Структура модуля

- `fact_check/store.py` — `InMemoryStarStore`, `.md`/`.docx` завантажувач (`load_document_text`), `auto_merge_points`, `build_hierarchical_context`.
- `fact_check/checker.py` — `IngestConflictChecker`, `ConflictTicket`, `CandidatePairCheck`, `IngestCheckReport`.
- `fact_check/demo.py` — відтворюваний прогін фонового інжесту на тестових файлах із `../sharepoint_dump`.
