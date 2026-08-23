# urllib3 #2494

The workload uses a real `urllib3.Retry` configuration against a local HTTP server that returns 503 responses. On revisions that support `backoff_max`, the workload uses it; older revisions exercise the unbounded default behavior.
