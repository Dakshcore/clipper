# clipper

Two small Python tools that work together.

- `clipper/` records live streams or reads videos (YouTube, Twitch, Kick),
  picks moments, and renders vertical short clips with captions into
  `clipper/out/`. See `clipper/README.md`.
- `publisher/` reads `clipper/out/` and uploads the clips to YouTube. See
  `publisher/README.md`.

Run every command from this folder. Each tool has its own virtual
environment and `requirements.txt`.

```
python -m venv clipper/.venv
clipper/.venv/Scripts/python -m pip install -r clipper/requirements.txt
python -m venv publisher/.venv
publisher/.venv/Scripts/python -m pip install -r publisher/requirements.txt
```

Keys go in `clipper/.env` and `publisher/.env` (copy the `.env.example`
files). They are never committed.

Only clip channels you run or have permission to clip. Discovery of top
streams does not check permission; that is on whoever runs it.
