# Failover Runbook

## Trigger conditions

Initiate failover when the primary region has been unreachable for more than four minutes, or when write latency exceeds the agreed threshold for three consecutive sampling windows.

## Sequence

1. Freeze writes at the edge.
2. Promote the standby replica and confirm its log position matches the last acknowledged transaction.
3. Repoint the service mesh and drain the old endpoints.
4. Verify the reconciliation job completes before lifting the write freeze.

## After the event

Record the achieved recovery point and recovery time, and raise a change request for anything that had to be done manually.
