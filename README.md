# gundi-integration-spidertracks
Spidertracks action runner.

## Extra Information
This Action Runner connects data from Spidertrack that you would otherwise view at https://app.spidertracks.io/.

Setting up a connection requires a manual step for creating a Private Consumer AFF Feed for a Spidertracks user.
Once that is complete, invite the user to your Spidertracks organization to make their AFF Feed available in your organization.
Assign your individual aircraft to Private Consumer AFF Feed to instruct Spidertracks to include it in the feed.

Once those steps are complete, you're ready to connect using this Action Runner.

## Spidertracks feed CLI

A tool for investigating a customer's Spidertracks feed directly. Reuses the
integration's own client, so it shows exactly what the integration sees.

```bash
# Credentials via env vars (or pass -u/-p; password is prompted if omitted)
export SPIDERTRACKS_USERNAME=acme
export SPIDERTRACKS_PASSWORD=...

# Verify credentials and connectivity
python -m app.cli check

# Per-aircraft rollup over the last 7 days
python -m app.cli summary --since 7d

# List positions for one aircraft
python -m app.cli positions --registration ZK-ABC --since 48h

# Dump the raw XML response (for debugging parsing issues)
python -m app.cli positions --raw --since 1h
```

Add `--json` to any command for machine-readable output, `--include-heartbeat`
to keep the heartbeat ESN, and `--no-retry` to surface the first failure
immediately.
