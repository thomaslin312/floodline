# Data manifest

Raw downloads live in `data/raw/` and are **never** committed. Every dataset used
must be recorded here with its URL, retrieval date and checksum so a run is
reproducible from a clean checkout.

Add rows as data lands. Anything `io/sources.py` can fetch without a login should
be marked `auto`; everything else records the manual steps.

| # | Dataset | Source URL | Retrieved | File | SHA256 | Fetch |
|---|---------|-----------|-----------|------|--------|-------|
| — | _none yet_ | | | | | |
