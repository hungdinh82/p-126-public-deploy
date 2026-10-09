"""One local page for testing real, resident STT / SLM / TTS models."""
import argparse
import signal
import threading

from local_ai_runtime import LocalAIServer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8789)
    parser.add_argument("--device", choices=["cuda", "cpu"], default="cuda")
    parser.add_argument("--gpu-layers", type=int, default=12,
                        help="Partial offload fits a 4 GB GPU; Xavier uses full offload")
    args = parser.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        with LocalAIServer(device=args.device, speech=True, port=args.port,
                           gpu_layers=args.gpu_layers) as server:
            print(f"\nMở {server.url}/test — Ctrl+C để dừng cả ba model.\n", flush=True)
            stop.wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
