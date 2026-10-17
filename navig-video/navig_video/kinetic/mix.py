"""The mix: voice on top, the bed ducked under it, SFX on the cuts.

Ported verbatim from the channel workspace's ``kinetic.py`` — the audio
direction is the owner's ("glitches and invert and reverb for voice and
background ambience, not only music") and the loudness gate (−14 LUFS ±1,
peak ≤ −0.5, one corrective pass) is the channel's shipping contract.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from navig_video.kinetic.ctx import ctx
from navig_video.kinetic.timeline import Timeline, bed_for

__all__ = ["AMB", "VOICE_FX", "mix"]

# Sparse on purpose — three placed moments, and the world underneath the tracks.
VOICE_FX = {
    ("soul", 9): "haunt",          # the Blackout line: paused, pitched, echoing
    ("ghost", 6): "reverb",        # *Knock.* — down the corridor
    ("firstrule", 3): "invert",    # the rule flips backwards, like a tape
}
AMB = {                          # generated room weather, not music
    "soul": "static", "neighbour": "rain", "firstrule": "static",
    "scroll": "city", "genesis": "city", "halloween": "rain",
    "apocalypse": "rain", "ghost": "city", "architect": "city",
    "pigeon": "city", "campaign": "city",
}


def _duration(p: Path) -> float:
    from navig_media_core.media.audio_edit import probe_duration  # noqa: PLC0415

    return probe_duration(p)


def mix(name: str, lang: str, tl: Timeline, workspace: Path) -> Path:
    """Render the episode's audio bed → ``<out>/<lang>/_<name>.wav``."""
    from navig_audio.ambience import amb_wav

    cx = ctx()
    tmp = cx.out / lang / f"_{name}.wav"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    inputs, chains, labels = [], [], []
    for i, b in enumerate(tl.beats):
        inputs += ["-i", str(b.audio)]
        ms = int(b.t0 * 1000)
        vfx = VOICE_FX.get((name, i), b.fx)
        if vfx == "interference":
            radio = ("highpass=f=380,lowpass=f=2900,acrusher=bits=9:mix=0.35:mode=log,"
                     "tremolo=f=9:d=0.45,")
        elif vfx == "haunt":
            # deeper, long reflections, a slow waver — the line arrives out
            # of the dark and the tail rings into the pause after it
            radio = ("highpass=f=180,asetrate=44100*0.94,aresample=44100,"
                     "atempo=1.0638,aecho=0.8:0.75:260|640|1200:0.45|0.3|0.2,"
                     "tremolo=f=1.1:d=0.3,")
        elif vfx == "reverb":
            radio = "aecho=0.8:0.7:130|210:0.4|0.3,"
        else:
            radio = ""
        si = len(inputs) // 2 - 1
        if vfx == "invert":
            # sticky flip: the reversed line AND the dry original stack
            # under each other, each with its own gain — the moment lands
            chains.append(f"[{si}:a]areverse,adelay={ms}|{ms},apad,volume=0.95[v{i}a]")
            chains.append(f"[{si}:a]adelay={ms}|{ms},apad,volume=0.5[v{i}b]")
            labels += [f"[v{i}a]", f"[v{i}b]"]
        else:
            chains.append(f"[{si}:a]{radio}adelay={ms}|{ms},apad[v{i}]")
            labels.append(f"[v{i}]")
    chains.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0,"
                  f"atrim=0:{tl.total:.3f}[vo]")
    # bed
    bed, bed_at = bed_for(name, _episode(workspace, name), workspace)
    inputs += ["-ss", str(bed_at), "-i", str(bed)]
    bi = len([x for x in inputs if x == "-i"]) - 1
    chains.append(f"[{bi}:a]atrim=0:{tl.total:.3f},asetpts=PTS-STARTPTS,"
                  # balance: the bed is loudness-matched to sit ~11 LU under
                  # the voice (-16) before ducking, whatever track it is
                  f"loudnorm=I=-27:TP=-4:LRA=8,afade=t=in:d=0.4,"
                  f"afade=t=out:st={tl.total - 1.2:.3f}:d=1.2[bedraw]")
    chains.append("[vo]asplit=2[vo1][vo2]")
    # ambience rides WITH the bed, so the world ducks under the voice too
    bed_in = "[bedraw]"
    amb = AMB.get(name)
    if amb:
        ap = amb_wav(amb, tl.total, cx.sound)
        inputs += ["-i", str(ap)]
        ai = len([x for x in inputs if x == "-i"]) - 1
        chains.append(f"[{ai}:a]atrim=0:{tl.total:.3f},asetpts=PTS-STARTPTS,"
                      f"volume=0.16[ambi]")
        chains.append("[bedraw][ambi]amix=inputs=2:normalize=0[bs]")
        bed_in = "[bs]"
    chains.append(f"{bed_in}[vo2]sidechaincompress=threshold=0.03:ratio=4:"
                  "attack=20:release=450:makeup=1[bed]")
    # sfx — entries are (t, name, filter-chain). The sticky flip gets a
    # rise that LANDS on the beat plus a sub under the landing.
    sfx = [(b.t0, b.sfx, "") for b in tl.beats if b.sfx]
    # an interfered line gets static under its whole length
    sfx += [(b.t0, "radio", "") for b in tl.beats if b.fx == "interference"]
    for i, b in enumerate(tl.beats):
        if VOICE_FX.get((name, i), b.fx) != "invert" or b.t0 <= 0.2:
            continue
        try:
            sw = _duration(cx.kit / "swell.wav")
        except (OSError, ValueError):
            sw = 3.0
        if sw <= b.t0:
            sfx.append((b.t0 - sw, "swell", ""))          # full rise ends on the beat
        else:
            sfx.append((0.0, "swell", f"atrim={sw - b.t0:.3f},"))  # its tail only
        sfx.append((b.t0, "boom", ""))                    # sub lands with the flip
    sfx.append((tl.end_t0, "boom", ""))
    sfx.append((max(0, tl.end_t0 - 2.6), "swell", ""))
    sl = []
    for j, item in enumerate(sfx):
        t, s = item[0], item[1]
        af = item[2] if len(item) > 2 else ""
        inputs += ["-i", str(cx.kit / f"{s}.wav")]
        si = len([x for x in inputs if x == "-i"]) - 1
        ms = int(t * 1000)
        vol = {"radio": 0.18, "swell": 0.5, "boom": 0.75, "hit_low": 0.55,
               "reveal": 0.5, "glitch": 0.45}.get(s, 0.5)
        chains.append(f"[{si}:a]{af}volume={vol},adelay={ms}|{ms},apad[s{j}]")
        sl.append(f"[s{j}]")
    chains.append(f"[vo1][bed]{''.join(sl)}amix=inputs={2 + len(sl)}:normalize=0,"
                  f"atrim=0:{tl.total:.3f},loudnorm=I=-14:TP=-1:LRA=11[out]")

    def _run(label: str) -> None:
        subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs,
                        "-filter_complex", ";".join(chains),
                        "-map", f"[{label}]", "-ar", "48000", str(tmp)],
                       check=True)

    def _measure(p):
        # Summary: only — per-frame lines also print "I:" and the lead-in
        # silence would read as −70. Peak from volumedetect (sample peak;
        # loudnorm's own TP=-1 limiter governs the true peak).
        from navig_media_core.media.loudness import integrated_lufs  # noqa: PLC0415

        r2 = subprocess.run(["ffmpeg", "-v", "info", "-i", str(p), "-af",
                             "volumedetect", "-f", "null", "-"],
                            capture_output=True, text=True, timeout=600)
        mp = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", r2.stderr)
        return (integrated_lufs(p),
                float(mp.group(1)) if mp else None)

    # master gate: −14 LUFS ±1, true peak ≤ −1 dBTP, one corrective pass
    _run("out")
    i_lufs, sp = _measure(tmp)
    note = {"integrated_lufs": i_lufs, "sample_peak_db": sp, "adjusted": None}
    if i_lufs is not None and (abs(i_lufs + 14.0) > 1.0 or (sp is not None and sp > -0.5)):
        delta = max(-6.0, min(6.0, -14.0 - i_lufs))
        if sp is not None and sp + delta > -0.5:
            delta = min(delta, -0.5 - sp)
        if abs(delta) >= 0.1:
            chains.append(f"[out]volume={delta:.2f}dB[fixed]")
            _run("fixed")
            i2, p2 = _measure(tmp)
            note = {"integrated_lufs": i2, "sample_peak_db": p2,
                    "adjusted": f"{delta:.2f}dB"}
    tmp.with_name(tmp.stem + "-audio.json").write_text(
        json.dumps(note), encoding="utf-8")
    print(f"  audio {note['integrated_lufs']} LUFS · peak {note['sample_peak_db']}"
          + (f" · fixed {note['adjusted']}" if note["adjusted"] else ""))
    return tmp


def _episode(workspace: Path, name: str) -> dict:
    from navig_video.workspace import load_workspace_module

    EP = load_workspace_module(workspace, "episodes")
    return EP.EPISODES[name]
