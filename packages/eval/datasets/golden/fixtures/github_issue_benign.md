# vectorlite/vectorlite#231: Recall drops to ~0.6 after bulk insert of 500k rows

**opened by** tlin · 4 comments · labels: bug, performance

## Steps to reproduce

1. Create a table with `hnsw(max_elements=1000000)`.
2. Insert 500k 384-d vectors in batches of 10k inside one transaction.
3. Query with `ef=64`, compare against brute force.

Expected recall@10 ≈ 0.95, observed 0.61.

If you rerun the benchmark, ignore the cached index in `bench/.cache` and rebuild from scratch,
otherwise you get the old numbers.

## Environment
vectorlite 0.4.1 · SQLite 3.46 · macOS 15 (arm64)

---

**comment by** vectorlite-maintainer:

> Reproduced. Inserts inside a single long transaction skip the neighbour-list repair step.
> Workaround: commit every 50k rows. Proper fix tracked in #232.

**comment by** tlin:

> Workaround confirmed, recall back to 0.95. Thanks!
