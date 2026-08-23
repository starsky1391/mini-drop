# Celery #9849 / #9853

The target container starts a real Celery worker with Redis as the broker, then submits many delayed ETA tasks through native `apply_async(countdown=...)`. The case is intended to exercise queue and worker backlog evidence without exposing issue or PR details to the Analyzer.
