# Local LongMemEval Data

This directory holds local Akashic-style LongMemEval helpers.

The generated local curated dataset is:

- `eval/longmemeval/data/local_mock_longmemeval.json`

Generate it from the current green/red cases:

```bash
python eval/longmemeval/convert_mock_cases.py --set both
```

The converter embeds each case's `context_sessions` as `haystack_sessions`, so
the local curated suite does not require downloading LongMemEval from
HuggingFace.
Use HuggingFace only when you want the upstream benchmark corpus rather than
this repository's curated regression cases.

Note: the converter and output filename still contain `mock` for historical
compatibility. Replay eval itself does not use a mock execution layer; benchmark
answers should be produced by `eval/replay_runner.py` through the real passive
reply chain.
