"""
CSI amplitude + per-subcarrier feature extractor.

Richer feature set per crossing:
  - Amplitude envelope shape  : resampled to fixed-length curve (used for DTW)
  - Crossing speed            : total frames in event (raw speed proxy)
  - Subcarrier selectivity    : which subcarriers are most disturbed (top-k mask)
  - Centre of mass            : weighted mean subcarrier index of disturbance
  - Classic scalar features   : peak variance, rise/fall time, energy, skewness

The extractor expects either:
  a) push(amplitude)                  — single aggregate amplitude per frame
  b) push_multi(amplitudes, sc_amps)  — aggregate + per-subcarrier array

When only aggregate amplitude is available the subcarrier features are zeroed.
"""
import numpy as np
from collections import deque
from scipy.interpolate import interp1d

# ── Tuning constants ──────────────────────────────────────────────────────────
WINDOW_SIZE        = 200    # max frames captured per event (~2s at 100 Hz)
CROSSING_THRESHOLD = 2.0    # z-score to start an event (lowered for real CSI)
EVENT_END_Z        = 0.5    # z-score to end an event
MIN_EVENT_FRAMES   = 15     # ignore very short blips
ENVELOPE_LEN       = 64     # resample every envelope to this length for DTW
N_SUBCARRIERS      = 10     # number of subcarrier channels expected
TOP_K_SC           = 3      # how many subcarriers count as "most disturbed"


class CSIFeatureExtractor:
    def __init__(self):
        self.baseline_buf  = deque(maxlen=300)
        self.baseline_mean = 0.0
        self.baseline_std  = 1.0
        self.calibrated    = False

        self.in_event      = False
        self.amp_buf: list[float]            = []
        self.sc_buf:  list[list[float]]      = []   # per-frame subcarrier amps

    # ── Baseline ──────────────────────────────────────────────────────────────
    def _update_baseline(self, amp: float):
        self.baseline_buf.append(amp)
        if len(self.baseline_buf) >= 100:
            self.baseline_mean = float(np.mean(self.baseline_buf))
            self.baseline_std  = max(float(np.std(self.baseline_buf)), 0.01)
            self.calibrated    = True

    # ── Public push interfaces ────────────────────────────────────────────────
    def push(self, amplitude: float) -> dict | None:
        """Single aggregate amplitude per frame."""
        return self.push_multi(amplitude, None)

    def push_multi(self, amplitude: float,
                   sc_amplitudes: list[float] | None) -> dict | None:
        """
        Aggregate amplitude + optional per-subcarrier amplitudes.
        Returns feature dict when a crossing event completes, else None.
        """
        self._update_baseline(amplitude)
        if not self.calibrated:
            return None

        z = (amplitude - self.baseline_mean) / self.baseline_std

        if not self.in_event:
            if z > CROSSING_THRESHOLD:
                self.in_event = True
                self.amp_buf  = [amplitude]
                self.sc_buf   = [sc_amplitudes or []]
        else:
            self.amp_buf.append(amplitude)
            self.sc_buf.append(sc_amplitudes or [])

            ended = (z < EVENT_END_Z and len(self.amp_buf) >= MIN_EVENT_FRAMES)
            capped = len(self.amp_buf) >= WINDOW_SIZE

            if ended or capped:
                features = self._extract(self.amp_buf, self.sc_buf)
                self.in_event = False
                self.amp_buf  = []
                self.sc_buf   = []
                return features

        return None

    # ── Feature extraction ────────────────────────────────────────────────────
    def _extract(self, amp_frames: list[float],
                 sc_frames: list[list[float]]) -> dict:
        arr  = np.array(amp_frames, dtype=float)
        norm = arr - self.baseline_mean          # baseline-subtracted envelope
        n    = len(norm)

        # ── Envelope shape (resampled to ENVELOPE_LEN for DTW) ────────────────
        x_orig = np.linspace(0, 1, n)
        x_new  = np.linspace(0, 1, ENVELOPE_LEN)
        envelope = interp1d(x_orig, norm, kind='linear')(x_new).tolist()

        # ── Crossing speed ────────────────────────────────────────────────────
        crossing_speed = n  # raw frame count; DTW handles normalisation

        # ── Classic scalar features ───────────────────────────────────────────
        peak_idx  = int(np.argmax(norm))
        peak_val  = float(norm[peak_idx])
        rise_time = peak_idx / max(n, 1)
        fall_time = (n - peak_idx) / max(n, 1)
        spike_dur = n / WINDOW_SIZE

        top_mask      = norm > (0.7 * peak_val)
        peak_variance = float(np.var(norm[top_mask])) if top_mask.sum() > 1 else 0.0
        energy        = float(np.sum(norm ** 2) / n)
        mu            = float(np.mean(norm))
        sigma         = float(np.std(norm)) + 1e-9
        skewness      = float(np.mean(((norm - mu) / sigma) ** 3))

        # ── Subcarrier features ───────────────────────────────────────────────
        sc_selectivity = [0.0] * N_SUBCARRIERS
        centre_of_mass = float(N_SUBCARRIERS) / 2.0   # default: centre

        valid_sc = [f for f in sc_frames if len(f) == N_SUBCARRIERS]
        if valid_sc:
            sc_arr   = np.array(valid_sc, dtype=float)          # (frames, N_SC)
            sc_var   = np.var(sc_arr, axis=0)                   # variance per SC
            sc_norm  = sc_var / (sc_var.sum() + 1e-9)           # normalised

            # Top-k selectivity mask (1 = most disturbed, 0 = quiet)
            top_k_idx = np.argsort(sc_var)[-TOP_K_SC:]
            mask = np.zeros(N_SUBCARRIERS)
            mask[top_k_idx] = 1.0
            sc_selectivity = mask.tolist()

            # Centre of mass of disturbance across subcarrier indices
            indices        = np.arange(N_SUBCARRIERS, dtype=float)
            centre_of_mass = float(np.sum(indices * sc_norm))

        return {
            # Shape sequence (used by DTW matcher)
            "envelope":        envelope,
            # Scalar features (used as secondary distance)
            "peak_variance":   peak_variance,
            "spike_duration":  spike_dur,
            "rise_time":       rise_time,
            "fall_time":       fall_time,
            "energy":          energy,
            "skewness":        skewness,
            "peak_amplitude":  peak_val,
            "crossing_speed":  crossing_speed,
            # Subcarrier features
            "sc_selectivity":  sc_selectivity,
            "centre_of_mass":  centre_of_mass,
            # Metadata
            "sample_count":    n,
            "hour_of_day":     _current_hour(),
        }

    # ── Convenience: scalar vector (for fallback / logging) ───────────────────
    def scalar_vector(self, features: dict) -> np.ndarray:
        return np.array([
            features["peak_variance"],
            features["spike_duration"],
            features["rise_time"],
            features["fall_time"],
            features["energy"],
            features["skewness"],
            features["peak_amplitude"],
            features["crossing_speed"] / WINDOW_SIZE,
            features["centre_of_mass"] / N_SUBCARRIERS,
        ] + features["sc_selectivity"], dtype=float)


def _current_hour() -> int:
    from datetime import datetime
    return datetime.now().hour
