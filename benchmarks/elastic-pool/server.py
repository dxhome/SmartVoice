import sys
import json
import argparse
from pathlib import Path
import uvicorn
from smartvoice.app import create_app
from smartvoice.config.settings import Settings, default_data_dir

parser = argparse.ArgumentParser()
parser.add_argument('port', type=int)
parser.add_argument('scenario')
parser.add_argument('idle_seconds', type=float)
parser.add_argument('--num-steps', type=int)
parser.add_argument('--threads-per-instance', type=int)
parser.add_argument('--instances', type=int)
args = parser.parse_args()
scenario = json.loads((Path(__file__).parent / "scenarios.json").read_text())["scenarios"][args.scenario]
idle_seconds = args.idle_seconds
threads_per_instance = args.threads_per_instance or int(scenario.get("threads_per_instance", 2))
instances = args.instances or int(scenario.get("instances", 2))

# Test-only interception: the product adapter keeps its production num_steps=8
# default. This benchmark process may override the GenerationConfig immediately
# before the native Sherpa call, without editing or importing modified product code.
if args.num_steps is not None:
    import sherpa_onnx

    original_generate = sherpa_onnx.OfflineTts.generate

    def generate_with_test_steps(runtime, *positional, **keyword):
        config = keyword.get("config")
        if config is None and len(positional) > 1:
            config = positional[1]
        if not isinstance(config, sherpa_onnx.GenerationConfig):
            raise TypeError("Supertonic step override expected a GenerationConfig argument")
        config.num_steps = args.num_steps
        return original_generate(runtime, *positional, **keyword)

    sherpa_onnx.OfflineTts.generate = generate_with_test_steps

settings = Settings(
    data_dir=default_data_dir(),
    max_concurrent_inference=1,
    num_threads=threads_per_instance,
    min_instances=int(scenario.get("min_instances", 1)),
    max_instances=instances,
    max_queued_inference=int(scenario.get("max_waiting", 4)),
    instance_idle_seconds=idle_seconds,
)
app = create_app(settings)
uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='error', access_log=False)
