# aiohttp #10570

The workload uses the real aiohttp server and client stream APIs. The server emits an 8 MiB response in small chunks; the client uses a small read buffer and calls `ClientResponse.read()`. This exercises the large-stream drain path changed by PR #10570 without feeding the Analyzer any patch-specific hint.
