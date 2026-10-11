# Wake-word models

Served at `/wake/<name>` for `wake.js` ("ViVi ơi" / "Hey ViVi", detected in the browser).

- `melspectrogram.onnx`, `embedding_model.onnx`: openWakeWord's fixed front end (Apache-2.0),
  copied from the `openwakeword` package's `resources/models`.
- `vivi.onnx`: the classifier trained by `scripts/train_wake.py`; `vivi.json` is its training report.

After retraining, replace the model and redeploy; the route sends `no-cache`, so browsers pick it up:

    cp models/wake/vivi.onnx models/wake/vivi.json assets/wake/
