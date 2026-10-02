import sys
import json
from pathlib import Path
import uvicorn
from smartvoice.app import create_app
from smartvoice.config.settings import Settings, default_data_dir

scenario = json.loads((Path(__file__).parent / "scenarios.json").read_text())["scenarios"][sys.argv[2]]
idle_seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 300.0
settings = Settings(
    data_dir=default_data_dir(),
    max_concurrent_inference=1,
    num_threads=int(scenario.get("threads_per_instance", 2)),
    min_instances=int(scenario.get("min_instances", 1)),
    max_instances=int(scenario.get("instances", 2)),
    max_queued_inference=int(scenario.get("max_waiting", 4)),
    instance_idle_seconds=idle_seconds,
)
app = create_app(settings)
uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]), log_level='error', access_log=False)
