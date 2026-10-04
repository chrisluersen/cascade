# Development and verification

Use Python 3.11+ with the repository's runtime dependencies and pytest supplied through your authorized development environment. Do not treat the offline fixture as a production configuration.

From the repository root, run the deterministic suite:

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest --collect-only -q
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q

The default test directory is `tests/`. Its router fixture sets `CASCADE_OFFLINE=1` before importing `cascade.py`, supplies synthetic credentials/providers, and blocks HTTP requests. For additional process-level socket and credential protection, run the suite within an authorized offline test runner. Offline mode skips environment-file and auth-file loading, Bitwarden startup, provider construction, startup probes, and tokenizer initialization. This does not make a normal import generally side-effect-free or configure an upstream service.

The root files `test_cascade.py` and `test_route_log.py` are legacy probes, excluded from default collection; they may access credential-bearing startup paths, contact a running router, or terminate during collection. `test_cascade.py` also contains copied prefix-price logic that is not production pricing. Do not run either as an offline CI gate. A green default suite has no live provider assertions and does not verify provider/SDK compatibility, a Docker build, or deployment. An isolated first-party import test checks the declared Docker COPY layout only; it does not execute Docker.

The in-memory cache uses a detached request identity scoped by endpoint family and free-only policy before adaptive tokens or prompt routing alter the upstream payload. It does not promise maximal deduplication of equivalent requests (omitted `stream` and explicit `false` can differ in the nested payload), multi-tenant isolation, or dynamic capability revocation. After approved routing/catalog/policy changes, restart the deployment to discard old cache entries.

Run `python -m cascade_lib.evaluation PATH_TO_RECEIPTS_JSONL` from the repository root for an offline summary of accepted-task model/topology experiments. The caller must provide complete receipts, including failed attempts and all implementation, coordination, worker, review, retry, and repair usage and costs. Unknown dollar cost remains null, not zero. The CLI computes summaries but cannot certify receipt completeness, quality judgments, or choose a winning model. Its tests use explicitly synthetic receipts, not a paid live evaluation. Extreme integer or aggregate floating-point overflow remains an unhandled-input limitation.

Only the new/touched tests and evaluator have been lint-verified; pre-existing diagnostics in `cascade.py` remain. Offline verification is not approval for production probing, service restarts, or publication.
