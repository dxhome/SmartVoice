"""Isolated offline conversion of catalog-pinned translation assets."""
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import subprocess
import sys
import time

from smartvoice.domain.errors import InvalidRequestError


def check_dependencies(preparation):
    mismatches = []
    for package, expected in preparation['versions'].items():
        try:
            actual = version(package).split('+')[0]
        except PackageNotFoundError:
            actual = 'missing'
        if actual != expected:
            mismatches.append(f'{package}=={expected} (found {actual})')
    if mismatches:
        raise InvalidRequestError(
            'Model conversion requires pinned preparation dependencies: ' + ', '.join(mismatches)
            + ". Install with: python -m pip install -e '.[model-preparation]'"
        )


def convert(source: Path, destination: Path, check_cancel=None):
    """A cancellation/timeout stops and joins only the owned converter child."""
    env = {**os.environ, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'CUDA_VISIBLE_DEVICES': ''}
    log = destination.parent / 'conversion.log'
    with log.open('wb') as output:
        process = subprocess.Popen(
            [sys.executable, '-m', 'smartvoice.adapters.storage.converted_model', str(source), str(destination)],
            env=env, stdout=output, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 900
        try:
            while process.poll() is None:
                if check_cancel:
                    check_cancel()
                if time.monotonic() >= deadline:
                    raise InvalidRequestError('Model conversion exceeded the 900 second preparation deadline.')
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode:
                raise InvalidRequestError('Pinned offline model conversion failed; verify the preparation dependencies.')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    import ctranslate2
    ctranslate2.converters.TransformersConverter(sys.argv[1]).convert(sys.argv[2], quantization='int8')
