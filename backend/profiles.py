"""
Enrollment profile store with DTW-based identity matching.

Key design decisions:
  - Each enrolled crossing stores its full amplitude envelope (ENVELOPE_LEN points)
    plus scalar features and metadata.
  - Matching uses DTW on the envelope sequence as the primary distance,
    with a scalar-feature penalty as a tiebreaker.
  - Time-of-day bucketing: profiles store which hour-bucket they were enrolled in.
    At detection time, only profiles enrolled in the same ±TIME_WINDOW_HRS bucket
    are considered (soft constraint — falls back to all profiles if none match).
  - MIN_ENROLLMENTS raised to 30 for a stable average.
  - UNKNOWN_THRESHOLD is a DTW distance; tune after collecting real data.
"""
import json
import numpy as np
from pathlib import Path
from datetime import datetime

PROFILES_PATH     = Path(__file__).parent / "profiles.json"
MIN_ENROLLMENTS   = 30      # crossings required before a profile is usable
UNKNOWN_THRESHOLD = 15.0    # DTW distance above which → unknown (tune per setup)
TIME_WINDOW_HRS   = 3       # ±hours for time-of-day soft constraint
SCALAR_WEIGHT     = 0.2     # blend weight for scalar penalty on top of DTW


# ── DTW ───────────────────────────────────────────────────────────────────────
def _dtw(a: np.ndarray, b: np.ndarray) -> float:
    """
    Standard DTW on two 1-D sequences.
    O(n*m) — fine for ENVELOPE_LEN=64.
    """
    n, m = len(a), len(b)
    cost = np.full((n + 1, m + 1), np.inf)
    cost[0, 0] = 0.0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d = (a[i - 1] - b[j - 1]) ** 2
            cost[i, j] = d + min(cost[i - 1, j],
                                 cost[i, j - 1],
                                 cost[i - 1, j - 1])
    return float(np.sqrt(cost[n, m]))


def _scalar_dist(v1: list[float], v2: list[float]) -> float:
    a, b = np.array(v1), np.array(v2)
    return float(np.linalg.norm(a - b))


class ProfileStore:
    def __init__(self):
        # profiles[name] = list of crossing dicts:
        #   { "envelope": [...], "scalars": [...], "hour": int }
        self.profiles: dict[str, list[dict]] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────
    def _load(self):
        if PROFILES_PATH.exists():
            with open(PROFILES_PATH) as f:
                self.profiles = json.load(f)

    def _save(self):
        with open(PROFILES_PATH, "w") as f:
            json.dump(self.profiles, f, indent=2)

    # ── Enrollment ────────────────────────────────────────────────────────────
    def enroll(self, name: str, features: dict, scalar_vec: list[float]) -> int:
        """Store one crossing for a person."""
        if name not in self.profiles:
            self.profiles[name] = []
        self.profiles[name].append({
            "envelope": features["envelope"],
            "scalars":  scalar_vec,
            "hour":     features.get("hour_of_day", datetime.now().hour),
        })
        self._save()
        return len(self.profiles[name])

    def delete(self, name: str) -> bool:
        if name in self.profiles:
            del self.profiles[name]
            self._save()
            return True
        return False

    def list_profiles(self) -> dict:
        return {name: len(crossings) for name, crossings in self.profiles.items()}

    # ── Identification ────────────────────────────────────────────────────────
    def identify(self, features: dict,
                 scalar_vec: np.ndarray) -> tuple[str, float]:
        """
        Returns (name, dtw_distance).
        Uses DTW on envelope + scalar penalty.
        Applies time-of-day soft constraint.
        """
        query_env    = np.array(features["envelope"], dtype=float)
        query_scalar = scalar_vec.tolist()
        query_hour   = features.get("hour_of_day", datetime.now().hour)

        best_name = "unknown"
        best_dist = float("inf")

        for name, crossings in self.profiles.items():
            if len(crossings) < MIN_ENROLLMENTS:
                continue

            # Time-of-day filter (soft): prefer same-hour crossings
            same_time = [c for c in crossings
                         if abs(c["hour"] - query_hour) <= TIME_WINDOW_HRS
                         or abs(c["hour"] - query_hour) >= (24 - TIME_WINDOW_HRS)]
            pool = same_time if len(same_time) >= max(5, MIN_ENROLLMENTS // 4) else crossings

            # DTW against each enrolled crossing, take median distance
            dtw_dists = []
            for c in pool:
                ref_env = np.array(c["envelope"], dtype=float)
                dtw_d   = _dtw(query_env, ref_env)
                # Add scalar penalty
                sc_d    = _scalar_dist(query_scalar, c["scalars"])
                dtw_dists.append(dtw_d + SCALAR_WEIGHT * sc_d)

            dist = float(np.median(dtw_dists))

            if dist < best_dist:
                best_dist = dist
                best_name = name

        if best_dist > UNKNOWN_THRESHOLD:
            return "unknown", best_dist

        return best_name, best_dist
