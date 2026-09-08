# Operations

## Draining a node

Cordon first, then drain with a grace period. Watch the reconcile lag metric.

```bash
kubectl cordon node-7
kubectl drain node-7 --grace-period=60
```

## Reading the metrics

`reconcile_lag_seconds` is the age of the oldest unprocessed item. Sustained
growth means the loop cannot keep up.
