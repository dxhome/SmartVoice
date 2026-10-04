"""Build a scoped Whisper repair wheel; installation is a separate action."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def build(output: Path, jobs: int) -> None:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    patch = Path(__file__).parent / 'patches/sherpa-onnx-1.13.8-whisper.patch'
    with tempfile.TemporaryDirectory(prefix='smartvoice-sherpa-build-') as work:
        source = Path(work) / 'sherpa-onnx'
        subprocess.run(['git', 'clone', '--depth', '1', '--branch', 'v1.13.8',
                        'https://github.com/k2-fsa/sherpa-onnx.git', str(source)], check=True)
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
        if revision != '11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf':
            raise RuntimeError(f'Unexpected upstream revision: {revision}')
        subprocess.run(['git', 'apply', '--check', str(patch.resolve())], cwd=source, check=True)
        subprocess.run(['git', 'apply', str(patch.resolve())], cwd=source, check=True)
        env = os.environ.copy()
        # Upstream tests presence, not the value; even "0" enables splitting.
        env.pop('SHERPA_ONNX_SPLIT_PYTHON_PACKAGE', None)
        env['SHERPA_ONNX_MAKE_ARGS'] = f'-j{jobs}'
        env['SHERPA_ONNX_CMAKE_ARGS'] = (
            '-DCMAKE_BUILD_TYPE=Release -DSHERPA_ONNX_ENABLE_BINARY=OFF '
            '-DSHERPA_ONNX_ENABLE_PORTAUDIO=OFF -DSHERPA_ONNX_ENABLE_WEBSOCKET=OFF '
            '-DSHERPA_ONNX_BUILD_C_API_EXAMPLES=OFF'
        )
        if sys.platform == 'darwin':
            sdk = subprocess.check_output(['xcrun', '--show-sdk-path'], text=True).strip()
            headers = str(Path(sdk) / 'usr/include/c++/v1')
            env['CPLUS_INCLUDE_PATH'] = os.pathsep.join(
                filter(None, [headers, env.get('CPLUS_INCLUDE_PATH')])
            )
        subprocess.run([sys.executable, '-m', 'pip', 'wheel', str(source), '--no-deps',
                        '--wheel-dir', str(output)], env=env, check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('wheels'))
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    build(args.output, args.jobs)
