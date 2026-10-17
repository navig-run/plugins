"""Mixing and mastering — a vocal over a beat, out as a finished song at a known loudness.

  loudness.py   integrated LUFS · loudness range · true peak (ffmpeg's EBU R128 meter)
  chain.py      the presets and the filter graphs: vocal chain, beat carve + duck, glue
  master.py     the pipeline: pre-master → gain + limiter to the target, measured → encode

Everything is an ffmpeg filter graph: deterministic, no plugin hosts, and reproducible from
the sidecar JSON each master writes beside itself.
"""
