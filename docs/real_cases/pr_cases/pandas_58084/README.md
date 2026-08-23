# pandas #58084

The workload runs real pandas `groupby(...).transform(...)` calls on categorical input with many unobserved categories. It is intended for input-triggered slow-path and CPU hotspot source localization.
