# Offline formal-research fixture

These are byte-for-byte copies of the three-company evidence and two local
unadjusted quote records used for the 2026-10-24 acceptance. They allow tests to
run without local `data/acceptance` or paid-run directories. Paths inside the
copied metadata describe historical provenance; tests do not open those paths.

The test harness redirects only the local-price reader to this fixture root.
It does not change runtime code, financial values, prompts, or paid responses.
Network connections are forbidden; model responses are explicitly simulated.
