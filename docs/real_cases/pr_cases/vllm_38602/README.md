# vLLM #38602 / #41218

The intended workload is a real multi-node Ray vLLM request with `logprobs` enabled. The current VM has no NVIDIA GPU, so the shared runner performs a hardware preflight and records an explicit blocked result instead of claiming a CPU-only run is equivalent. A GPU-capable VM is required for the full case.
