# PyAV #751 / #1383

The workload uses the real PyAV logging capture and media-open APIs from several Python threads. It intentionally keeps FFmpeg logging enabled while repeatedly opening invalid media input, then disables the logging hook at shutdown. The runner observes thread liveness, process stacks, logs, and exit behavior without injecting PR text into Analyzer input.
