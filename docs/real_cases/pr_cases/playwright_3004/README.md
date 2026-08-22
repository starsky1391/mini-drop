# Playwright Python #3004

The vulnerable revision is the parent of PR #3031. The workload starts the real async Playwright driver, launches Chromium, performs concurrent page operations, and repeatedly closes those operations. The runner supplies only the target PID, container metadata, source root, revision, and symptom window to the Agent.
