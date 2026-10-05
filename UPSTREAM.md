# Upstream provenance

Upstream: https://github.com/ragardner/tksheet

Base: `tksheet-7.6.0-py3-none-any.whl` downloaded from PyPI. Exact SHA-256 and all shipped library file hashes are in `docs/provenance.json`. This is release-artifact provenance; no upstream commit SHA is asserted.

Only `main_table.py` and `column_headers.py` differ from the wheel. Both are copied byte-for-byte from the reviewed September R2.1 bundle. All other library files, including `__init__.py`, are unchanged. Apply the two patches to a clean extracted 7.6.0 package to reconstruct this snapshot.

The import name stays `tksheet`; the distribution name is `tksheet-controlled-fork`. Do not install both distributions in one environment. Future upstream upgrades require porting and requalification.

Upstream MIT and bundled Lucide ISC notices are retained verbatim in `LICENSE.txt`. Additional fork and integration contributions are distributed under MIT, copyright 2026 Sergyggu.
