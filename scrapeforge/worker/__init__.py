"""Ingestion workers (Phase 6).

The event-driven pipeline runs two stateless worker types over the queue ports:

- ``scraper_worker`` — consumes the JOB queue, fetches via ``ScrapeEngine`` (curl_cffi,
  minimal parse), writes the RAW payload to the object store (claim-check), and publishes
  a small pointer to the RESULTS queue.  Never writes the serving DB.
- ``transform_worker`` — consumes the RESULTS queue, reads the raw payload, validates /
  cleans / normalizes / dedupes (idempotent), and UPSERTs structured rows via
  ``PostgresSink`` (the sole writer of structured data).

``messages`` defines the shared message contracts both workers agree on, so the producer
and consumer never drift.  See ``ingestion-pipeline-architecture`` (memory) and SPEC §7.5.
"""
