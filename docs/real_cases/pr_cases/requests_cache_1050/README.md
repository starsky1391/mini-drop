# requests-cache #1050 / #1052

The workload uses a real `requests_cache.CachedSession` filesystem cache and repeatedly requests unique URLs from a local HTTP server. It records cache file count and size as runtime observations for cache growth localization.
