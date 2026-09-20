"""Recreation-Bench unified evaluation core.

Platform-agnostic evaluation building blocks shared by all five platform pipelines
(linux / macos / windows / android / web). Platforms differ only in how they
*probe* the UI and *run* tests; the manifest/denominator logic, result parsing,
scoring, and the unified metrics contract live here so there is exactly one
implementation of each.

The release lifecycle and platform boundary are documented in
the shared pipeline contract.
"""
