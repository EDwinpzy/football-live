"""Measure app delay relative to the concurrently decoded original HLS output.

At each screenshot time, compare the matching app frame to the newest frame that
the direct-source decoder has emitted. This subtracts the reference decoder's
own live/network delay instead of treating its absolute frame arrival as source
wall-clock time. Weak or static-image matches remain inconclusive.
No source URL, referer or credentials are written to the evidence file.
"""
import argparse
import hashlib
import io
import json
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

REFERENCE_FPS = 5


def picture_feature(picture):
    gray = np.asarray(picture.convert('L'))
    # Find the largest visible video band in portrait letterboxed playback.
    active = np.flatnonzero((gray > 16).mean(axis=1) > .35)
    if active.size:
        groups = np.split(active, np.flatnonzero(np.diff(active) > 1) + 1)
        band = max(groups, key=len)
        if len(band) > picture.width * .3:
            picture = picture.crop((0, int(band[0]), picture.width, int(band[-1]) + 1))
    return np.asarray(picture.convert('L').resize((64, 36)), dtype=np.float32)


def estimate_added_delay(reference, at, feature, screenshot_end=None):
    """Return a confidence-scored frame offset against the direct-source head.

    `reference` contains (frame index, monotonic arrival time, image feature)
    triples. Frame indices avoid distorted timing when FFmpeg emits a complete
    HLS segment in a short burst. Comparing to the newest raw frame cancels
    shared source packaging/network/decoder latency.
    """
    candidates = [(index, stamp, image) for index, stamp, image in reference
                  if stamp <= at and at - stamp <= 20]
    if len(candidates) < 10:
        return None
    head_index, head_stamp, _ = candidates[-1]
    end = at if screenshot_end is None else screenshot_end
    end_heads = [index for index, stamp, _ in reference if stamp <= end]
    end_head_index = end_heads[-1] if end_heads else head_index
    distances = np.array([np.abs(feature - image).mean() for _, _, image in candidates])
    best = int(distances.argmin())
    matched_index, matched_stamp, _ = candidates[best]
    alternatives = [
        float(score) for (index, _, _), score in zip(candidates, distances)
        if abs(index - matched_index) >= 2 * REFERENCE_FPS
    ]
    margin = min(alternatives) - float(distances[best]) if alternatives else 0.0
    delay = max(0, head_index - matched_index) / REFERENCE_FPS
    return {
        'addedDelaySeconds': round(delay, 3),
        'referenceHeadFrameIndex': head_index,
        'referenceHeadFrameIndexAtScreenshotEnd': end_head_index,
        'matchedReferenceFrameIndex': matched_index,
        'referenceHeadIdleSeconds': round(max(0.0, at - head_stamp), 3),
        'imageDifference': round(float(distances[best]), 3),
        'alternativeMargin': round(float(margin), 3),
        'distinctMatch': bool(distances[best] < 3 and margin >= 2.5),
        'referenceFrameAgeSeconds': round(max(0.0, at - matched_stamp), 3),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adb', required=True)
    parser.add_argument('--serial', action='append', required=True)
    parser.add_argument('--api-base', default='http://127.0.0.1:18090')
    parser.add_argument('--match-id', required=True)
    parser.add_argument('--seconds', type=int, default=45)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Use a new evidence file.')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        parser.error('ffmpeg is required.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'{args.api_base}/api/stream/{args.match_id}?src=all', timeout=45) as response:
        sources = json.load(response).get('sources', [])
    if not sources:
        parser.error('No playable source.')
    source = sources[0]
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-rw_timeout', '15000000',
               '-headers', 'Referer: ' + source.get('referer', '') + '\r\n',
               '-live_start_index', '-1', '-i', source['url'], '-an',
               '-vf', 'fps=5,scale=64:36', '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    reference = []
    samples = []
    lock = threading.Lock()
    stop = threading.Event()
    started = time.monotonic()

    def decode_original():
        while not stop.is_set():
            frame = process.stdout.read(64 * 36)
            if len(frame) != 64 * 36:
                break
            arrival = time.monotonic()
            feature = np.frombuffer(frame, dtype=np.uint8).reshape(36, 64).astype(np.float32)
            with lock:
                reference.append((len(reference), arrival, feature))

    worker = threading.Thread(target=decode_original, daemon=True)
    worker.start()
    try:
        while time.monotonic() - started < args.seconds:
            for serial in args.serial:
                before = time.monotonic()
                result = subprocess.run([args.adb, '-s', serial, 'exec-out', 'screencap', '-p'],
                                        capture_output=True, timeout=10, check=True)
                after = time.monotonic()
                with Image.open(io.BytesIO(result.stdout)) as picture:
                    feature = picture_feature(picture)
                samples.append((serial, (before + after) / 2, after, after - before, feature))
            stop.wait(.5)
    finally:
        stop.set()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        worker.join(timeout=2)
    rows = []
    for serial, at, after, uncertainty, feature in samples:
        estimate = estimate_added_delay(reference, at, feature, after)
        if estimate is None:
            continue
        rows.append({
            'serial': serial,
            'elapsedSeconds': round(at - started, 3),
            'screenshotTimingSpanSeconds': round(uncertainty, 3),
            **estimate,
        })
    source_id = str(source.get('source') or '')
    report = {'matchId': args.match_id, 'sourceId': source_id,
              'sourceFingerprint': hashlib.sha256(source_id.encode('utf-8')).hexdigest() if source_id else None,
              'upstreamFingerprint': hashlib.sha256(source['url'].encode('utf-8')).hexdigest(),
              'referenceFrames': len(reference), 'referenceFps': REFERENCE_FPS, 'samples': rows,
              'method': 'At screenshot midpoint, compare the app frame to the newest frame emitted by a concurrent direct-source decoder; added delay is their reference-frame time difference.',
              'limitations': ['The comparison is relative to this direct-source decoder output, not an external broadcast clock.',
                              'The selected reference source is not proof of the source actually used by the app.',
                              'Frame matching, source progress and decoder freshness are heuristics, not proof of continuous playback or live-edge proximity.',
                              'Static images, stale raw reference output and low match confidence cannot prove the latency gate.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    for serial in args.serial:
        selected = [row['addedDelaySeconds'] for row in rows
                    if row['serial'] == serial and row['distinctMatch']]
        print(json.dumps({'serial': serial, 'distinctSamples': len(selected),
                          'medianAddedDelaySeconds': round(float(np.median(selected)), 3) if selected else None,
                          'maxAddedDelaySeconds': max(selected) if selected else None}), flush=True)


if __name__ == '__main__':
    main()
