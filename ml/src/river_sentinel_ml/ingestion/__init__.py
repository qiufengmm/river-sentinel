"""Data-source adapters for the River Sentinel project.

Adapters in this package are read-only entry points: they return raw records in the
official field layout and never clean, deduplicate, impute, convert units or shift
timezones. Normalization belongs to the downstream snapshot and audit layer.
"""
