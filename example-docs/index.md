# Reconciler Handbook

A short example book, used for the screenshots in the README.

The control plane reconciles desired state with observed state on a fixed
interval, and emits events when they diverge. This loop is **idempotent**: a
second pass over an already-correct system changes nothing.

## Why a loop

Edge-triggered systems lose work when an event is dropped. A level-triggered
loop re-derives the truth every pass, so a missed event costs latency rather
than correctness.

## Retries

Failed reconciles are requeued with exponential backoff and jitter, capped at
five minutes. See [the guide](guide/operations.md) for the runbook.
