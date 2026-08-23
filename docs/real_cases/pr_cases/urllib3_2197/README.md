# urllib3 #2197

The workload drives a real `urllib3.PoolManager` with a small blocking pool while concurrent requests hold connections. It is intended for pool exhaustion and wait-site evidence.
