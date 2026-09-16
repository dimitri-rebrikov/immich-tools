# immich-tools

Small scripts for the [Immich](https://immich.app) API.

## `favorite_rated.py`

Finds images with a star rating >= `--min-rating` (default 3) that are **not** favorited yet and sets the favorite flag.

- Only asset **metadata** (JSON) is fetched - image bytes are never downloaded.
- **Dry run by default**: nothing is written unless `--apply` is passed.
- Uses the current Immich search API (structured `filter` + cursor pagination, v3.2.0+) and `PATCH /api/assets`.

### Requirements

- Immich **3.2.0 or newer** (older servers are rejected with a clear message).
- An API key with permissions: `asset.read`, `asset.update`, `user.read`.
- [uv](https://docs.astral.sh/uv/) - the script carries PEP 723 metadata, so `uv run` fetches nothing but Python itself.

### Usage

```bash
export IMMICH_URL=https://immich.example.com
export IMMICH_API_KEY=xxxxxxxx

# dry run: list what would be favorited (default, writes nothing)
uv run favorite_rated.py

# canary: favorite 5 assets, keeping a revert report
uv run favorite_rated.py --limit 5 --apply --json-report favorites.json

# full run, rating threshold 4
uv run favorite_rated.py --min-rating 4 --apply

# undo a previous run
uv run favorite_rated.py --revert favorites.json --apply
```

`--url`/`--api-key` can be passed as flags instead of environment variables.

### What is searched

```json
{
  "filter": {
    "type": { "eq": "IMAGE" },
    "rating": { "gte": 3 },
    "isFavorite": { "eq": false },
    "trashedAt": { "eq": null }
  },
  "orderBy": { "field": "fileCreatedAt", "direction": "desc" },
  "withExif": true,
  "size": 250
}
```

Archived images are included, trashed and locked ones are not. Because search spans partner
libraries while `PATCH /api/assets` only accepts assets you own, every result is re-checked
client side (`ownerId`, `isFavorite`, `rating`) before it is written.

### Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--url`, `--api-key` | env `IMMICH_URL`, `IMMICH_API_KEY` | server and credentials |
| `--min-rating N` | `3` | minimum star rating (1-5) |
| `--apply` | off | actually write; otherwise dry run |
| `--limit N` | - | stop after N candidates |
| `--page-size N` | `250` | search page size, 1-1000 |
| `--batch-size N` | `500` | assets per update request |
| `--json-report PATH` | - | write affected assets for later `--revert` |
| `--revert PATH` | - | remove the favorite flag from a report's assets |
| `--retries N`, `--timeout S` | `3`, `30` | retry/backoff and request timeout |
| `--insecure` | off | skip TLS verification (self-signed homelab certs) |
| `-q`, `-v` | - | quiet progress / log every HTTP request |

Exit codes: `0` success, `1` some update batches failed, `2` configuration or API error.

### Tests

```bash
uv run tests/test_favorite_rated.py
```

The HTTP layer is faked, so the suite never touches a real server.

## License

MIT, see [LICENSE](LICENSE).
