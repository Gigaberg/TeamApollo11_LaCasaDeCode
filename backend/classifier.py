"""
CSI Activity Classifier — production inference wrapper
======================================================
Loads trained Random Forest or CNN model and provides a simple
predict() interface for real-time classification in server.py

Usage in server.py:
    from classifier import ActivityClassifier
    
    clf = ActivityClassifier(model_path="rf_model.pkl")
    # or
    clf = ActivityClassifier(model_path="cnn_model.keras")
    
    # In your CSI processing loop:
    label, confidence = clf.predict(amplitude_vector)
    # label: 0=empty, 1=walking, 2=stationary, 3=breathing, 4=fall
    # confidence: 0.0-1.0 probability of predicted class
"""

import pickle
from pathlib import Path
import numpy as np

# Try importing TensorFlow for CNN support
try:
    from tensorflow import keras
    TF_AVAILABLE = True
except ImportError:
    TF_AVAILABLE = False


CLASS_NAMES = ["empty", "walking", "stationary", "breathing", "fall"]
N_SUBCARRIERS = 64
SENSITIVE = [19, 20, 21, 22, 23, 24, 25, 26, 38, 39]


class ActivityClassifier:
    """Unified interface for both Random Forest and CNN models."""
    
    def __init__(self, model_path: str):
        self.model_path = Path(model_path)
        self.model_type = None
        self.model = None
        self.scaler = None
        self.norm_params = None
        
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model not found: {model_path}")
        
        self._load_model()
    
    def _load_model(self):
        """Auto-detect model type and load."""
        if self.model_path.suffix == ".pkl":
            self._load_rf()
        elif self.model_path.suffix in [".keras", ".h5"]:
            self._load_cnn()
        else:
            raise ValueError(f"Unknown model format: {self.model_path.suffix}")
    
    def _load_rf(self):
        """Load Random Forest + scaler."""
        with open(self.model_path, "rb") as f:
            data = pickle.load(f)
        self.model = data["model"]
        self.scaler = data["scaler"]
        self.model_type = "rf"
        print(f"✅ Loaded Random Forest from {self.model_path}")
    
    def _load_cnn(self):
        """Load Keras CNN + normalization params."""
        if not TF_AVAILABLE:
            raise ImportError("TensorFlow not installed — cannot load CNN model")
        self.model = keras.models.load_model(self.model_path)
        norm_path = self.model_path.parent / "cnn_norm.pkl"
        if norm_path.exists():
            with open(norm_path, "rb") as f:
                self.norm_params = pickle.load(f)
        else:
            print("⚠️  cnn_norm.pkl not found — using default normalization")
            self.norm_params = {"mean": 13.0, "std": 2.0}
        self.model_type = "cnn"
        print(f"✅ Loaded 1D CNN from {self.model_path}")
    
    def _extract_features(self, amp_vector: np.ndarray) -> np.ndarray:
        """Extract engineered features for Random Forest."""
        sensitive_amps = [amp_vector[i] for i in SENSITIVE]
        all_amps = amp_vector[amp_vector > 0]
        
        features = [
            np.mean(sensitive_amps),
            np.std(sensitive_amps),
            np.max(sensitive_amps),
            np.min(sensitive_amps),
            np.median(sensitive_amps),
            np.percentile(sensitive_amps, 75) - np.percentile(sensitive_amps, 25),
            np.mean(all_amps),
            np.std(all_amps),
            np.var(all_amps),
            np.sum(np.diff(sensitive_amps) ** 2),
            np.max(sensitive_amps) / (np.mean(sensitive_amps) + 1e-9),
            np.sum(np.array(sensitive_amps) ** 2) / (np.sum(all_amps ** 2) + 1e-9),
        ]
        return np.array(features).reshape(1, -1)
    
    def predict(self, amp_vector: np.ndarray) -> tuple[int, float]:
        """
        Predict activity class from amplitude vector.
        
        Args:
            amp_vector: shape (64,) — amplitudes for all subcarriers
        
        Returns:
            (label, confidence) where label is 0-4 and confidence is 0.0-1.0
        """
        if len(amp_vector) != N_SUBCARRIERS:
            raise ValueError(f"Expected {N_SUBCARRIERS} amplitudes, got {len(amp_vector)}")
        
        if self.model_type == "rf":
            # Extract features and scale
            X_feat = self._extract_features(amp_vector)
            X_scaled = self.scaler.transform(X_feat)
            
            # Predict
            label = int(self.model.predict(X_scaled)[0])
            probs = self.model.predict_proba(X_scaled)[0]
            confidence = float(probs[label])
            
        elif self.model_type == "cnn":
            # Normalize and reshape
            X_norm = (amp_vector - self.norm_params["mean"]) / (self.norm_params["std"] + 1e-9)
            X_cnn = X_norm.reshape(1, N_SUBCARRIERS, 1)
            
            # Predict
            probs = self.model.predict(X_cnn, verbose=0)[0]
            label = int(np.argmax(probs))
            confidence = float(probs[label])
        
        else:
            raise RuntimeError("Model not loaded")
        
        return label, confidence
    
    def predict_name(self, amp_vector: np.ndarray) -> tuple[str, float]:
        """Same as predict() but returns class name instead of integer."""
        label, conf = self.predict(amp_vector)
        return CLASS_NAMES[label], conf


# ── Quick test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python classifier.py <model_path>")
        print("Example: python classifier.py rf_model.pkl")
        sys.exit(1)
    
    clf = ActivityClassifier(sys.argv[1])
    
    # Test with dummy data
    print("\n🧪 Testing with dummy amplitude vector...")
    dummy = np.random.uniform(10, 15, N_SUBCARRIERS)
    label, conf = clf.predict(dummy)
    print(f"   Predicted: {CLASS_NAMES[label]} (confidence: {conf:.3f})")
