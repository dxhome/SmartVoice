"""Pinned causal gain policy; never removes or delays samples."""
import math
import numpy as np


class CausalGain:
    def __init__(self, policy='none'):
        self.policy = policy
        self.envelope = 0.0
        self.enabled = policy != 'none'
        self.max_gain = 512.0 if policy == 'gain_v3' else (128.0 if policy == 'gain_v2' else 8.0)
        self.energy_floor = 0.00001 if policy == 'gain_v3' else (0.00005 if policy == 'gain_v2' else 0.004)
        self.gain = 1.0
        self.peak_gain = 1.0

    def process(self, samples):
        if not self.enabled or not len(samples):
            return samples
        rms = float(np.sqrt(np.mean(samples * samples)))
        peak = float(np.max(np.abs(samples)))
        if self.policy == 'gain_v4':
            # Peak-envelope AGC avoids syllable-by-syllable RMS pumping. Causal,
            # bounded, float-domain only; no samples are removed or delayed.
            self.envelope = max(peak, self.envelope * math.exp(-len(samples) / (16000 * 0.8)))
            target = min(512.0, max(1.0, 0.15 / max(self.envelope, 1 / 32768))) if peak >= 1 / 32768 else self.gain
            tau = 0.02 if target < self.gain else 0.04
            alpha = 1 - math.exp(-len(samples) / (16000 * tau))
            self.gain += alpha * (target - self.gain)
            actual = min(self.gain, 0.95 / max(peak, 0.001))
            self.peak_gain = max(self.peak_gain, actual)
            return (samples * actual).astype(np.float32, copy=False)
        # Never increase gain for very low energy alone; bounded and causal.
        target = min(self.max_gain, max(1.0, 0.04 / max(rms, self.energy_floor))) if rms >= self.energy_floor else self.gain
        alpha = 1 - math.exp(-len(samples) / (16000 * 0.04))
        self.gain += alpha * (target - self.gain)
        actual = min(self.gain, 0.95 / max(peak, 0.001))
        self.peak_gain = max(self.peak_gain, actual)
        return (samples * actual).astype(np.float32, copy=False)
