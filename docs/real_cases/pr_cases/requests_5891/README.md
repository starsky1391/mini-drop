# requests #5891

This case exercises repeated real `requests.Session.get()` calls against a local HTTP server. It targets the performance regression fixed by PR #5924 without passing issue or patch hints to the Analyzer.
