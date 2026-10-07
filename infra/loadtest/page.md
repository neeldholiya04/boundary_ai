# vectorlite

A small embedded vector index for local retrieval experiments. It stores dense embeddings in a single
file next to your data and answers nearest-neighbour queries without a separate server.

## Installation

    pip install vectorlite

vectorlite needs Python 3.10 or newer. Wheels are published for Linux, macOS and Windows on x86-64
and arm64. Building from source needs a C compiler and the numpy headers.

## Quick start

    import vectorlite
    index = vectorlite.open("notes.vlite", dim=384)
    index.add(ids, embeddings)
    hits = index.search(query_embedding, k=5)

Each hit carries the id you inserted and a cosine similarity between -1 and 1. Ids are opaque
strings, so you can use file paths, database keys or anything else that identifies a document.

## How it works

The index keeps vectors in fixed-size pages and builds a navigable small-world graph over them.
Searches start from a few entry points and walk the graph greedily towards the query, keeping a
bounded candidate list. The `ef_search` parameter trades recall for speed: larger values explore
more of the graph and find better neighbours at the cost of latency.

Writes go to a write-ahead log first and are folded into the graph in the background. A crash
between the two loses nothing: on the next open, the log is replayed. Compaction rewrites the file
without deleted vectors; it runs automatically when more than a quarter of the pages are garbage.

## Configuration

| Option | Default | Meaning |
|---|---|---|
| `dim` | required | embedding dimension; fixed when the file is created |
| `metric` | `cosine` | `cosine`, `dot` or `l2` |
| `ef_construction` | 200 | graph quality at insert time |
| `ef_search` | 64 | candidates kept during a search |
| `cache_mb` | 64 | page cache size |

## Known issues

After upgrading from 0.6 to 0.7, some users see searches return fewer results than before. The
on-disk format changed, and an index written by 0.6 keeps its old graph until it is rebuilt.
Workaround: run `vectorlite rebuild notes.vlite`, then clear the page cache directory and restart
your process. A fix that migrates old files automatically is planned for 0.7.1.

Large batch inserts can briefly double memory use while the log is folded in. If memory is tight,
insert in batches of a few thousand vectors and call `index.flush()` between them.

## Benchmarks

On a laptop with 16 GB of RAM, a one-million-vector index of 384-dimensional embeddings answers a
top-10 query in about 2 ms at 0.95 recall with the default settings. Building that index takes
around four minutes. Numbers vary with hardware and with the distribution of your embeddings, so
measure on your own data before choosing settings.

## Contributing

Bug reports with a minimal reproduction are the most useful kind. Please include the vectorlite
version, your Python version and operating system, and the smallest script that shows the problem.
Pull requests should come with a test; run `pytest` and `ruff check` before opening one.

## Licence

MIT. See LICENSE for the full text.
